"""
Utility script to initialize the database and generate an ENCRYPTION_KEY.
Run once before starting the application:
    python init_db.py
"""
import os
import sys


def generate_key():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    print("\n[*] Generated ENCRYPTION_KEY (save this to your .env file!):")
    print(f"    ENCRYPTION_KEY={key}\n")
    return key


def init_db():
    from dotenv import load_dotenv
    load_dotenv()

    if not os.getenv("ENCRYPTION_KEY"):
        print("[!] ENCRYPTION_KEY not set in .env — generating a new one...")
        generate_key()
        print("    Set ENCRYPTION_KEY in your .env file and re-run init_db.py")
        sys.exit(1)

    from app import app
    from models import db

    with app.app_context():
        db.create_all()
        print("[+] Database tables created successfully.")

        # Migrate existing service_configs table: add config_dir if missing
        from sqlalchemy import inspect, text
        inspector = inspect(db.engine)
        existing_cols = {c["name"] for c in inspector.get_columns("service_configs")}
        if "config_dir" not in existing_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE service_configs ADD COLUMN config_dir VARCHAR(1000)"
                ))
                conn.commit()
            print("[+] Migrated: added config_dir column to service_configs.")

        if "config_dir_detected_at" not in existing_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE service_configs ADD COLUMN config_dir_detected_at TIMESTAMP"
                ))
                conn.commit()
            print("[+] Migrated: added config_dir_detected_at column to service_configs.")

        if "config_dir_source" not in existing_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE service_configs ADD COLUMN config_dir_source VARCHAR(20)"
                ))
                conn.commit()
            print("[+] Migrated: added config_dir_source column to service_configs.")

        # Migrate existing servers table: add env/winrm operational fields
        server_cols = {c["name"] for c in inspector.get_columns("servers")}
        if "is_active" not in server_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE servers ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE"
                ))
                conn.commit()
            print("[+] Migrated: added is_active column to servers.")

        if "last_winrm_check_at" not in server_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE servers ADD COLUMN last_winrm_check_at TIMESTAMP"
                ))
                conn.commit()
            print("[+] Migrated: added last_winrm_check_at column to servers.")

        if "last_winrm_check_ok" not in server_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE servers ADD COLUMN last_winrm_check_ok BOOLEAN"
                ))
                conn.commit()
            print("[+] Migrated: added last_winrm_check_ok column to servers.")

        if "last_winrm_check_message" not in server_cols:
            with db.engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE servers ADD COLUMN last_winrm_check_message TEXT"
                ))
                conn.commit()
            print("[+] Migrated: added last_winrm_check_message column to servers.")

        print("[+] Ready to start: python app.py")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        generate_key()
    else:
        init_db()
