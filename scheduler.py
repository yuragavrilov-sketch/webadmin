"""
Config-poll scheduler.

Runs in a background thread (APScheduler BackgroundScheduler).
Every CONFIG_POLL_INTERVAL_MINUTES minutes it reads config directories
for all ServiceConfigs that have either legacy ``config_dir`` or new
``config_dirs``, computes a content hash, and writes a new ConfigSnapshot
only when something changed.

Public API used by app.py:
    start_scheduler(app)  -> BackgroundScheduler
    take_snapshot(db, mgr, cfg, comment)  -> bool   (also called directly for pre-action snaps)
    get_scheduler()  -> BackgroundScheduler | None
    get_last_run()   -> dict
"""
import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None
_last_run: dict = {}


# ---------------------------------------------------------------------------
# Core snapshot logic (shared with on-demand / pre-action callers)
# ---------------------------------------------------------------------------

def _compute_hash(files: dict[str, str]) -> str:
    """Stable SHA-256 over sorted (relative_path, content) pairs."""
    h = hashlib.sha256()
    for path in sorted(files):
        h.update(path.encode())
        h.update(b"\x00")
        h.update((files[path] or "").encode())
        h.update(b"\x00")
    return h.hexdigest()


def take_snapshot(db, mgr, cfg, comment: str = "auto") -> bool:
    """
    Read all text files from resolved config directories via *mgr*.
    Resolution order: ``cfg.config_dir`` (legacy/single) first, then entries
    from ``cfg.config_dirs``.
    Relative paths are prefixed with the directory label (or last folder name)
    so files from different dirs don't collide, e.g. ``Main\\app.json``.

    Compares combined SHA-256 with the latest stored snapshot and writes a
    new ConfigSnapshot + ConfigSnapshotFile rows only when content changed.

    Returns True when a new snapshot was written.
    """
    from models import ConfigSnapshot, ConfigSnapshotFile

    resolved_dirs = []
    legacy_path = (cfg.config_dir or "").strip()
    if legacy_path:
        resolved_dirs.append({"path": legacy_path, "label": "Primary"})

    for cdir in (cfg.config_dirs or []):
        cdir_path = (cdir.path or "").strip()
        if not cdir_path:
            continue
        if legacy_path and cdir_path.lower() == legacy_path.lower():
            continue
        resolved_dirs.append({"path": cdir_path, "label": cdir.label})

    if not resolved_dirs:
        return False

    files: dict[str, str] = {}
    for cdir in resolved_dirs:
        cdir_path = cdir["path"]
        dir_label = (cdir.get("label") or cdir_path.rstrip("\\").rsplit("\\", 1)[-1])
        base = cdir_path.rstrip("\\")
        try:
            rel_paths = mgr.list_config_files(cdir_path)
        except Exception:
            continue
        for rel in rel_paths:
            prefixed = f"{dir_label}\\{rel}"
            try:
                files[prefixed] = mgr.read_config_file(f"{base}\\{rel}")
            except Exception:
                files[prefixed] = ""

    if not files:
        return False

    new_hash = _compute_hash(files)

    last = (
        ConfigSnapshot.query
        .filter_by(service_config_id=cfg.id)
        .order_by(ConfigSnapshot.created_at.desc())
        .first()
    )
    if last and last.content_hash == new_hash:
        return False

    snap = ConfigSnapshot(
        service_config_id=cfg.id,
        comment=comment,
        content_hash=new_hash,
    )
    db.session.add(snap)
    db.session.flush()  # populate snap.id

    for rel, content in files.items():
        db.session.add(ConfigSnapshotFile(
            snapshot_id=snap.id,
            relative_path=rel,
            content=content,
        ))

    db.session.commit()
    return True


# ---------------------------------------------------------------------------
# Scheduled job
# ---------------------------------------------------------------------------

def poll_configs(app) -> None:
    """
    APScheduler job entry-point.
    Iterates over all ServiceConfigs with config directories configured, groups them by
    server to reuse one WinRM connection per server, and calls take_snapshot
    for each service.
    """
    global _last_run

    started_at = datetime.now(timezone.utc)
    snapshots_created = 0
    errors = 0

    with app.app_context():
        from models import db, ServiceConfig, Server
        from winrm_manager import WinRMManager
        from cryptography.fernet import Fernet

        fernet = Fernet(app.config["ENCRYPTION_KEY"])

        configs = (
            ServiceConfig.query
            .filter(
                db.or_(
                    ServiceConfig.config_dirs.any(),
                    db.and_(
                        ServiceConfig.config_dir.isnot(None),
                        ServiceConfig.config_dir != "",
                    ),
                )
            )
            .all()
        )

        by_server: dict[int, list] = defaultdict(list)
        for cfg in configs:
            by_server[cfg.server_id].append(cfg)

        for server_id, server_configs in by_server.items():
            server = db.session.get(Server, server_id)
            if not server:
                continue

            try:
                password = fernet.decrypt(server.password_enc.encode()).decode()
                mgr = WinRMManager(
                    hostname=server.hostname,
                    port=server.port,
                    username=server.username,
                    password=password,
                    use_ssl=server.use_ssl,
                    timeout=app.config["WINRM_TIMEOUT"],
                )
                for cfg in server_configs:
                    try:
                        if take_snapshot(db, mgr, cfg, comment="auto"):
                            snapshots_created += 1
                            logger.info(
                                "Config snapshot saved: service_config #%d (%s) @ %s",
                                cfg.id, cfg.service_name, server.hostname,
                            )
                    except Exception as exc:
                        errors += 1
                        logger.warning(
                            "Config poll failed — %s / %s: %s",
                            server.hostname, cfg.service_name, exc,
                        )

            except Exception as exc:
                errors += len(server_configs)
                logger.warning(
                    "Config poll: cannot connect to %s: %s", server.hostname, exc
                )

    _last_run = {
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "snapshots_created": snapshots_created,
        "errors": errors,
    }
    logger.info(
        "Config poll finished: %d new snapshots, %d errors", snapshots_created, errors
    )


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

def start_scheduler(app) -> BackgroundScheduler:
    global _scheduler
    interval_minutes = app.config.get("CONFIG_POLL_INTERVAL_MINUTES", 60)
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        func=poll_configs,
        args=[app],
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="config_poll",
    )
    _scheduler.start()
    logger.info("Config poll scheduler started, interval=%d min", interval_minutes)
    return _scheduler


def get_scheduler() -> BackgroundScheduler | None:
    return _scheduler


def get_last_run() -> dict:
    return _last_run
