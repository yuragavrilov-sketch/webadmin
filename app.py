from flask import Flask, jsonify, request, render_template, abort
from cryptography.fernet import Fernet, InvalidToken
from config import Config
from collections import defaultdict
import hashlib
import json
from models import (
    db,
    Server,
    AuditLog,
    Environment,
    EnvServer,
    ManagedService,
    ServiceInstance,
    ConfigSyncJob,
    ServiceConfig,
    ServiceGroup,
    ServiceGroupItem,
    ConfigSnapshot,
    ConfigSnapshotFile,
    ServiceConfigDir,
    GroupConfig,
    ServiceConfigOverride,
    ConfigRevision,
)
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


def _normalize_env_code(code: str) -> str:
    return (code or "").strip().upper()


def _update_server_winrm_check(server: Server, success: bool, message: str):
    server.last_winrm_check_at = datetime.utcnow()
    server.last_winrm_check_ok = bool(success)
    server.last_winrm_check_message = (message or "").strip() or None


def _json_required_object(data, field_name: str):
    value = data.get(field_name)
    if value is None:
        return None, jsonify({"error": f"{field_name} is required"}), 400
    if not isinstance(value, dict):
        return None, jsonify({"error": f"{field_name} must be a JSON object"}), 400
    return value, None, None


def _json_to_text(obj: dict) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _content_hash(obj: dict) -> str:
    payload = _json_to_text(obj)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _deep_merge(base, override):
    """Deterministic deep-merge: override has priority over base."""
    if not isinstance(base, dict):
        base = {}
    if not isinstance(override, dict):
        return override

    merged = {}
    for key in sorted(set(base.keys()) | set(override.keys())):
        if key in base and key in override:
            if isinstance(base[key], dict) and isinstance(override[key], dict):
                merged[key] = _deep_merge(base[key], override[key])
            else:
                merged[key] = override[key]
        elif key in override:
            merged[key] = override[key]
        else:
            merged[key] = base[key]
    return merged


def _next_revision_version(scope_type: str, scope_id: int) -> int:
    current = (
        db.session.query(db.func.max(ConfigRevision.version))
        .filter_by(scope_type=scope_type, scope_id=scope_id)
        .scalar()
        or 0
    )
    return current + 1


def _create_config_revision(scope_type: str, scope_id: int, content_obj: dict, comment: str | None, source: str):
    if source not in ("manual", "auto", "ui"):
        source = "manual"
    rev = ConfigRevision(
        scope_type=scope_type,
        scope_id=scope_id,
        version=_next_revision_version(scope_type, scope_id),
        content_hash=_content_hash(content_obj),
        content=_json_to_text(content_obj),
        comment=(comment or "").strip() or None,
        source=(source or "manual").strip() or "manual",
    )
    db.session.add(rev)
    return rev


def _detect_and_fill_config_dir(cfg: ServiceConfig):
    """Best-effort auto-detection. Must not fail create endpoints."""
    try:
        detected = (get_winrm(cfg.server).get_service_config_dir(cfg.service_name) or "").strip()
        if detected:
            cfg.config_dir = detected
            cfg.config_dir_detected_at = datetime.utcnow()
            cfg.config_dir_source = "auto"
    except Exception:
        pass


def _get_primary_group_for_config(cfg: ServiceConfig):
    item = (
        ServiceGroupItem.query
        .filter_by(service_config_id=cfg.id)
        .order_by(ServiceGroupItem.sort_order.asc(), ServiceGroupItem.id.asc())
        .first()
    )
    return item.group if item else None


def _queue_config_sync(instance_id: int, *, priority: int = 100) -> ConfigSyncJob:
    job = ConfigSyncJob(
        service_instance_id=instance_id,
        job_type="sync_config",
        status="queued",
        priority=priority,
        scheduled_at=datetime.utcnow(),
    )
    db.session.add(job)
    return job


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/servers")
def servers_page():
    return render_template("servers.html")


