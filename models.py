from datetime import datetime
import json
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class Server(db.Model):
    __tablename__ = "servers"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    hostname = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=5985)
    username = db.Column(db.String(255), nullable=False)
    password_enc = db.Column(db.Text, nullable=False)
    use_ssl = db.Column(db.Boolean, nullable=False, default=False)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    logs = db.relationship("AuditLog", backref="server", lazy=True, cascade="all, delete-orphan")
    service_configs = db.relationship("ServiceConfig", backref="server", lazy=True, cascade="all, delete-orphan", order_by="ServiceConfig.sort_order")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "hostname": self.hostname,
            "port": self.port,
            "username": self.username,
            "use_ssl": self.use_ssl,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ServiceConfig(db.Model):
    """A specific Windows service configured for monitoring on a given server."""
    __tablename__ = "service_configs"
    __table_args__ = (db.UniqueConstraint("server_id", "service_name", name="uq_server_service"),)

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("servers.id"), nullable=False)
    service_name = db.Column(db.String(255), nullable=False)   # actual Windows service name
    display_name = db.Column(db.String(255), nullable=True)    # optional label override
    description = db.Column(db.Text, nullable=True)
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    config_dir = db.Column(db.String(1000), nullable=True)
    config_dir_detected_at = db.Column(db.DateTime, nullable=True)
    config_dir_source = db.Column(db.String(20), nullable=True)  # auto/manual
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    config_dirs = db.relationship(
        "ServiceConfigDir", backref="service_config", lazy=True,
        cascade="all, delete-orphan", order_by="ServiceConfigDir.sort_order",
    )
    snapshots = db.relationship(
        "ConfigSnapshot", backref="service_config", lazy=True,
        cascade="all, delete-orphan",
    )
    config_override = db.relationship(
        "ServiceConfigOverride",
        backref="service_config",
        uselist=False,
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "server_id": self.server_id,
            "service_name": self.service_name,
            "display_name": self.display_name or "",
            "description": self.description or "",
            "sort_order": self.sort_order,
            "config_dir": self.config_dir,
            "config_dir_detected_at": self.config_dir_detected_at.isoformat() if self.config_dir_detected_at else None,
            "config_dir_source": self.config_dir_source,
        }


class ServiceGroup(db.Model):
    """A named logical group of services, potentially spanning multiple servers."""
    __tablename__ = "service_groups"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=True)
    color = db.Column(db.String(20), nullable=False, default="primary")  # Bootstrap color name
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    items = db.relationship(
        "ServiceGroupItem", backref="group", lazy=True,
        cascade="all, delete-orphan", order_by="ServiceGroupItem.sort_order"
    )
    group_config = db.relationship(
        "GroupConfig",
        backref="group",
        uselist=False,
        lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "color": self.color,
            "sort_order": self.sort_order,
            "item_count": len(self.items),
        }


class ServiceGroupItem(db.Model):
    """A single service (from service_configs) that belongs to a group."""
    __tablename__ = "service_group_items"
    __table_args__ = (
        db.UniqueConstraint("group_id", "service_config_id", name="uq_group_config_item"),
    )

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("service_groups.id"), nullable=False)
    service_config_id = db.Column(
        db.Integer, db.ForeignKey("service_configs.id", ondelete="CASCADE"), nullable=False
    )
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    service_config = db.relationship("ServiceConfig", backref=db.backref("group_items", lazy=True))

    def to_dict(self):
        cfg = self.service_config
        return {
            "id": self.id,
            "group_id": self.group_id,
            "service_config_id": self.service_config_id,
            "server_id": cfg.server_id if cfg else None,
            "server_name": cfg.server.name if cfg and cfg.server else None,
            "service_name": cfg.service_name if cfg else "",
            "display_name": cfg.display_name or "" if cfg else "",
            "description": cfg.description or "" if cfg else "",
            "sort_order": self.sort_order,
        }


