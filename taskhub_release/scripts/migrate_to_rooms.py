#!/usr/bin/env python3
"""Explicit, backup-gated migration from the single-workspace MVP to rooms.

This script is intentionally NOT called by app startup. First run --check on a
verified backup, then --apply against a copied database. Production application
must be stopped for the final cutover.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4
REQUIRED_TABLES = {
    "users", "projects", "stages", "tours", "tasks", "submissions",
    "ledger", "audit", "task_criteria", "criterion_results", "disputes", "payouts",
}


def table_names(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )}


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}


def verify_database(conn: sqlite3.Connection) -> None:
    missing = REQUIRED_TABLES - table_names(conn)
    if missing:
        raise RuntimeError(f"Not a TaskHub MVP database; missing tables: {sorted(missing)}")
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity}")
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise RuntimeError(f"Foreign-key violations exist: {len(fk)}")


def create_schema(conn: sqlite3.Connection) -> None:
    schema_sql = """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        owner_id INTEGER NOT NULL REFERENCES users(id),
        payout_day INTEGER NOT NULL DEFAULT 15 CHECK(payout_day BETWEEN 1 AND 28),
        payout_calendar_region TEXT NOT NULL DEFAULT 'RU',
        moderation_enabled INTEGER NOT NULL DEFAULT 1 CHECK(moderation_enabled IN (0,1)),
        budget_limit_kop INTEGER CHECK(budget_limit_kop IS NULL OR budget_limit_kop>=0),
        budget_reserved_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_reserved_kop>=0),
        budget_spent_kop INTEGER NOT NULL DEFAULT 0 CHECK(budget_spent_kop>=0),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        archived_at TEXT
    );

    CREATE TABLE IF NOT EXISTS room_members (
        id INTEGER PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id),
        user_id INTEGER NOT NULL REFERENCES users(id),
        room_role TEXT NOT NULL DEFAULT 'member'
            CHECK(room_role IN ('owner','admin','member')),
        work_mode TEXT
            CHECK(work_mode IS NULL OR work_mode IN ('customer','executor')),
        joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        left_at TEXT,
        UNIQUE(room_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS room_invites (
        id INTEGER PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id),
        token_hash TEXT NOT NULL UNIQUE,
        created_by INTEGER NOT NULL REFERENCES users(id),
        expires_at TEXT,
        max_uses INTEGER,
        uses_count INTEGER NOT NULL DEFAULT 0 CHECK(uses_count>=0),
        revoked_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS room_payout_settings (
        room_id INTEGER PRIMARY KEY REFERENCES rooms(id),
        payout_day INTEGER NOT NULL DEFAULT 15 CHECK(payout_day BETWEEN 1 AND 28),
        calendar_region TEXT NOT NULL DEFAULT 'RU',
        updated_by INTEGER REFERENCES users(id),
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS payout_items (
        id INTEGER PRIMARY KEY,
        payout_id INTEGER NOT NULL REFERENCES payouts(id),
        ledger_id INTEGER NOT NULL REFERENCES ledger(id),
        amount_kop INTEGER NOT NULL,
        UNIQUE(payout_id,ledger_id)
    );

    CREATE TABLE IF NOT EXISTS payout_events (
        id INTEGER PRIMARY KEY,
        payout_id INTEGER NOT NULL REFERENCES payouts(id),
        actor_id INTEGER REFERENCES users(id),
        event_type TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS task_events (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        actor_id INTEGER REFERENCES users(id),
        event_type TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        details TEXT NOT NULL DEFAULT '',
        idempotency_key TEXT UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS budget_events (
        id INTEGER PRIMARY KEY,
        room_id INTEGER NOT NULL REFERENCES rooms(id),
        task_id INTEGER REFERENCES tasks(id),
        actor_id INTEGER REFERENCES users(id),
        event_type TEXT NOT NULL CHECK(event_type IN ('limit_changed','reserved','released','spent')),
        amount_kop INTEGER NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TRIGGER IF NOT EXISTS budget_events_no_update
        BEFORE UPDATE ON budget_events
        BEGIN SELECT RAISE(ABORT, 'budget_events are immutable'); END;

    CREATE TRIGGER IF NOT EXISTS budget_events_no_delete
        BEFORE DELETE ON budget_events
        BEGIN SELECT RAISE(ABORT, 'budget_events are immutable'); END;

    CREATE TABLE IF NOT EXISTS task_deadline_events (
        id INTEGER PRIMARY KEY,
        task_id INTEGER NOT NULL REFERENCES tasks(id),
        event_type TEXT NOT NULL,
        scheduled_at TEXT NOT NULL,
        sent_at TEXT,
        UNIQUE(task_id,event_type,scheduled_at)
    );
    """
    # Keep DDL inside the caller transaction; executescript() auto-commits.
    for statement in schema_sql.split(";"):
        if statement.strip():
            conn.execute(statement)

    # Additive columns retain existing IDs and records. Nullable room_id permits
    # the transaction to create the legacy room and backfill all rows safely.
    additions = {
        "rooms": ("budget_limit_kop INTEGER", "budget_reserved_kop INTEGER NOT NULL DEFAULT 0", "budget_spent_kop INTEGER NOT NULL DEFAULT 0"),
        "projects": ("room_id INTEGER REFERENCES rooms(id)",),
        "stages": ("room_id INTEGER REFERENCES rooms(id)",),
        "tours": ("room_id INTEGER REFERENCES rooms(id)",),
        "tasks": ("room_id INTEGER REFERENCES rooms(id)",),
        "ledger": ("room_id INTEGER REFERENCES rooms(id)",),
        "payouts": (
            "room_id INTEGER REFERENCES rooms(id)",
            "snapshot_version INTEGER NOT NULL DEFAULT 0",
            "legacy_snapshot INTEGER NOT NULL DEFAULT 1",
            "payout_date TEXT",
        ),
    }
    for table, defs in additions.items():
        present = columns(conn, table)
        for definition in defs:
            name = definition.split()[0]
            if name not in present:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {definition}')
                present.add(name)


def migrate(conn: sqlite3.Connection) -> dict:
    verify_database(conn)
    create_schema(conn)
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
    ).fetchone()
    if already:
        existing_room = conn.execute("SELECT id FROM rooms ORDER BY id LIMIT 1").fetchone()
        result = {"status": "already_applied", "version": SCHEMA_VERSION}
        if existing_room:
            result["legacy_room_id"] = existing_room[0]
        return result

    users = conn.execute("SELECT id,role FROM users ORDER BY id").fetchall()
    if not users:
        raise RuntimeError(
            "No users exist. A legacy room owner cannot be inferred; create a room explicitly."
        )

    existing_rooms = conn.execute("SELECT id FROM rooms ORDER BY id LIMIT 1").fetchone()
    if existing_rooms:
        room_id = existing_rooms["id"] if isinstance(existing_rooms, sqlite3.Row) else existing_rooms[0]
    else:
        admins = [u["id"] for u in users if u["role"] == "admin"]
        owner_id = admins[0] if admins else users[0]["id"]
        cur = conn.execute(
            "INSERT INTO rooms(name,owner_id,payout_day,payout_calendar_region) VALUES(?,?,15,'RU')",
            ("Основная комната", owner_id),
        )
        room_id = cur.lastrowid

    # Preserve legacy admin access as room administration. The first legacy
    # admin becomes owner; no work mode is guessed during migration.
    owner_id = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()[0]
    for u in users:
        role = "owner" if u["id"] == owner_id else ("admin" if u["role"] == "admin" else "member")
        conn.execute(
            """INSERT OR IGNORE INTO room_members(room_id,user_id,room_role,work_mode)
               VALUES(?,?,?,NULL)""",
            (room_id, u["id"], role),
        )

    for table in ("projects", "stages", "tours", "tasks", "ledger", "payouts"):
        conn.execute(f'UPDATE "{table}" SET room_id=? WHERE room_id IS NULL', (room_id,))

    conn.execute(
        "INSERT OR IGNORE INTO room_payout_settings(room_id,payout_day,calendar_region) VALUES(?,15,'RU')",
        (room_id,),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_room_members_user ON room_members(user_id,left_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_room ON projects(room_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stages_room ON stages(room_id,project_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_room_status ON tasks(room_id,status,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_room_user ON ledger(room_id,user_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_budget_events_room ON budget_events(room_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payouts_room_period ON payouts(room_id,period_start,period_end)")
    conn.execute(
        "INSERT INTO schema_migrations(version,name) VALUES(?,?)",
        (SCHEMA_VERSION, "introduce_rooms_and_room_scoped_records"),
    )
    return {"status": "migrated", "version": SCHEMA_VERSION, "legacy_room_id": room_id}


def snapshot(conn: sqlite3.Connection) -> dict:
    result = {}
    for table in sorted(REQUIRED_TABLES):
        result[table] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    result["ledger_sum_kop"] = conn.execute(
        "SELECT COALESCE(SUM(amount_kop),0) FROM ledger"
    ).fetchone()[0]
    result["payout_sum_kop"] = conn.execute(
        "SELECT COALESCE(SUM(amount_kop),0) FROM payouts"
    ).fetchone()[0]
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="SQLite database file to inspect/migrate")
    parser.add_argument("--backup", help="Verified backup file required for --apply")
    parser.add_argument("--apply", action="store_true", help="Apply migration to --db")
    parser.add_argument("--check", action="store_true", help="Validate database and print snapshot")
    args = parser.parse_args()

    db_path = Path(args.db).resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")
    if args.apply:
        if not args.backup:
            raise SystemExit("--apply requires --backup pointing to a verified backup")
        backup_path = Path(args.backup).resolve()
        if not backup_path.is_file() or backup_path == db_path:
            raise SystemExit("A separate, existing backup file is required")
        with sqlite3.connect(str(backup_path)) as backup:
            verify_database(backup)
        # Refuse to modify the supplied database unless it matches the backup's
        # pre-migration row counts and financial totals.
        with sqlite3.connect(str(db_path)) as target:
            verify_database(target)
            with sqlite3.connect(str(backup_path)) as backup:
                if snapshot(target) != snapshot(backup):
                    raise SystemExit("Target DB differs from verified backup; refusing migration")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_copy = db_path.with_name(db_path.name + f".pre-rooms-{stamp}.copy")
        source = sqlite3.connect(str(db_path), timeout=30)
        safety = sqlite3.connect(str(safety_copy), timeout=30)
        try:
            source.backup(safety)
            safety.commit()
        finally:
            safety.close()
            source.close()
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = snapshot(conn)
            outcome = migrate(conn)
            after = snapshot(conn)
            if before != after:
                raise RuntimeError(f"Legacy row/financial snapshot changed: {before} != {after}")
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if integrity != "ok" or fk_issues:
                raise RuntimeError(
                    f"Post-migration validation failed: integrity={integrity}, foreign_keys={len(fk_issues)}"
                )
            conn.commit()
            print(json.dumps({
                "result": outcome,
                "pre_migration_snapshot": before,
                "post_migration_snapshot": after,
                "safety_copy": str(safety_copy),
                "integrity_check": integrity,
            }, ensure_ascii=False, indent=2))
        except Exception:
            conn.rollback()
            conn.close()
            raise
        conn.close()
        return 0

    if args.check:
        with sqlite3.connect(str(db_path)) as conn:
            verify_database(conn)
            print(json.dumps({"status": "valid", "snapshot": snapshot(conn)}, ensure_ascii=False, indent=2))
        return 0

    parser.error("Choose --check or --apply")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