@app.route("/envs")
def envs_page():
    return render_template("envs.html")


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
# API — Environments (ENV directory)
# ---------------------------------------------------------------------------

@app.route("/api/envs", methods=["GET"])
def api_list_envs():
    envs = Environment.query.order_by(Environment.sort_order.asc(), Environment.name.asc()).all()
    return jsonify([e.to_dict() for e in envs])


@app.route("/api/envs", methods=["POST"])
def api_create_env():
    data = request.get_json(force=True)
    code = _normalize_env_code(data.get("code"))
    name = (data.get("name") or "").strip()

    if not code or not name:
        return jsonify({"error": "code and name are required"}), 400
    if Environment.query.filter_by(code=code).first():
        return jsonify({"error": "Environment with this code already exists."}), 409
    if Environment.query.filter_by(name=name).first():
        return jsonify({"error": "Environment with this name already exists."}), 409

    max_order = db.session.query(db.func.max(Environment.sort_order)).scalar() or 0
    env = Environment(
        code=code,
        name=name,
        description=(data.get("description") or "").strip() or None,
        is_active=bool(data.get("is_active", True)),
        sort_order=int(data.get("sort_order", max_order + 10)),
    )
    db.session.add(env)
    db.session.commit()
    return jsonify(env.to_dict()), 201


@app.route("/api/envs/<int:env_id>", methods=["PUT"])
def api_update_env(env_id):
    env = db.session.get(Environment, env_id)
    if not env:
        return jsonify({"error": "Environment not found"}), 404

    data = request.get_json(force=True)
    if "code" in data:
        code = _normalize_env_code(data.get("code"))
        if not code:
            return jsonify({"error": "code cannot be empty"}), 400
        existing = Environment.query.filter_by(code=code).first()
        if existing and existing.id != env_id:
            return jsonify({"error": "Environment with this code already exists."}), 409
        env.code = code

    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "name cannot be empty"}), 400
        existing = Environment.query.filter_by(name=name).first()
        if existing and existing.id != env_id:
            return jsonify({"error": "Environment with this name already exists."}), 409
        env.name = name

    if "description" in data:
        env.description = (data.get("description") or "").strip() or None
    if "is_active" in data:
        env.is_active = bool(data.get("is_active"))
    if "sort_order" in data:
        env.sort_order = int(data.get("sort_order"))

    env.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(env.to_dict())


@app.route("/api/envs/<int:env_id>", methods=["DELETE"])
def api_delete_env(env_id):
    env = db.session.get(Environment, env_id)
    if not env:
        return jsonify({"error": "Environment not found"}), 404
    db.session.delete(env)
    db.session.commit()
    return jsonify({"message": "Environment deleted."})


# ---------------------------------------------------------------------------
# API — Environment servers (link + WinRM validation)
# ---------------------------------------------------------------------------

@app.route("/api/envs/<int:env_id>/servers", methods=["GET"])
def api_list_env_servers(env_id):
    env = db.session.get(Environment, env_id)
    if not env:
        return jsonify({"error": "Environment not found"}), 404

    links = EnvServer.query.filter_by(env_id=env_id).order_by(EnvServer.id.asc()).all()
    return jsonify([l.to_dict() for l in links])


