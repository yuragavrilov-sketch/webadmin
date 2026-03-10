from flask import Flask, jsonify, request, render_template, abort
from cryptography.fernet import Fernet, InvalidToken
from config import Config
from collections import defaultdict
from models import db, Server, AuditLog, ServiceConfig, ServiceGroup, ServiceGroupItem, ConfigSnapshot, ConfigSnapshotFile
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
    return render_template("index.html")


@app.route("/servers")
def servers_page():
    return render_template("servers.html")


@app.route("/logs")
def logs_page():
    return render_template("logs.html")


@app.route("/configs")
def configs_page():
    return render_template("configs.html")


@app.route("/groups")
def groups_page():
    return render_template("groups.html")


@app.route("/services")
def services_page():
    return render_template("services.html")


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

    cfg_record = ServiceConfig.query.filter_by(server_id=server_id, service_name=service_name).first()
    if not cfg_record:
        return jsonify({"error": f"Service '{service_name}' is not configured for this server."}), 403

    data = request.get_json(force=True)
    action = data.get("action", "").lower()
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action. Use: start, stop, restart"}), 400

    success = False
    message = ""
    try:
        mgr = get_winrm(server)

        # Auto-snapshot before the action if a config_dir is configured
        if cfg_record.config_dir:
            try:
                from scheduler import take_snapshot
                take_snapshot(db, mgr, cfg_record, comment=f"pre-action:{action}")
            except Exception as snap_err:
                app.logger.warning("Pre-action snapshot failed for %s: %s", service_name, snap_err)

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
# API — Service-configs global (Services page)
# ---------------------------------------------------------------------------

@app.route("/api/service-configs", methods=["GET"])
def api_all_service_configs():
    """All configured services across all servers, with group membership."""
    configs = (ServiceConfig.query
               .join(ServiceConfig.server)
               .order_by(Server.name, ServiceConfig.sort_order)
               .all())
    result = []
    for c in configs:
        groups = [
            {"item_id": gi.id, "group_id": gi.group_id,
             "name": gi.group.name, "color": gi.group.color}
            for gi in c.group_items if gi.group
        ]
        result.append({
            **c.to_dict(),
            "server_name": c.server.name if c.server else "",
            "server_hostname": c.server.hostname if c.server else "",
            "groups": groups,
        })
    return jsonify(result)


@app.route("/api/service-configs", methods=["POST"])
def api_create_service_config_global():
    """Create a service config from the global Services page (server_id in body)."""
    data = request.get_json(force=True)
    server_id = data.get("server_id")
    if not server_id:
        return jsonify({"error": "server_id is required"}), 400
    server = db.session.get(Server, server_id)
    if not server:
        return jsonify({"error": "Server not found"}), 404

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
    db.session.flush()  # get cfg.id without full commit

    for gid in (data.get("group_ids") or []):
        grp = db.session.get(ServiceGroup, int(gid))
        if grp and not ServiceGroupItem.query.filter_by(group_id=gid, service_config_id=cfg.id).first():
            max_go = db.session.query(db.func.max(ServiceGroupItem.sort_order)).filter_by(group_id=gid).scalar() or 0
            db.session.add(ServiceGroupItem(group_id=gid, service_config_id=cfg.id, sort_order=max_go + 10))

    db.session.commit()
    groups = [
        {"item_id": gi.id, "group_id": gi.group_id, "name": gi.group.name, "color": gi.group.color}
        for gi in cfg.group_items if gi.group
    ]
    return jsonify({**cfg.to_dict(), "server_name": server.name, "groups": groups}), 201


@app.route("/api/service-configs/<int:cfg_id>", methods=["PUT"])
def api_update_service_config_global(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True)
    if "display_name" in data:
        cfg.display_name = (data["display_name"] or "").strip() or None
    if "description" in data:
        cfg.description = (data["description"] or "").strip() or None
    db.session.commit()
    return jsonify(cfg.to_dict())


