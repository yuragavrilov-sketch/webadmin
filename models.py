from datetime import datetime
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
    config_dir = db.Column(db.String(1000), nullable=True)     # remote path to Config/ folder
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    snapshots = db.relationship(
        "ConfigSnapshot", backref="service_config", lazy=True,
        cascade="all, delete-orphan",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "server_id": self.server_id,
            "service_name": self.service_name,
            "display_name": self.display_name or "",
            "description": self.description or "",
            "config_dir": self.config_dir or "",
            "sort_order": self.sort_order,
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
