"""Database setup and the small set of queries the bot needs."""

from __future__ import annotations

import time
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from bot.storage.models import AICall, AlertSent, Base, BotState, Position, Signal

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
    _migrate(engine)
    return engine


# Columns added after a table first shipped: {table: {column: DDL type/default}}.
ADDED_COLUMNS = {
    "positions": {
        "highest_price": "FLOAT",
        "trailing": "BOOLEAN NOT NULL DEFAULT 0",
    },
}


def _migrate(engine: Engine) -> None:
    """Add missing columns to existing tables (create_all only creates missing tables)."""
    with engine.begin() as conn:
        for table, columns in ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


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

    def set_signal_ai_advice(self, signal_id: int, advice_json: str) -> None:
        with self._session() as s, s.begin():
            signal = s.get(Signal, signal_id)
            if signal is not None:
                signal.ai_advice_json = advice_json

    def recent_signals(self, limit: int = 10) -> list[Signal]:
        with self._session() as s:
            query = select(Signal).order_by(Signal.ts.desc(), Signal.id.desc()).limit(limit)
            return list(s.scalars(query))

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

    def get_position(self, position_id: int) -> Position | None:
        with self._session() as s:
            return s.get(Position, position_id)

    def update_position(self, position_id: int, **fields) -> None:
        with self._session() as s, s.begin():
            s.execute(update(Position).where(Position.id == position_id).values(**fields))

    def close_position(self, position_id: int, **fields) -> bool:
        """Close only if still open. Returns False if it was already closed (or missing)."""
        with self._session() as s, s.begin():
            result = s.execute(
                update(Position)
                .where(Position.id == position_id, Position.status == "open")
                .values(status="closed", **fields)
            )
            return result.rowcount == 1

    def closed_positions(self, limit: int = 20) -> list[Position]:
        with self._session() as s:
            query = (
                select(Position)
                .where(Position.status == "closed")
                .order_by(Position.exit_time.desc(), Position.id.desc())
                .limit(limit)
            )
            return list(s.scalars(query))

    def recent_closed_positions(self, symbol: str, limit: int = 20) -> list[Position]:
        with self._session() as s:
            query = (
                select(Position)
                .where(Position.status == "closed", Position.symbol == symbol)
                .order_by(Position.exit_time.desc(), Position.id.desc())
                .limit(limit)
            )
            return list(s.scalars(query))

    # --- ai_calls --------------------------------------------------------------------

    def add_ai_call(self, call: AICall) -> AICall:
        with self._session() as s, s.begin():
            s.add(call)
        return call

    def ai_calls_since(self, ts: int, *, exclude_purpose: str | None = "experiment") -> int:
        """Calls counted against the hourly limit (experiments are run by hand, not limited)."""
        with self._session() as s:
            query = select(func.count(AICall.id)).where(AICall.ts >= ts)
            if exclude_purpose:
                query = query.where(AICall.purpose != exclude_purpose)
            return s.scalar(query) or 0

    def ai_spend_since(self, ts: int) -> float:
        with self._session() as s:
            total = s.scalar(select(func.sum(AICall.cost_usd)).where(AICall.ts >= ts))
            return float(total or 0.0)

    # --- alerts_sent -----------------------------------------------------------------

    def sent_alert_keys(self, position_ids: Iterable[int]) -> dict[int, set[str]]:
        ids = list(position_ids)
        out: dict[int, set[str]] = {pid: set() for pid in ids}
        if not ids:
            return out
        with self._session() as s:
            rows = s.execute(
                select(AlertSent.position_id, AlertSent.alert_type).where(
                    AlertSent.position_id.in_(ids)
                )
            )
            for pid, key in rows:
                out[pid].add(key)
        return out

    def record_alerts(self, position_id: int, keys: Iterable[str]) -> None:
        now = int(time.time())
        for key in keys:
            try:
                with self._session() as s, s.begin():
                    s.add(AlertSent(position_id=position_id, alert_type=key, ts=now))
            except IntegrityError:
                pass  # already recorded

    # --- key/value state -------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._session() as s:
            row = s.get(BotState, key)
            return row.value if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._session() as s, s.begin():
            s.merge(BotState(key=key, value=value))
