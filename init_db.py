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
        print("[+] Ready to start: python app.py")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "genkey":
        generate_key()
    else:
        init_db()
