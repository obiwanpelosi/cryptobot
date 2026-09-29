"""Safe SQLite backup, run while the bot is live.

    python -m bot.storage.backup            # data/bot.db -> data/backups/bot-YYYYMMDD-HHMM.db
    python -m bot.storage.backup --keep-days 30

Uses SQLite's online backup API (safe during writes), verifies the copy with
PRAGMA integrity_check, and deletes backups older than --keep-days (default 14).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from bot.settings import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "data" / "bot.db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
KEEP_DAYS = 14


def backup(
    db_path: Path = DB_PATH, backup_dir: Path = BACKUP_DIR, *, now: float | None = None
) -> Path:
    now = time.time() if now is None else now
    if not db_path.exists():
        raise FileNotFoundError(f"No database at {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"bot-{time.strftime('%Y%m%d-%H%M', time.gmtime(now))}.db"
    tmp = target.with_suffix(".db.tmp")
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(tmp)
        try:
            source.backup(dest)
            result = dest.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dest.close()
    finally:
        source.close()
    if result != "ok":
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed integrity check: {result}")
    tmp.replace(target)
    return target


def prune(backup_dir: Path = BACKUP_DIR, *, keep_days: int = KEEP_DAYS, now: float | None = None):
    """Delete backups older than keep_days. Returns the deleted paths."""
    now = time.time() if now is None else now
    cutoff = now - keep_days * 86400
    removed = []
    for path in sorted(backup_dir.glob("bot-*.db")):
        if path.stat().st_mtime < cutoff:
            path.unlink()
            removed.append(path)
    return removed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    args = parser.parse_args(argv)
    target = backup()
    removed = prune(keep_days=args.keep_days)
    size_kb = target.stat().st_size / 1024
    print(
        f"Backup OK: {target.relative_to(PROJECT_ROOT)} ({size_kb:.0f} KB), pruned {len(removed)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
