import sqlite3
import time

from bot.health import check, write_heartbeat
from bot.storage.backup import backup, prune

# --- heartbeat / healthcheck ---------------------------------------------------------------


def test_fresh_heartbeat_is_healthy(tmp_path):
    path = tmp_path / "heartbeat"
    write_heartbeat(path, stream_stale=False, now=1000)
    ok, message = check(path, now=1030)
    assert ok and "30s ago" in message


def test_old_missing_or_stale_heartbeat_is_unhealthy(tmp_path):
    path = tmp_path / "heartbeat"
    assert check(path)[0] is False  # missing
    write_heartbeat(path, stream_stale=False, now=1000)
    ok, message = check(path, now=1000 + 181)
    assert not ok and "181s old" in message
    write_heartbeat(path, stream_stale=True, now=2000)
    ok, message = check(path, now=2005)
    assert not ok and "no price data" in message
    path.write_text("garbage")
    assert check(path)[0] is False


# --- backups -------------------------------------------------------------------------------


def make_db(path, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("create table signals (id integer primary key, symbol text)")
    conn.executemany("insert into signals (symbol) values (?)", [("SOLUSDT",)] * rows)
    conn.commit()
    return conn


def test_backup_of_open_database_is_a_valid_copy(tmp_path):
    db = tmp_path / "bot.db"
    live = make_db(db)  # still open, like the running bot
    live.execute("insert into signals (symbol) values ('LINKUSDT')")
    live.commit()
    target = backup(db, tmp_path / "backups", now=1_790_586_003)
    assert target.name == "bot-20260928-0900.db"
    copy = sqlite3.connect(target)
    assert copy.execute("select count(*) from signals").fetchone()[0] == 4
    assert copy.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert not list((tmp_path / "backups").glob("*.tmp"))
    live.close()


def test_prune_keeps_recent_backups(tmp_path):
    import os

    old, recent = tmp_path / "bot-old.db", tmp_path / "bot-new.db"
    for p in (old, recent):
        p.write_bytes(b"x")
    now = time.time()
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    removed = prune(tmp_path, keep_days=14, now=now)
    assert removed == [old] and recent.exists() and not old.exists()


def test_backup_missing_database(tmp_path):
    import pytest

    with pytest.raises(FileNotFoundError):
        backup(tmp_path / "nope.db", tmp_path / "backups")
