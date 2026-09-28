import sqlite3

from bot.storage.db import Repo, make_engine

# The positions table as shipped in Phase 4 (no highest_price / trailing, no alerts_sent).
PHASE4_POSITIONS = """
CREATE TABLE positions (
    id INTEGER PRIMARY KEY, symbol VARCHAR(20) NOT NULL, entry_time INTEGER NOT NULL,
    entry_price FLOAT NOT NULL, amount_usdt FLOAT NOT NULL, quantity FLOAT NOT NULL,
    stop_price FLOAT, targets_hit TEXT NOT NULL, status VARCHAR(10) NOT NULL,
    exit_time INTEGER, exit_price FLOAT, realised_pnl_usdt FLOAT, realised_pnl_pct FLOAT,
    fees_usdt FLOAT NOT NULL, linked_signal_id INTEGER, is_paper BOOLEAN NOT NULL
)
"""


def test_old_database_upgraded_in_place(tmp_path):
    path = tmp_path / "bot.db"
    conn = sqlite3.connect(path)
    conn.execute(PHASE4_POSITIONS)
    conn.execute(
        "INSERT INTO positions (id, symbol, entry_time, entry_price, amount_usdt, quantity,"
        " stop_price, targets_hit, status, fees_usdt, is_paper)"
        " VALUES (7, 'SOLUSDT', 1, 121.5, 59.6, 0.49, 117.3, '[]', 'open', 0.06, 1)"
    )
    conn.commit()
    conn.close()

    repo = Repo(make_engine(path))
    (position,) = repo.open_positions()
    assert position.id == 7 and position.entry_price == 121.5
    assert position.trailing is False and position.highest_price is None
    repo.record_alerts(7, ["target:10"])
    assert repo.sent_alert_keys([7]) == {7: {"target:10"}}

    make_engine(path)  # running the migration again is a no-op
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(positions)")}
    assert {"highest_price", "trailing"} <= columns


def test_record_alerts_ignores_duplicates():
    repo = Repo(make_engine(None))
    repo.record_alerts(1, ["a", "b"])
    repo.record_alerts(1, ["a", "c"])
    assert repo.sent_alert_keys([1, 2]) == {1: {"a", "b", "c"}, 2: set()}


def test_close_position_is_guarded():
    from tests.test_tracker import add_position

    repo = Repo(make_engine(None))
    p = add_position(repo)
    assert repo.close_position(p.id, exit_price=110.0) is True
    assert repo.close_position(p.id, exit_price=120.0) is False
    assert repo.get_position(p.id).exit_price == 110.0
    assert [x.id for x in repo.closed_positions()] == [p.id]