@app.route("/api/service-configs/<int:cfg_id>", methods=["DELETE"])
def api_delete_service_config_global(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(cfg)
    db.session.commit()
    return jsonify({"message": "Deleted."})


# ---------------------------------------------------------------------------
# API — Groups tree (main page — DB only, no WinRM)
# ---------------------------------------------------------------------------

@app.route("/api/groups/tree", methods=["GET"])
def api_groups_tree():
    """All groups with items pre-grouped by server. No live WinRM calls."""
    groups = ServiceGroup.query.order_by(ServiceGroup.sort_order, ServiceGroup.name).all()
    result = []
    for g in groups:
        servers_map: dict[int, dict] = {}
        for it in g.items:
            cfg = it.service_config
            if not cfg:
                continue
            sid = cfg.server_id
            if sid not in servers_map:
                servers_map[sid] = {
                    "server_id": sid,
                    "server_name": cfg.server.name if cfg.server else "Unknown",
                    "services": [],
                }
            servers_map[sid]["services"].append({
                "item_id": it.id,
                "config_id": cfg.id,
                "server_id": sid,
                "service_name": cfg.service_name,
                "label": cfg.display_name or cfg.service_name,
                "description": cfg.description or "",
            })
        result.append({
            **g.to_dict(),
            "servers": list(servers_map.values()),
        })
    return jsonify(result)


# ---------------------------------------------------------------------------
# API — Groups CRUD
# ---------------------------------------------------------------------------

@app.route("/api/groups", methods=["GET"])
def api_list_groups():
    groups = ServiceGroup.query.order_by(ServiceGroup.sort_order, ServiceGroup.name).all()
    return jsonify([g.to_dict() for g in groups])


@app.route("/api/groups", methods=["POST"])
def api_create_group():
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if ServiceGroup.query.filter_by(name=name).first():
        return jsonify({"error": "Group with this name already exists."}), 409

    max_order = db.session.query(db.func.max(ServiceGroup.sort_order)).scalar() or 0
    grp = ServiceGroup(
        name=name,
        description=(data.get("description") or "").strip() or None,
        color=data.get("color", "primary"),
        sort_order=max_order + 10,
    )
    db.session.add(grp)
    db.session.commit()
    return jsonify(grp.to_dict()), 201


@app.route("/api/groups/<int:group_id>", methods=["GET"])
def api_get_group(group_id):
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404
    result = grp.to_dict()
    result["items"] = [i.to_dict() for i in grp.items]
    return jsonify(result)


@app.route("/api/groups/<int:group_id>", methods=["PUT"])
def api_update_group(group_id):
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404
    data = request.get_json(force=True)
    if "name" in data:
        name = data["name"].strip()
        existing = ServiceGroup.query.filter_by(name=name).first()
        if existing and existing.id != group_id:
            return jsonify({"error": "Group with this name already exists."}), 409
        grp.name = name
    if "description" in data:
        grp.description = (data["description"] or "").strip() or None
    if "color" in data:
        grp.color = data["color"]
    if "sort_order" in data:
        grp.sort_order = int(data["sort_order"])
    db.session.commit()
    return jsonify(grp.to_dict())


@app.route("/api/groups/<int:group_id>", methods=["DELETE"])
def api_delete_group(group_id):
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404
    db.session.delete(grp)
    db.session.commit()
    return jsonify({"message": "Group deleted."})


# ---------------------------------------------------------------------------
# API — Group items
# ---------------------------------------------------------------------------

@app.route("/api/groups/<int:group_id>/items", methods=["POST"])
def api_add_group_item(group_id):
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404

    data = request.get_json(force=True)
    cfg_id = data.get("service_config_id")
    if not cfg_id:
        return jsonify({"error": "service_config_id is required"}), 400

    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "ServiceConfig not found"}), 404

    if ServiceGroupItem.query.filter_by(group_id=group_id, service_config_id=cfg_id).first():
        return jsonify({"error": "This service is already in the group."}), 409

    max_order = db.session.query(db.func.max(ServiceGroupItem.sort_order)).filter_by(group_id=group_id).scalar() or 0
    item = ServiceGroupItem(group_id=group_id, service_config_id=cfg_id, sort_order=max_order + 10)
    db.session.add(item)
    db.session.commit()
    return jsonify(item.to_dict()), 201


@app.route("/api/groups/<int:group_id>/items/<int:item_id>", methods=["DELETE"])
def api_remove_group_item(group_id, item_id):
    item = ServiceGroupItem.query.filter_by(id=item_id, group_id=group_id).first()
    if not item:
        return jsonify({"error": "Item not found"}), 404
    db.session.delete(item)
    db.session.commit()
    return jsonify({"message": "Removed."})


