"""SQLite tables (requirements.md §6). Timestamps are epoch seconds (UTC)."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Signal(Base):
    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)
    price: Mapped[float] = mapped_column(Float)
    snapshot_json: Mapped[str] = mapped_column(Text)
    rule_values_json: Mapped[str] = mapped_column(Text)
    suggestion_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    high_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_advice_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_action: Mapped[str] = mapped_column(String(10), default="none")  # none|entered|ignored
    alert_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    entry_time: Mapped[int] = mapped_column(Integer)
    entry_price: Mapped[float] = mapped_column(Float)
    amount_usdt: Mapped[float] = mapped_column(Float)
    quantity: Mapped[float] = mapped_column(Float)
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    targets_hit: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of target pcts
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|closed
    exit_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    realised_pnl_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    realised_pnl_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    linked_signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("signals.id"), nullable=True
    )
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True)


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
