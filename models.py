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
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "server_id": self.server_id,
            "service_name": self.service_name,
            "display_name": self.display_name or "",
            "description": self.description or "",
            "sort_order": self.sort_order,
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