@app.route("/api/envs/<int:env_id>/servers", methods=["POST"])
def api_add_env_server(env_id):
    env = db.session.get(Environment, env_id)
    if not env:
        return jsonify({"error": "Environment not found"}), 404

    data = request.get_json(force=True)
    server = None

    server_id = data.get("server_id")
    if server_id:
        server = db.session.get(Server, int(server_id))
        if not server:
            return jsonify({"error": "Server not found"}), 404
    else:
        required = ("name", "hostname", "username", "password")
        missing = [f for f in required if not data.get(f)]
        if missing:
            return jsonify({"error": f"Missing fields for server create: {', '.join(missing)}"}), 400
        if Server.query.filter_by(name=(data.get("name") or "").strip()).first():
            return jsonify({"error": "Server with this name already exists."}), 409

        server = Server(
            name=(data.get("name") or "").strip(),
            hostname=(data.get("hostname") or "").strip(),
            port=int(data.get("port", 5985)),
            username=(data.get("username") or "").strip(),
            password_enc=encrypt_password(data.get("password")),
            use_ssl=bool(data.get("use_ssl", False)),
            description=(data.get("description") or "").strip() or None,
            is_active=bool(data.get("is_active", True)),
        )
        db.session.add(server)
        db.session.flush()

    existing_link = EnvServer.query.filter_by(env_id=env_id, server_id=server.id).first()
    if existing_link:
        return jsonify({"error": "Server already linked to this environment."}), 409

    link = EnvServer(
        env_id=env_id,
        server_id=server.id,
        winrm_enabled=bool(data.get("winrm_enabled", True)),
    )
    db.session.add(link)

    winrm_ok = False
    winrm_message = "Not tested"
    try:
        winrm_ok, winrm_message = get_winrm(server).test_connection()
    except Exception as exc:
        winrm_ok = False
        winrm_message = str(exc)

    _update_server_winrm_check(server, winrm_ok, winrm_message)
    link.winrm_enabled = bool(link.winrm_enabled and winrm_ok)
    db.session.commit()

    return jsonify({
        "link": link.to_dict(),
        "winrm": {"success": winrm_ok, "message": winrm_message},
    }), 201


@app.route("/api/envs/<int:env_id>/servers/<int:server_id>", methods=["DELETE"])
def api_remove_env_server(env_id, server_id):
    link = EnvServer.query.filter_by(env_id=env_id, server_id=server_id).first()
    if not link:
        return jsonify({"error": "Link not found"}), 404
    db.session.delete(link)
    db.session.commit()
    return jsonify({"message": "Server unlinked from environment."})


@app.route("/api/envs/<int:env_id>/servers/<int:server_id>/test-winrm", methods=["POST"])
def api_test_env_server_winrm(env_id, server_id):
    link = EnvServer.query.filter_by(env_id=env_id, server_id=server_id).first()
    if not link:
        return jsonify({"error": "Link not found"}), 404

    server = link.server
    try:
        ok, message = get_winrm(server).test_connection()
    except Exception as exc:
        ok, message = False, str(exc)

    _update_server_winrm_check(server, ok, message)
    link.winrm_enabled = bool(ok)
    db.session.commit()
    return jsonify({"success": ok, "message": message, "link": link.to_dict()})


# ---------------------------------------------------------------------------
# API — Services catalog (global)
# ---------------------------------------------------------------------------

@app.route("/api/services/catalog", methods=["GET"])
def api_list_services_catalog():
    rows = ManagedService.query.order_by(ManagedService.service_name.asc()).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/services/catalog", methods=["POST"])
