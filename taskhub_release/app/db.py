import sqlite3, os
from pathlib import Path
DB_PATH=Path(os.getenv("DB_PATH", "/data/taskhub.sqlite3" if os.getenv("RAILWAY_VOLUME_MOUNT_PATH") else str(Path(__file__).resolve().parent.parent/"taskhub.sqlite3")))
DB_PATH.parent.mkdir(parents=True,exist_ok=True)
def connect():
 c=sqlite3.connect(DB_PATH,timeout=30); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA journal_mode=WAL"); return c
def init_db():
 with connect() as c:
  c.executescript("""
  CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, telegram_id INTEGER UNIQUE NOT NULL, username TEXT, first_name TEXT NOT NULL DEFAULT '', role TEXT NOT NULL DEFAULT 'helper', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
  CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'Другое', reward_kop INTEGER NOT NULL DEFAULT 0 CHECK(reward_kop>=0), status TEXT NOT NULL DEFAULT 'available', assignee_id INTEGER REFERENCES users(id), created_by INTEGER REFERENCES users(id), deadline TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
  CREATE TABLE IF NOT EXISTS submissions(id INTEGER PRIMARY KEY, task_id INTEGER NOT NULL REFERENCES tasks(id), user_id INTEGER NOT NULL REFERENCES users(id), body TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
  CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), task_id INTEGER REFERENCES tasks(id), amount_kop INTEGER NOT NULL, kind TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(task_id,kind));
  CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL, object_type TEXT NOT NULL, object_id INTEGER, details TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
  """)
