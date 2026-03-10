from flask import Flask, jsonify, request, render_template, abort
from cryptography.fernet import Fernet, InvalidToken
from config import Config
from models import db, Server, AuditLog, ServiceConfig
from winrm_manager import WinRMManager, WinRMError
from datetime import datetime

app = Flask(__name__)
app.config.from_object(Config)
db.init_app(app)


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------

def _get_fernet() -> Fernet:
    key = app.config["ENCRYPTION_KEY"]
    if not key:
        raise RuntimeError("ENCRYPTION_KEY is not set in environment variables.")
    return Fernet(key)


def encrypt_password(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_password(token: str) -> str:
    try:
        return _get_fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt password — check ENCRYPTION_KEY.")


# ---------------------------------------------------------------------------
# WinRM session factory
# ---------------------------------------------------------------------------

def get_winrm(server: Server) -> WinRMManager:
    password = decrypt_password(server.password_enc)
    return WinRMManager(
        hostname=server.hostname,
        port=server.port,
        username=server.username,
        password=password,
        use_ssl=server.use_ssl,
        timeout=app.config["WINRM_TIMEOUT"],
    )


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    servers = Server.query.order_by(Server.name).all()
    return render_template("index.html", servers=servers)


@app.route("/servers")
def servers_page():
    return render_template("servers.html")


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


# ---------------------------------------------------------------------------
# API — Servers CRUD
# ---------------------------------------------------------------------------

@app.route("/api/servers", methods=["GET"])
def api_list_servers():
    servers = Server.query.order_by(Server.name).all()
    return jsonify([s.to_dict() for s in servers])


@app.route("/api/servers", methods=["POST"])
def api_create_server():
    data = request.get_json(force=True)
    required = ("name", "hostname", "username", "password")
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400

    if Server.query.filter_by(name=data["name"]).first():
        return jsonify({"error": "Server with this name already exists."}), 409

    server = Server(
        name=data["name"].strip(),
        hostname=data["hostname"].strip(),
        port=int(data.get("port", 5985)),
        username=data["username"].strip(),
        password_enc=encrypt_password(data["password"]),
        use_ssl=bool(data.get("use_ssl", False)),
        description=data.get("description", "").strip(),
    )
    db.session.add(server)
    db.session.commit()
    return jsonify(server.to_dict()), 201


@app.route("/api/servers/<int:server_id>", methods=["GET"])
def api_get_server(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404
    return jsonify(server.to_dict())


@app.route("/api/servers/<int:server_id>", methods=["PUT"])
def api_update_server(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    data = request.get_json(force=True)

    if "name" in data:
        existing = Server.query.filter_by(name=data["name"]).first()
        if existing and existing.id != server_id:
            return jsonify({"error": "Server with this name already exists."}), 409
        server.name = data["name"].strip()

    if "hostname" in data:
        server.hostname = data["hostname"].strip()
    if "port" in data:
        server.port = int(data["port"])
    if "username" in data:
        server.username = data["username"].strip()
    if "password" in data and data["password"]:
        server.password_enc = encrypt_password(data["password"])
    if "use_ssl" in data:
        server.use_ssl = bool(data["use_ssl"])
    if "description" in data:
        server.description = data["description"].strip()

    server.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(server.to_dict())


@app.route("/api/servers/<int:server_id>", methods=["DELETE"])
def api_delete_server(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404
    db.session.delete(server)
    db.session.commit()
    return jsonify({"message": "Server deleted."})


@app.route("/api/servers/<int:server_id>/test", methods=["POST"])
def api_test_connection(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404
    try:
        mgr = get_winrm(server)
        ok, msg = mgr.test_connection()
        return jsonify({"success": ok, "message": msg})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# ---------------------------------------------------------------------------
# API — Service configs (which services to manage per server)
# ---------------------------------------------------------------------------

@app.route("/api/servers/<int:server_id>/service-configs", methods=["GET"])
def api_list_service_configs(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404
    configs = ServiceConfig.query.filter_by(server_id=server_id).order_by(ServiceConfig.sort_order).all()
    return jsonify([c.to_dict() for c in configs])


@app.route("/api/servers/<int:server_id>/service-configs", methods=["POST"])
def api_create_service_config(server_id):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    data = request.get_json(force=True)
    service_name = (data.get("service_name") or "").strip()
    if not service_name:
        return jsonify({"error": "service_name is required"}), 400

    if ServiceConfig.query.filter_by(server_id=server_id, service_name=service_name).first():
        return jsonify({"error": f"Service '{service_name}' is already configured for this server."}), 409

    max_order = db.session.query(db.func.max(ServiceConfig.sort_order)).filter_by(server_id=server_id).scalar() or 0
    cfg = ServiceConfig(
        server_id=server_id,
        service_name=service_name,
        display_name=(data.get("display_name") or "").strip() or None,
        description=(data.get("description") or "").strip() or None,
        sort_order=max_order + 10,
    )
    db.session.add(cfg)
    db.session.commit()
    return jsonify(cfg.to_dict()), 201


@app.route("/api/servers/<int:server_id>/service-configs/<int:cfg_id>", methods=["PUT"])
def api_update_service_config(server_id, cfg_id):
    cfg = ServiceConfig.query.filter_by(id=cfg_id, server_id=server_id).first()
    if not cfg:
        return jsonify({"error": "Config not found"}), 404

    data = request.get_json(force=True)
    if "display_name" in data:
        cfg.display_name = (data["display_name"] or "").strip() or None
    if "description" in data:
        cfg.description = (data["description"] or "").strip() or None
    if "sort_order" in data:
        cfg.sort_order = int(data["sort_order"])
    db.session.commit()
    return jsonify(cfg.to_dict())


@app.route("/api/servers/<int:server_id>/service-configs/<int:cfg_id>", methods=["DELETE"])
def api_delete_service_config(server_id, cfg_id):
    cfg = ServiceConfig.query.filter_by(id=cfg_id, server_id=server_id).first()
    if not cfg:
        return jsonify({"error": "Config not found"}), 404
    db.session.delete(cfg)
    db.session.commit()
    return jsonify({"message": "Removed."})


# ---------------------------------------------------------------------------
# API — Services  (operates only on configured services)
# ---------------------------------------------------------------------------

@app.route("/api/servers/<int:server_id>/services", methods=["GET"])
def api_list_services(server_id):
    """Return live status for all *configured* services on the server.

    Pass ?all=1 to return the full service list from the remote host
    (used when browsing services to add to the config).
    """
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    browse_all = request.args.get("all") == "1"
    try:
        mgr = get_winrm(server)
        if browse_all:
            return jsonify(mgr.list_services())

        configs = ServiceConfig.query.filter_by(server_id=server_id).order_by(ServiceConfig.sort_order).all()
        if not configs:
            return jsonify([])

        names = [c.service_name for c in configs]
        live = mgr.get_services_by_names(names)

        # Merge live status with config metadata (label override, description)
        cfg_map = {c.service_name: c for c in configs}
        result = []
        for svc in live:
            cfg = cfg_map.get(svc["name"])
            result.append({
                **svc,
                "config_id": cfg.id if cfg else None,
                "label": (cfg.display_name if cfg and cfg.display_name else svc["display_name"]),
                "config_description": cfg.description if cfg else "",
            })
        return jsonify(result)

    except WinRMError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Connection error: {e}"}), 502


@app.route("/api/servers/<int:server_id>/services/<service_name>", methods=["GET"])
def api_get_service(server_id, service_name):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404
    try:
        mgr = get_winrm(server)
        svc = mgr.get_service(service_name)
        return jsonify(svc)
    except WinRMError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        return jsonify({"error": f"Connection error: {e}"}), 502


@app.route("/api/servers/<int:server_id>/services/<service_name>/action", methods=["POST"])
def api_service_action(server_id, service_name):
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

    # Verify the service is in the configured list
    if not ServiceConfig.query.filter_by(server_id=server_id, service_name=service_name).first():
        return jsonify({"error": f"Service '{service_name}' is not configured for this server."}), 403

    data = request.get_json(force=True)
    action = data.get("action", "").lower()
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action. Use: start, stop, restart"}), 400

    success = False
    message = ""
    try:
        mgr = get_winrm(server)
        if action == "start":
            message = mgr.start_service(service_name)
        elif action == "stop":
            message = mgr.stop_service(service_name)
        elif action == "restart":
            message = mgr.restart_service(service_name)
        success = True
        message = message or "OK"
    except WinRMError as e:
        message = str(e)
    except Exception as e:
        message = f"Connection error: {e}"

    db.session.add(AuditLog(
        server_id=server_id,
        service_name=service_name,
        action=action,
        success=success,
        message=message,
    ))
    db.session.commit()

    return jsonify({"success": success, "message": message}), (200 if success else 502)


# ---------------------------------------------------------------------------
# API — Audit logs
# ---------------------------------------------------------------------------

@app.route("/api/logs", methods=["GET"])
def api_list_logs():
    server_id = request.args.get("server_id", type=int)
    limit = min(int(request.args.get("limit", 200)), 1000)

    query = AuditLog.query.order_by(AuditLog.performed_at.desc())
    if server_id:
        query = query.filter_by(server_id=server_id)
    logs = query.limit(limit).all()
    return jsonify([l.to_dict() for l in logs])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(host="0.0.0.0", port=5000, debug=True)