# ---------------------------------------------------------------------------
# API — Group live services + bulk actions
# ---------------------------------------------------------------------------

def _fetch_group_services(group_id: int) -> list[dict]:
    """Return live status for all services in a group, grouped by server for efficiency."""
    items = (ServiceGroupItem.query
             .filter_by(group_id=group_id)
             .order_by(ServiceGroupItem.sort_order)
             .all())
    if not items:
        return []

    # Keep original item order for final sort
    item_order = {it.id: idx for idx, it in enumerate(items)}

    # Group by server to minimise WinRM connections
    by_server: dict[int, list[ServiceGroupItem]] = defaultdict(list)
    for it in items:
        if it.service_config:
            by_server[it.service_config.server_id].append(it)

    results: list[dict] = []
    for server_id, server_items in by_server.items():
        server = db.session.get(Server, server_id)
        if not server:
            continue
        names = [it.service_config.service_name for it in server_items]
        try:
            live_list = get_winrm(server).get_services_by_names(names)
            live_map = {s["name"]: s for s in live_list}
            for it in server_items:
                cfg = it.service_config
                svc = live_map.get(cfg.service_name, {})
                results.append({
                    "item_id": it.id,
                    "config_id": cfg.id,
                    "server_id": server_id,
                    "server_name": server.name,
                    "service_name": cfg.service_name,
                    "label": cfg.display_name or svc.get("display_name", cfg.service_name),
                    "config_description": cfg.description or "",
                    "status": svc.get("status", "Unknown"),
                    "start_type": svc.get("start_type", ""),
                    "error": svc.get("error"),
                })
        except Exception as exc:
            for it in server_items:
                cfg = it.service_config
                results.append({
                    "item_id": it.id,
                    "config_id": cfg.id,
                    "server_id": server_id,
                    "server_name": server.name,
                    "service_name": cfg.service_name,
                    "label": cfg.display_name or cfg.service_name,
                    "config_description": cfg.description or "",
                    "status": "Unknown",
                    "start_type": "",
                    "error": str(exc),
                })

    results.sort(key=lambda r: item_order.get(r["item_id"], 999))
    return results


