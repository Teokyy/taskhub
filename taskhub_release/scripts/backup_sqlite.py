#!/usr/bin/env python3
"""Create and verify a consistent SQLite backup of the configured TaskHub database.

Run from the taskhub_release directory:
    python scripts/backup_sqlite.py

The SQLite backup API includes committed WAL content. The script never modifies
or truncates the source database.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

from app.db import DB_PATH  # noqa: E402


def main() -> int:
    source_path = Path(DB_PATH).resolve()
    if not source_path.is_file():
        raise SystemExit(f"Source database does not exist: {source_path}")

    backup_dir = Path(os.getenv("BACKUP_DIR", str(source_path.parent / "backups"))).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target_path = backup_dir / f"taskhub-pre-rooms-{timestamp}.sqlite3"
    if target_path.exists():
        raise SystemExit(f"Refusing to overwrite existing backup: {target_path}")

    source = sqlite3.connect(str(source_path), timeout=30)
    target = sqlite3.connect(str(target_path), timeout=30)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_issues = target.execute("PRAGMA foreign_key_check").fetchall()
        target.commit()
    except Exception:
        target.close()
        source.close()
        target_path.unlink(missing_ok=True)
        raise
    finally:
        try:
            target.close()
        except Exception:
            pass
        source.close()

    os.chmod(target_path, 0o600)
    size_bytes = target_path.stat().st_size
    result = {
        "source": str(source_path),
        "backup": str(target_path),
        "size_bytes": size_bytes,
        "integrity_check": integrity,
        "foreign_key_violations": len(foreign_key_issues),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if integrity != "ok":
        raise SystemExit("Backup integrity check failed; do not proceed with migration.")
    if foreign_key_issues:
        print("WARNING: existing foreign-key violations were found in the backup.")
        for issue in foreign_key_issues[:20]:
            print(tuple(issue))
        raise SystemExit("Foreign-key check failed; investigate before migration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