class ServiceConfigDir(db.Model):
    """One remote Config/ directory path associated with a ServiceConfig."""
    __tablename__ = "service_config_dirs"

    id = db.Column(db.Integer, primary_key=True)
    service_config_id = db.Column(
        db.Integer, db.ForeignKey("service_configs.id", ondelete="CASCADE"), nullable=False
    )
    path = db.Column(db.String(1000), nullable=False)    # full remote directory path
    label = db.Column(db.String(100), nullable=True)     # human-readable name, e.g. "Main", "Logging"
    sort_order = db.Column(db.Integer, nullable=False, default=0)

    def to_dict(self):
        return {
            "id": self.id,
            "service_config_id": self.service_config_id,
            "path": self.path,
            "label": self.label or "",
            "sort_order": self.sort_order,
        }


class GroupConfig(db.Model):
    """Base JSON config for a service group (1:1 with ServiceGroup)."""
    __tablename__ = "group_configs"

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(
        db.Integer, db.ForeignKey("service_groups.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    base_config = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "group_id": self.group_id,
            "base_config": json.loads(self.base_config or "{}"),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ServiceConfigOverride(db.Model):
    """Instance-level JSON override for a configured service (1:1 with ServiceConfig)."""
    __tablename__ = "service_config_overrides"

    id = db.Column(db.Integer, primary_key=True)
    service_config_id = db.Column(
        db.Integer, db.ForeignKey("service_configs.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    override_config = db.Column(db.Text, nullable=False, default="{}")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "service_config_id": self.service_config_id,
            "override_config": json.loads(self.override_config or "{}"),
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ConfigRevision(db.Model):
    """Versioned config blob for group/instance/effective scopes."""
    __tablename__ = "config_revisions"
    __table_args__ = (
        db.UniqueConstraint("scope_type", "scope_id", "version", name="uq_config_revision_scope_version"),
    )

    id = db.Column(db.Integer, primary_key=True)
    scope_type = db.Column(db.String(20), nullable=False)  # group/instance/effective
    scope_id = db.Column(db.Integer, nullable=False)
    version = db.Column(db.Integer, nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    content = db.Column(db.Text, nullable=False)
    comment = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(20), nullable=False, default="manual")  # manual/auto

    def to_dict(self):
        return {
            "id": self.id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "version": self.version,
            "content_hash": self.content_hash,
            "content": json.loads(self.content or "{}"),
            "comment": self.comment or "",
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "source": self.source,
        }


class ConfigSnapshot(db.Model):
    """Point-in-time snapshot of all files in a service's Config/ directory."""
    __tablename__ = "config_snapshots"

    id = db.Column(db.Integer, primary_key=True)
    service_config_id = db.Column(
        db.Integer, db.ForeignKey("service_configs.id", ondelete="CASCADE"), nullable=False
    )
    # source: "auto" (scheduler), "manual", "pre-action:start", "pre-action:stop", "pre-action:restart"
    comment = db.Column(db.String(200), nullable=True)
    content_hash = db.Column(db.String(64), nullable=False)  # sha256 for change detection
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    files = db.relationship(
        "ConfigSnapshotFile", backref="snapshot", lazy=True, cascade="all, delete-orphan"
    )

    def to_dict(self, include_files: bool = False):
        d = {
            "id": self.id,
            "service_config_id": self.service_config_id,
            "comment": self.comment or "",
            "content_hash": self.content_hash,
            "file_count": len(self.files),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_files:
            d["files"] = [f.to_dict() for f in self.files]
        return d


class ConfigSnapshotFile(db.Model):
    """A single file captured inside a ConfigSnapshot."""
    __tablename__ = "config_snapshot_files"

    id = db.Column(db.Integer, primary_key=True)
    snapshot_id = db.Column(
        db.Integer, db.ForeignKey("config_snapshots.id", ondelete="CASCADE"), nullable=False
    )
    relative_path = db.Column(db.String(1000), nullable=False)
    content = db.Column(db.Text, nullable=True)

    def to_dict(self):
        return {
            "id": self.id,
            "snapshot_id": self.snapshot_id,
            "relative_path": self.relative_path,
            "content": self.content or "",
        }


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    server_id = db.Column(db.Integer, db.ForeignKey("servers.id"), nullable=False)
    service_name = db.Column(db.String(255), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # start, stop, restart
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text, nullable=True)
    performed_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "server_id": self.server_id,
            "server_name": self.server.name if self.server else None,
            "service_name": self.service_name,
            "action": self.action,
            "success": self.success,
            "message": self.message,
            "performed_at": self.performed_at.isoformat() if self.performed_at else None,
        }