@app.route("/api/groups/<int:group_id>/services", methods=["GET"])
def api_group_services(group_id):
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404
    try:
        return jsonify(_fetch_group_services(group_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/groups/<int:group_id>/action", methods=["POST"])
def api_group_action(group_id):
    """Perform start / stop / restart on every service in the group."""
    grp = db.session.get(ServiceGroup, group_id)
    if not grp:
        return jsonify({"error": "Group not found"}), 404

    data = request.get_json(force=True)
    action = data.get("action", "").lower()
    if action not in ("start", "stop", "restart"):
        return jsonify({"error": "Invalid action. Use: start, stop, restart"}), 400

    items = ServiceGroupItem.query.filter_by(group_id=group_id).all()
    if not items:
        return jsonify({"error": "Group has no services"}), 400

    # Group by server to reuse WinRM sessions
    by_server: dict[int, list[ServiceGroupItem]] = defaultdict(list)
    for it in items:
        if it.service_config:
            by_server[it.service_config.server_id].append(it)

    action_results = []
    for server_id, server_items in by_server.items():
        server = db.session.get(Server, server_id)
        if not server:
            continue
        try:
            mgr = get_winrm(server)
            for it in server_items:
                cfg = it.service_config
                success, message = False, ""
                try:
                    if action == "start":
                        mgr.start_service(cfg.service_name)
                    elif action == "stop":
                        mgr.stop_service(cfg.service_name)
                    elif action == "restart":
                        mgr.restart_service(cfg.service_name)
                    success, message = True, "OK"
                except WinRMError as e:
                    message = str(e)
                except Exception as e:
                    message = str(e)

                db.session.add(AuditLog(
                    server_id=server_id,
                    service_name=cfg.service_name,
                    action=action,
                    success=success,
                    message=message,
                ))
                action_results.append({
                    "server_name": server.name,
                    "service_name": cfg.service_name,
                    "label": cfg.display_name or cfg.service_name,
                    "success": success,
                    "message": message,
                })
        except Exception as exc:
            for it in server_items:
                cfg = it.service_config
                db.session.add(AuditLog(
                    server_id=server_id,
                    service_name=cfg.service_name,
                    action=action,
                    success=False,
                    message=f"Connection error: {exc}",
                ))
                action_results.append({
                    "server_name": server.name,
                    "service_name": cfg.service_name,
                    "label": cfg.display_name or cfg.service_name,
                    "success": False,
                    "message": f"Connection error: {exc}",
                })

    db.session.commit()
    ok_count = sum(1 for r in action_results if r["success"])
    return jsonify({
        "results": action_results,
        "success_count": ok_count,
        "error_count": len(action_results) - ok_count,
    })


# ---------------------------------------------------------------------------
# API — Config snapshots
# ---------------------------------------------------------------------------

@app.route("/api/service-configs/<int:cfg_id>/config-dir", methods=["PUT"])
def api_update_config_dir(cfg_id):
    """Set (or clear) the config_dir path for a service config."""
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True)
    cfg.config_dir = (data.get("config_dir") or "").strip() or None
    db.session.commit()
    return jsonify(cfg.to_dict())


@app.route("/api/service-configs/<int:cfg_id>/detect-config-dir", methods=["POST"])
def api_detect_config_dir(cfg_id):
    """Auto-detect Config/ directory from the service exe path via WinRM."""
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    try:
        detected = get_winrm(cfg.server).get_service_config_dir(cfg.service_name)
        return jsonify({"config_dir": detected})
    except (WinRMError, Exception) as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/service-configs/<int:cfg_id>/snapshots", methods=["GET"])
def api_list_snapshots(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    snaps = (ConfigSnapshot.query
             .filter_by(service_config_id=cfg_id)
             .order_by(ConfigSnapshot.created_at.desc())
             .all())
    return jsonify([s.to_dict() for s in snaps])


@app.route("/api/service-configs/<int:cfg_id>/snapshots/<int:snap_id>", methods=["GET"])
def api_get_snapshot(cfg_id, snap_id):
    snap = ConfigSnapshot.query.filter_by(id=snap_id, service_config_id=cfg_id).first()
    if not snap:
        return jsonify({"error": "Not found"}), 404
    return jsonify(snap.to_dict(include_files=True))


@app.route("/api/service-configs/<int:cfg_id>/snapshots", methods=["POST"])
def api_create_snapshot_manual(cfg_id):
    """Manually trigger a snapshot for one service config."""
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    if not cfg.config_dir:
        return jsonify({"error": "config_dir not set for this service config"}), 400
    try:
        from scheduler import take_snapshot
        created = take_snapshot(db, get_winrm(cfg.server), cfg, comment="manual")
        return jsonify({"created": created})
    except (WinRMError, Exception) as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/service-configs/<int:cfg_id>/snapshots/<int:snap_id>", methods=["DELETE"])
def api_delete_snapshot(cfg_id, snap_id):
    snap = ConfigSnapshot.query.filter_by(id=snap_id, service_config_id=cfg_id).first()
    if not snap:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(snap)
    db.session.commit()
    return jsonify({"message": "Deleted."})


# ---------------------------------------------------------------------------
# API — Scheduler status / control
# ---------------------------------------------------------------------------

@app.route("/api/scheduler/status", methods=["GET"])
def api_scheduler_status():
    from scheduler import get_scheduler, get_last_run
    sched = get_scheduler()
    job = sched.get_job("config_poll") if sched else None
    return jsonify({
        "running": bool(sched and sched.running),
        "interval_minutes": app.config.get("CONFIG_POLL_INTERVAL_MINUTES", 60),
        "next_run": job.next_run_time.isoformat() if job and job.next_run_time else None,
        "last_run": get_last_run(),
    })


@app.route("/api/scheduler/run-now", methods=["POST"])
def api_scheduler_run_now():
    """Trigger an immediate full config poll in a background thread."""
    import threading
    from scheduler import poll_configs
    threading.Thread(target=poll_configs, args=[app], daemon=True).start()
    return jsonify({"message": "Config poll started in background."})


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
    from scheduler import start_scheduler
    start_scheduler(app)
    # use_reloader=False prevents APScheduler from starting twice in debug mode
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
