import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "taskhub.db"

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    with connect() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            username TEXT,
            first_name TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'helper',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """)
