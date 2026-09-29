"""SQLite tables (requirements.md §6). Timestamps are epoch seconds (UTC)."""

from __future__ import annotations

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
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
    highest_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    trailing: Mapped[bool] = mapped_column(Boolean, default=False)


class AlertSent(Base):
    """One row per alert actually delivered; the unique key makes every alert fire once."""

    __tablename__ = "alerts_sent"
    __table_args__ = (UniqueConstraint("position_id", "alert_type"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    alert_type: Mapped[str] = mapped_column(String(40))
    ts: Mapped[int] = mapped_column(Integer)


class AICall(Base):
    """Every AI request, successful or not (requirements.md §5.9)."""

    __tablename__ = "ai_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(100))  # requested
    served_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    purpose: Mapped[str] = mapped_column(String(30))  # entry | exit | analysis | experiment
    role: Mapped[str] = mapped_column(String(10), default="primary")  # primary | shadow
    prompt: Mapped[str] = mapped_column(Text)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_json: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
    position_id: Mapped[int | None] = mapped_column(ForeignKey("positions.id"), nullable=True)


class SignalOutcome(Base):
    """What the price did after a signal (requirements.md §5.10, §6)."""

    __tablename__ = "signal_outcomes"
    __table_args__ = (UniqueConstraint("signal_id", "horizon"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(5))  # 24h | 72h | 7d
    return_pct: Mapped[float] = mapped_column(Float)  # net of both fees
    max_favourable_pct: Mapped[float] = mapped_column(Float)
    max_adverse_pct: Mapped[float] = mapped_column(Float)
    first_hit: Mapped[str] = mapped_column(String(10))  # target | stop | none
    stop_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss_pct: Mapped[float | None] = mapped_column(Float, nullable=True)  # net, at stop
    evaluated_at: Mapped[int] = mapped_column(Integer)


class BotState(Base):
    __tablename__ = "bot_state"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
