from bot.signals.engine import alerts_paused, set_alerts_paused
from bot.storage.db import Repo, make_engine
from bot.storage.models import Position, Signal


def make_repo():
    return Repo(make_engine(None))


def sig(symbol="SOLUSDT", ts=100):
    return Signal(
        symbol=symbol,
        ts=ts,
        price=100.0,
        snapshot_json="{}",
        rule_values_json="[]",
        high_risk=False,
    )


def test_signal_round_trip_and_last_time():
    repo = make_repo()
    assert repo.last_signal_time("SOLUSDT") is None
    first = repo.add_signal(sig(ts=100))
    repo.add_signal(sig(ts=250))
    repo.add_signal(sig(symbol="LINKUSDT", ts=999))
    assert first.id is not None
    got = repo.get_signal(first.id)
    assert got.symbol == "SOLUSDT" and got.user_action == "none" and not got.alert_sent
    assert repo.last_signal_time("SOLUSDT") == 250


def test_user_action_set_once():
    repo = make_repo()
    s = repo.add_signal(sig())
    assert repo.set_user_action(s.id, "ignored") is True
    assert repo.set_user_action(s.id, "entered") is False
    assert repo.get_signal(s.id).user_action == "ignored"
    assert repo.set_user_action(12345, "ignored") is False


def test_mark_alert_sent():
    repo = make_repo()
    s = repo.add_signal(sig())
    repo.mark_alert_sent(s.id)
    assert repo.get_signal(s.id).alert_sent


def test_position_insert_and_open_list():
    repo = make_repo()
    s = repo.add_signal(sig())
    p = repo.add_position(
        Position(
            symbol="SOLUSDT",
            entry_time=1,
            entry_price=100.0,
            amount_usdt=50.0,
            quantity=0.4995,
            stop_price=96.0,
            fees_usdt=0.05,
            linked_signal_id=s.id,
            is_paper=True,
        )
    )
    (open_p,) = repo.open_positions()
    assert open_p.id == p.id and open_p.status == "open" and open_p.targets_hit == "[]"


def test_state_persists_and_paused_helpers(tmp_path):
    engine = make_engine(tmp_path / "bot.db")
    repo = Repo(engine)
    assert not alerts_paused(repo)
    set_alerts_paused(repo, True)
    assert Repo(make_engine(tmp_path / "bot.db")).get_state("alerts_paused") == "1"
    set_alerts_paused(repo, False)
    assert not alerts_paused(repo)