def api_create_services_catalog_item():
    data = request.get_json(force=True)
    service_name = (data.get("service_name") or "").strip()
    if not service_name:
        return jsonify({"error": "service_name is required"}), 400
    if ManagedService.query.filter_by(service_name=service_name).first():
        return jsonify({"error": "Service with this name already exists."}), 409

    row = ManagedService(
        service_name=service_name,
        display_name_default=(data.get("display_name_default") or "").strip() or None,
        description=(data.get("description") or "").strip() or None,
        owner_team=(data.get("owner_team") or "").strip() or None,
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@app.route("/api/services/catalog/<int:service_id>", methods=["PUT"])
def api_update_services_catalog_item(service_id):
    row = db.session.get(ManagedService, service_id)
    if not row:
        return jsonify({"error": "Service not found"}), 404

    data = request.get_json(force=True)
    if "service_name" in data:
        service_name = (data.get("service_name") or "").strip()
        if not service_name:
            return jsonify({"error": "service_name cannot be empty"}), 400
        existing = ManagedService.query.filter_by(service_name=service_name).first()
        if existing and existing.id != service_id:
            return jsonify({"error": "Service with this name already exists."}), 409
        row.service_name = service_name

    if "display_name_default" in data:
        row.display_name_default = (data.get("display_name_default") or "").strip() or None
    if "description" in data:
        row.description = (data.get("description") or "").strip() or None
    if "owner_team" in data:
        row.owner_team = (data.get("owner_team") or "").strip() or None
    if "is_active" in data:
        row.is_active = bool(data.get("is_active"))

    row.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(row.to_dict())


# ---------------------------------------------------------------------------
# API — Service instances (env + server + service)
# ---------------------------------------------------------------------------

@app.route("/api/service-instances", methods=["GET"])
def api_list_service_instances():
    env_id = request.args.get("env_id", type=int)
    server_id = request.args.get("server_id", type=int)
    service_id = request.args.get("service_id", type=int)

    q = ServiceInstance.query
    if env_id:
        q = q.filter_by(env_id=env_id)
    if server_id:
        q = q.filter_by(server_id=server_id)
    if service_id:
        q = q.filter_by(service_id=service_id)

    rows = q.order_by(ServiceInstance.sort_order.asc(), ServiceInstance.id.asc()).all()
    return jsonify([r.to_dict() for r in rows])


@app.route("/api/service-instances", methods=["POST"])
def api_create_service_instance():
    data = request.get_json(force=True)
    env_id = data.get("env_id")
    server_id = data.get("server_id")
    service_id = data.get("service_id")
    if not env_id or not server_id or not service_id:
        return jsonify({"error": "env_id, server_id and service_id are required"}), 400

    env = db.session.get(Environment, int(env_id))
    server = db.session.get(Server, int(server_id))
    service = db.session.get(ManagedService, int(service_id))
    if not env or not server or not service:
        return jsonify({"error": "Environment, Server or Service not found"}), 404

    if not EnvServer.query.filter_by(env_id=env.id, server_id=server.id).first():
        return jsonify({"error": "Server is not linked to this environment"}), 400

    existing = ServiceInstance.query.filter_by(env_id=env.id, server_id=server.id, service_id=service.id).first()
    if existing:
        return jsonify({"error": "Service instance already exists"}), 409

    max_order = (
        db.session.query(db.func.max(ServiceInstance.sort_order))
        .filter_by(env_id=env.id, server_id=server.id)
        .scalar()
        or 0
    )
    inst = ServiceInstance(
        env_id=env.id,
        server_id=server.id,
        service_id=service.id,
        display_name_override=(data.get("display_name_override") or "").strip() or None,
        description_override=(data.get("description_override") or "").strip() or None,
        sort_order=int(data.get("sort_order", max_order + 10)),
        config_sync_state="pending",
    )
    db.session.add(inst)
    db.session.flush()
    _queue_config_sync(inst.id)
    db.session.commit()
    return jsonify(inst.to_dict()), 201


@app.route("/api/service-instances/<int:instance_id>", methods=["PUT"])
def api_update_service_instance(instance_id):
    inst = db.session.get(ServiceInstance, instance_id)
    if not inst:
        return jsonify({"error": "Service instance not found"}), 404

    data = request.get_json(force=True)
    if "display_name_override" in data:
        inst.display_name_override = (data.get("display_name_override") or "").strip() or None
    if "description_override" in data:
        inst.description_override = (data.get("description_override") or "").strip() or None
    if "sort_order" in data:
        inst.sort_order = int(data.get("sort_order"))
    if "config_dir_path" in data:
        inst.config_dir_path = (data.get("config_dir_path") or "").strip() or None
        inst.config_dir_source = "manual" if inst.config_dir_path else inst.config_dir_source
    if "config_sync_state" in data:
        inst.config_sync_state = (data.get("config_sync_state") or "").strip() or inst.config_sync_state

    inst.updated_at = datetime.utcnow()
    db.session.commit()
    return jsonify(inst.to_dict())


@app.route("/api/service-instances/<int:instance_id>", methods=["DELETE"])
def api_delete_service_instance(instance_id):
    inst = db.session.get(ServiceInstance, instance_id)
    if not inst:
        return jsonify({"error": "Service instance not found"}), 404
    db.session.delete(inst)
    db.session.commit()
    return jsonify({"message": "Service instance deleted."})


@app.route("/api/service-instances/<int:instance_id>/sync-config", methods=["POST"])
def api_trigger_service_instance_sync(instance_id):
    inst = db.session.get(ServiceInstance, instance_id)
    if not inst:
        return jsonify({"error": "Service instance not found"}), 404

    inst.config_sync_state = "pending"
    inst.config_sync_message = None
    _queue_config_sync(instance_id, priority=10)
    db.session.commit()
    return jsonify({"message": "Config sync queued.", "instance": inst.to_dict()})


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
    db.session.flush()
    _detect_and_fill_config_dir(cfg)
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

        # Auto-snapshot before the action if any config dirs are configured
        if cfg_record.config_dirs:
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
    _detect_and_fill_config_dir(cfg)

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


@app.route("/api/groups/<int:group_id>/config", methods=["GET"])
def api_get_group_config(group_id):
    group = db.session.get(ServiceGroup, group_id)
    if not group:
        return jsonify({"error": "Group not found"}), 404
    gc = GroupConfig.query.filter_by(group_id=group_id).first()
    base = json.loads(gc.base_config) if gc and gc.base_config else {}
    return jsonify({
        "group_id": group_id,
        "base_config": base,
        "updated_at": gc.updated_at.isoformat() if gc and gc.updated_at else None,
    })


@app.route("/api/groups/<int:group_id>/config", methods=["PUT"])
def api_put_group_config(group_id):
    group = db.session.get(ServiceGroup, group_id)
    if not group:
        return jsonify({"error": "Group not found"}), 404

    data = request.get_json(force=True)
    base_config, error_resp, error_code = _json_required_object(data, "base_config")
    if error_resp:
        return error_resp, error_code

    gc = GroupConfig.query.filter_by(group_id=group_id).first()
    if not gc:
        gc = GroupConfig(group_id=group_id, base_config="{}")
        db.session.add(gc)

    gc.base_config = _json_to_text(base_config)
    gc.updated_at = datetime.utcnow()

    _create_config_revision(
        scope_type="group",
        scope_id=group_id,
        content_obj=base_config,
        comment=data.get("comment") or "group config updated",
        source=(data.get("source") or "manual"),
    )

    affected_cfg_ids = [it.service_config_id for it in group.items if it.service_config_id]
    for cfg_id in affected_cfg_ids:
        ov = ServiceConfigOverride.query.filter_by(service_config_id=cfg_id).first()
        ov_obj = json.loads(ov.override_config) if ov and ov.override_config else {}
        eff_obj = _deep_merge(base_config, ov_obj)
        _create_config_revision(
            scope_type="effective",
            scope_id=cfg_id,
            content_obj=eff_obj,
            comment=f"effective config updated by group:{group_id}",
            source=(data.get("source") or "manual"),
        )

    db.session.commit()
    return jsonify({
        "group_id": group_id,
        "base_config": base_config,
        "updated_at": gc.updated_at.isoformat() if gc.updated_at else None,
    })


@app.route("/api/service-configs/<int:cfg_id>/config-override", methods=["GET"])
def api_get_config_override(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    ov = ServiceConfigOverride.query.filter_by(service_config_id=cfg_id).first()
    override_obj = json.loads(ov.override_config) if ov and ov.override_config else {}
    return jsonify({
        "service_config_id": cfg_id,
        "override_config": override_obj,
        "updated_at": ov.updated_at.isoformat() if ov and ov.updated_at else None,
    })


@app.route("/api/service-configs/<int:cfg_id>/config-override", methods=["PUT"])
def api_put_config_override(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404

    data = request.get_json(force=True)
    override_config, error_resp, error_code = _json_required_object(data, "override_config")
    if error_resp:
        return error_resp, error_code

    ov = ServiceConfigOverride.query.filter_by(service_config_id=cfg_id).first()
    if not ov:
        ov = ServiceConfigOverride(service_config_id=cfg_id, override_config="{}")
        db.session.add(ov)

    ov.override_config = _json_to_text(override_config)
    ov.updated_at = datetime.utcnow()

    _create_config_revision(
        scope_type="instance",
        scope_id=cfg_id,
        content_obj=override_config,
        comment=data.get("comment") or "instance override updated",
        source=(data.get("source") or "manual"),
    )

    group = _get_primary_group_for_config(cfg)
    gc = GroupConfig.query.filter_by(group_id=group.id).first() if group else None
    base_obj = json.loads(gc.base_config) if gc and gc.base_config else {}
    eff_obj = _deep_merge(base_obj, override_config)
    _create_config_revision(
        scope_type="effective",
        scope_id=cfg_id,
        content_obj=eff_obj,
        comment=data.get("comment") or "effective config recalculated",
        source=(data.get("source") or "manual"),
    )

    db.session.commit()
    return jsonify({
        "service_config_id": cfg_id,
        "override_config": override_config,
        "updated_at": ov.updated_at.isoformat() if ov.updated_at else None,
    })


@app.route("/api/service-configs/<int:cfg_id>/config-override", methods=["DELETE"])
def api_delete_config_override(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404

    ov = ServiceConfigOverride.query.filter_by(service_config_id=cfg_id).first()
    if ov:
        db.session.delete(ov)

    _create_config_revision(
        scope_type="instance",
        scope_id=cfg_id,
        content_obj={},
        comment="instance override deleted",
        source="manual",
    )

    group = _get_primary_group_for_config(cfg)
    gc = GroupConfig.query.filter_by(group_id=group.id).first() if group else None
    base_obj = json.loads(gc.base_config) if gc and gc.base_config else {}
    _create_config_revision(
        scope_type="effective",
        scope_id=cfg_id,
        content_obj=base_obj,
        comment="effective config reset to base after override delete",
        source="manual",
    )
    db.session.commit()
    return jsonify({"message": "Deleted."})


@app.route("/api/service-configs/<int:cfg_id>/effective-config", methods=["GET"])
def api_get_effective_config(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404

    selected_group_id_raw = request.args.get("group_id")
    if selected_group_id_raw is not None:
        try:
            selected_group_id = int(selected_group_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "group_id must be an integer"}), 400

        group = db.session.get(ServiceGroup, selected_group_id)
        if not group:
            return jsonify({"error": "Group not found"}), 404

        membership = ServiceGroupItem.query.filter_by(
            group_id=selected_group_id,
            service_config_id=cfg_id,
        ).first()
        if not membership:
            return jsonify({"error": "Service config is not a member of selected group"}), 400
    else:
        group = _get_primary_group_for_config(cfg)

    gc = GroupConfig.query.filter_by(group_id=group.id).first() if group else None
    ov = ServiceConfigOverride.query.filter_by(service_config_id=cfg_id).first()

    base_obj = json.loads(gc.base_config) if gc and gc.base_config else {}
    override_obj = json.loads(ov.override_config) if ov and ov.override_config else {}
    effective_obj = _deep_merge(base_obj, override_obj)

    return jsonify({
        "service_config_id": cfg_id,
        "group_id": group.id if group else None,
        "base_config": base_obj,
        "override_config": override_obj,
        "effective_config": effective_obj,
    })


@app.route("/api/groups/<int:group_id>/config/revisions", methods=["GET"])
def api_list_group_config_revisions(group_id):
    group = db.session.get(ServiceGroup, group_id)
    if not group:
        return jsonify({"error": "Group not found"}), 404

    revisions = (
        ConfigRevision.query
        .filter_by(scope_type="group", scope_id=group_id)
        .order_by(ConfigRevision.version.desc())
        .all()
    )
    return jsonify([r.to_dict() for r in revisions])


@app.route("/api/groups/<int:group_id>/config/revisions", methods=["POST"])
def api_create_group_config_revision(group_id):
    group = db.session.get(ServiceGroup, group_id)
    if not group:
        return jsonify({"error": "Group not found"}), 404

    data = request.get_json(silent=True) or {}
    content_obj = data.get("content")
    if content_obj is None:
        gc = GroupConfig.query.filter_by(group_id=group_id).first()
        content_obj = json.loads(gc.base_config) if gc and gc.base_config else {}
    if not isinstance(content_obj, dict):
        return jsonify({"error": "content must be a JSON object"}), 400

    rev = _create_config_revision(
        scope_type="group",
        scope_id=group_id,
        content_obj=content_obj,
        comment=data.get("comment") or "group revision created",
        source=(data.get("source") or "manual"),
    )
    db.session.commit()
    return jsonify(rev.to_dict()), 201


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

@app.route("/api/service-configs/<int:cfg_id>/config-dirs", methods=["GET"])
def api_list_config_dirs(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    return jsonify([d.to_dict() for d in cfg.config_dirs])


@app.route("/api/service-configs/<int:cfg_id>/config-dirs", methods=["POST"])
def api_add_config_dir(cfg_id):
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True)
    path = (data.get("path") or "").strip()
    if not path:
        return jsonify({"error": "path is required"}), 400
    max_order = db.session.query(db.func.max(ServiceConfigDir.sort_order))\
        .filter_by(service_config_id=cfg_id).scalar() or 0
    d = ServiceConfigDir(
        service_config_id=cfg_id,
        path=path,
        label=(data.get("label") or "").strip() or None,
        sort_order=max_order + 10,
    )
    db.session.add(d)
    db.session.commit()
    return jsonify(d.to_dict()), 201


@app.route("/api/service-configs/<int:cfg_id>/config-dirs/<int:dir_id>", methods=["PUT"])
def api_update_config_dir(cfg_id, dir_id):
    d = ServiceConfigDir.query.filter_by(id=dir_id, service_config_id=cfg_id).first()
    if not d:
        return jsonify({"error": "Not found"}), 404
    data = request.get_json(force=True)
    if "path" in data:
        d.path = data["path"].strip()
    if "label" in data:
        d.label = (data["label"] or "").strip() or None
    db.session.commit()
    return jsonify(d.to_dict())


@app.route("/api/service-configs/<int:cfg_id>/config-dirs/<int:dir_id>", methods=["DELETE"])
def api_delete_config_dir(cfg_id, dir_id):
    d = ServiceConfigDir.query.filter_by(id=dir_id, service_config_id=cfg_id).first()
    if not d:
        return jsonify({"error": "Not found"}), 404
    db.session.delete(d)
    db.session.commit()
    return jsonify({"message": "Deleted."})


@app.route("/api/service-configs/<int:cfg_id>/detect-config-dir", methods=["POST"])
def api_detect_config_dir(cfg_id):
    """Auto-detect Config/ directory from service exe path via WinRM (returns path, does not save)."""
    cfg = db.session.get(ServiceConfig, cfg_id)
    if not cfg:
        return jsonify({"error": "Not found"}), 404
    try:
        detected = get_winrm(cfg.server).get_service_config_dir(cfg.service_name)
        return jsonify({"path": detected})
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

    resolved_config_dir = (cfg.config_dir or "").strip()
    if not resolved_config_dir and cfg.config_dirs:
        resolved_config_dir = (cfg.config_dirs[0].path or "").strip()

    if resolved_config_dir and not cfg.config_dirs:
        db.session.add(ServiceConfigDir(service_config_id=cfg.id, path=resolved_config_dir, label="Primary", sort_order=10))
        db.session.flush()

    if not resolved_config_dir:
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
