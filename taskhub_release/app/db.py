import os
import sqlite3
from pathlib import Path

DB_PATH = Path(
    os.getenv(
        "DB_PATH",
        "/data/taskhub.sqlite3"
        if os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
        else str(Path(__file__).resolve().parent.parent / "taskhub.sqlite3"),
    )
)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _add_column(conn, table, definition):
    name = definition.split()[0]
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY,
                telegram_id INTEGER UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'helper',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS projects(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                status TEXT NOT NULL DEFAULT 'active',
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS stages(
                id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                position INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tours(
                id INTEGER PRIMARY KEY,
                stage_id INTEGER NOT NULL REFERENCES stages(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                budget_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_kop>=0),
                position INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS tasks(
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'Другое',
                reward_kop INTEGER NOT NULL DEFAULT 0 CHECK(reward_kop>=0),
                status TEXT NOT NULL DEFAULT 'available',
                assignee_id INTEGER REFERENCES users(id),
                created_by INTEGER REFERENCES users(id),
                deadline TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS submissions(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                body TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS ledger(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                task_id INTEGER REFERENCES tasks(id),
                amount_kop INTEGER NOT NULL,
                kind TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(task_id,kind)
            );
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY,
                actor_id INTEGER,
                action TEXT NOT NULL,
                object_type TEXT NOT NULL,
                object_id INTEGER,
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS task_criteria(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                title TEXT NOT NULL,
                weight INTEGER NOT NULL DEFAULT 0 CHECK(weight BETWEEN 0 AND 100),
                critical INTEGER NOT NULL DEFAULT 0,
                position INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS criterion_results(
                id INTEGER PRIMARY KEY,
                submission_id INTEGER NOT NULL REFERENCES submissions(id),
                criterion_id INTEGER NOT NULL REFERENCES task_criteria(id),
                result TEXT NOT NULL CHECK(result IN ('done','partial','violated','na')),
                comment TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '',
                UNIQUE(submission_id,criterion_id)
            );
            CREATE TABLE IF NOT EXISTS disputes(
                id INTEGER PRIMARY KEY,
                task_id INTEGER NOT NULL REFERENCES tasks(id),
                submission_id INTEGER REFERENCES submissions(id),
                criterion_id INTEGER REFERENCES task_criteria(id),
                user_id INTEGER NOT NULL REFERENCES users(id),
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                admin_decision TEXT NOT NULL DEFAULT '',
                admin_comment TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS payouts(
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id),
                period_start TEXT NOT NULL,
                period_end TEXT NOT NULL,
                amount_kop INTEGER NOT NULL CHECK(amount_kop>=0),
                status TEXT NOT NULL DEFAULT 'prepared',
                paid_at TEXT,
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id,period_start,period_end)
            );
            """
        )
        # Additive migrations preserve the existing MVP database and its balances.
        _add_column(conn, "users", "photo_url TEXT")
        for definition in (
            "project_id INTEGER REFERENCES projects(id)",
            "stage_id INTEGER REFERENCES stages(id)",
            "tour_id INTEGER REFERENCES tours(id)",
            "pricing_model TEXT NOT NULL DEFAULT 'fixed'",
            "norm_minutes INTEGER NOT NULL DEFAULT 0",
            "difficulty REAL NOT NULL DEFAULT 1.0",
            "urgency REAL NOT NULL DEFAULT 1.0",
            "quality_factor REAL NOT NULL DEFAULT 1.0",
            "fixed_reward_kop INTEGER NOT NULL DEFAULT 0",
            "criteria_frozen INTEGER NOT NULL DEFAULT 0",
            "budget_reserved_kop INTEGER NOT NULL DEFAULT 0",
            "moderation_status TEXT NOT NULL DEFAULT 'approved'",
            "moderation_comment TEXT NOT NULL DEFAULT ''",
            "moderated_by INTEGER REFERENCES users(id)",
            "moderated_at TEXT",
        ):
            _add_column(conn, "tasks", definition)
        conn.execute(
            "UPDATE tasks SET fixed_reward_kop=reward_kop WHERE fixed_reward_kop=0 AND reward_kop>0"
        )
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id,stage_id,tour_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id,id);
            CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
            """
        )
