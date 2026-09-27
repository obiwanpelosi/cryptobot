"""Database setup and the small set of queries the bot needs."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from bot.storage.models import Base, BotState, Position, Signal

DEFAULT_DB_PATH = Path("data") / "bot.db"


def make_engine(path: str | Path | None = DEFAULT_DB_PATH) -> Engine:
    """SQLite engine; `path=None` gives an in-memory database (tests)."""
    if path is None:
        # One shared connection, otherwise every session sees a fresh empty database.
        engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine


class Repo:
    def __init__(self, engine: Engine):
        self._sessions = sessionmaker(engine, expire_on_commit=False)

    def _session(self) -> Session:
        return self._sessions()

    # --- signals ---------------------------------------------------------------------

    def add_signal(self, signal: Signal) -> Signal:
        with self._session() as s, s.begin():
            s.add(signal)
        return signal

    def get_signal(self, signal_id: int) -> Signal | None:
        with self._session() as s:
            return s.get(Signal, signal_id)

    def last_signal_time(self, symbol: str) -> int | None:
        with self._session() as s:
            return s.scalar(select(func.max(Signal.ts)).where(Signal.symbol == symbol))

    def set_user_action(self, signal_id: int, action: str) -> bool:
        """Set the action only if none was recorded yet. Returns False if already set."""
        with self._session() as s, s.begin():
            signal = s.get(Signal, signal_id)
            if signal is None or signal.user_action != "none":
                return False
            signal.user_action = action
            return True

    def mark_alert_sent(self, signal_id: int) -> None:
        with self._session() as s, s.begin():
            signal = s.get(Signal, signal_id)
            if signal is not None:
                signal.alert_sent = True

    # --- positions -------------------------------------------------------------------

    def add_position(self, position: Position) -> Position:
        with self._session() as s, s.begin():
            s.add(position)
        return position

    def open_positions(self) -> list[Position]:
        with self._session() as s:
            return list(s.scalars(select(Position).where(Position.status == "open")))

    # --- key/value state -------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._session() as s:
            row = s.get(BotState, key)
            return row.value if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._session() as s, s.begin():
            s.merge(BotState(key=key, value=value))
