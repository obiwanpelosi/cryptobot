"""Missed-dip analysis: replay the dip rules over price history (no API key needed).

    uv run python -m bot.evaluation.missed_dips                 # last 90 days, SOL and LINK
    uv run python -m bot.evaluation.missed_dips --days 180
    uv run python -m bot.evaluation.missed_dips --db backups/bot-20261012-0330.db --days 14

Every 15-minute close is evaluated exactly like the live bot (closed candles only, same
rounding, same cooldown). Dips the current rules alert on are labelled "alerted"; dips just
outside them are "near misses". Every dip is scored like Phase 7 scores real alerts: entry at
that close, stop at entry - ATR(4h) x multiplier, first profit target after fees, and whether
the target or the stop is hit first within 7 days. A grid shows what other thresholds would
have done over the same period.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sqlite3
import sys
import time
from bisect import bisect_left
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from bot.evaluation.history import cached_klines
from bot.evaluation.outcomes import covers, evaluate_path, net_pct, window
from bot.exchange.models import INTERVAL_MS, Candle
from bot.market import indicators as ind
from bot.settings import PROJECT_ROOT, DipRules, StrategyConfig, load_settings
from bot.signals.sizing import fee_fraction, target_price

REPORT_DIR = PROJECT_ROOT / "data" / "experiments"
MAX_DAYS = 3 * 365
HOLD_SECONDS = 7 * 24 * 3600
WARMUP_CANDLES = 250
GRID_DROPS = [3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
GRID_RSIS = [30.0, 35.0, 40.0, 45.0]
LABELS = {
    "alerted": "Alerted (current rules)",
    "drop_too_small": "Near miss: drop too small",
    "rsi_too_high": "Near miss: RSI too high",
    "both": "Near miss: both just outside",
    "bollinger": "Near miss: not below lower 4h band",
    "trend": "Filtered: against the bigger trend",
}


# --- per-close rule values (what the live snapshot would have shown) ------------------------


def _frame(candles: list[Candle], interval: str) -> pd.DataFrame:
    df = pd.DataFrame(
        {
            "open_time": [c.open_time_ms for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
        }
    )
    df["avail"] = df["open_time"] + INTERVAL_MS[interval]  # when the closed candle is usable
    return df


def _daily_emas(candles: list[Candle], prefix: str) -> tuple[pd.DataFrame, list[str]]:
    d = _frame(candles, "1d")
    cols = []
    for n in (20, 50, 200):
        col = f"{prefix}_ema{n}_1d"
        d[col] = ind.ema(d["close"], n)
        cols.append(col)
    return d, cols


def _round_sig(series: pd.Series, digits: int) -> pd.Series:
    return series.map(lambda v: ind.round_sig(v, digits) if pd.notna(v) else np.nan)


def rule_series(
    c15: list[Candle],
    c1h: list[Candle],
    c4h: list[Candle],
    btc15: list[Candle],
    *,
    lookback_hours: int,
    c1d: list[Candle] | None = None,
    btc1d: list[Candle] | None = None,
) -> pd.DataFrame:
    """Rule inputs at every 15m close `t`, using only candles closed by `t` (no look-ahead).

    With daily candles, also adds the coin's and BTC's daily EMA 20/50/200 (for trend filters).
    """
    base = _frame(c15, "15m").rename(columns={"close": "price"})
    base = base[["avail", "price"]].rename(columns={"avail": "t"}).sort_values("t")

    h = _frame(c1h, "1h")
    h["rsi_1h"] = ind.rsi(h["close"])
    h["high_lookback"] = h["high"].rolling(lookback_hours, min_periods=1).max()

    f = _frame(c4h, "4h")
    _, upper, lower = ind.bollinger(f["close"])
    f["bb_upper"], f["bb_lower"] = upper, lower
    f["atr_4h"] = ind.atr(f["high"], f["low"], f["close"])

    b = _frame(btc15, "15m")
    b["btc_change_1h_pct"] = (b["close"] / b["close"].shift(4) - 1) * 100
    b["btc_price"] = b["close"]

    merges = [
        (h, ["rsi_1h", "high_lookback"]),
        (f, ["bb_upper", "bb_lower", "atr_4h"]),
        (b, ["btc_change_1h_pct", "btc_price"]),
    ]
    for daily, prefix in ((c1d, "coin"), (btc1d, "btc")):
        if daily:
            merges.append(_daily_emas(daily, prefix))

    out = base
    for right, cols in merges:
        out = pd.merge_asof(
            out, right[["avail", *cols]].sort_values("avail"), left_on="t", right_on="avail"
        ).drop(columns="avail")

    recent_high = np.maximum(out["high_lookback"], out["price"])
    out["drop_pct"] = (recent_high - out["price"]) / recent_high * 100
    width = out["bb_upper"] - out["bb_lower"]
    out["pct_b_4h"] = (out["price"] - out["bb_lower"]) / width.where(width != 0)
    # Same rounding as the live snapshot (bot/market/snapshot.py, indicators.derive).
    out["drop_pct"] = _round_sig(out["drop_pct"], 3)
    out["rsi_1h"] = _round_sig(out["rsi_1h"], 4)
    out["pct_b_4h"] = _round_sig(out["pct_b_4h"], 3)
    out["atr_4h"] = _round_sig(out["atr_4h"], 4)
    for col in [c for c in out.columns if c.endswith("_1d")]:
        out[col] = _round_sig(out[col], 6)  # same as the live snapshot's EMA values
    return out.drop(columns=["high_lookback"]).reset_index(drop=True)


# --- which closes pass, and grouping into dips -----------------------------------------------


def passes(series: pd.DataFrame, rules: DipRules, *, drop=None, rsi=None) -> pd.Series:
    """Rule mask; missing values never pass (as live). drop/rsi override the config thresholds."""
    drop = rules.min_drop_from_high_pct if drop is None else drop
    rsi = rules.rsi_1h_max if rsi is None else rsi
    mask = pd.Series(True, index=series.index)
    if drop is not None:
        mask &= series["drop_pct"] >= drop
    if rsi is not None:
        mask &= series["rsi_1h"] <= rsi
    if rules.require_lower_bollinger_4h:
        mask &= series["pct_b_4h"] <= 0
    return mask.fillna(False).astype(bool) & trend_ok(series, rules)


def trend_ok(series: pd.DataFrame, rules: DipRules) -> pd.Series:
    """The trend-filter rules (same as bot/signals/rules.py); missing data never passes."""
    mask = pd.Series(True, index=series.index)
    if rules.require_coin_above_ema200_1d:
        mask &= _gt(series["price"], series.get("coin_ema200_1d", pd.Series(index=series.index)))
    if rules.require_btc_above_ema50_1d:
        mask &= _gt(
            series.get("btc_price", pd.Series(index=series.index)),
            series.get("btc_ema50_1d", pd.Series(index=series.index)),
        )
    return mask


def find_dips(times: pd.Series, mask: pd.Series, cooldown_minutes: int) -> list[int]:
    """Indices where an alert would fire: mask true, and the cooldown since the last has passed."""
    cooldown_ms = cooldown_minutes * 60_000
    picked: list[int] = []
    last = None
    for idx in np.flatnonzero(mask.to_numpy()):
        t = int(times.iloc[idx])
        if last is None or t - last >= cooldown_ms:
            picked.append(int(idx))
            last = t
    return picked


def near_miss_dips(
    series: pd.DataFrame,
    rules: DipRules,
    alerted: list[int],
    *,
    near_drop: float,
    near_rsi: float,
    cooldown_minutes: int,
) -> list[int]:
    """Dips just outside the rules, excluding the run-up to (or tail of) an actual alert."""
    near = (series["drop_pct"] >= near_drop) & (series["rsi_1h"] <= near_rsi)
    if rules.require_lower_bollinger_4h:
        near |= passes(series, rules.model_copy(update={"require_lower_bollinger_4h": False}))
    near = near.fillna(False).astype(bool) & ~passes(series, rules)
    alert_times = series["t"].iloc[alerted].to_numpy() if alerted else np.array([])
    cooldown_ms = cooldown_minutes * 60_000
    out = []
    for idx in find_dips(series["t"], near, cooldown_minutes):
        t = series["t"].iloc[idx]
        if alert_times.size and np.min(np.abs(alert_times - t)) < cooldown_ms:
            continue
        out.append(idx)
    return out


def classify(row: pd.Series, rules: DipRules) -> str:
    drop_ok = (
        rules.min_drop_from_high_pct is None or row["drop_pct"] >= rules.min_drop_from_high_pct
    )
    rsi_ok = rules.rsi_1h_max is None or row["rsi_1h"] <= rules.rsi_1h_max
    bb_ok = not rules.require_lower_bollinger_4h or row["pct_b_4h"] <= 0
    if drop_ok and rsi_ok and bb_ok:
        return "alerted" if bool(trend_ok(row.to_frame().T, rules).iloc[0]) else "trend"
    if drop_ok and rsi_ok:
        return "bollinger"
    if drop_ok:
        return "rsi_too_high"
    if rsi_ok:
        return "drop_too_small"
    return "both"


# --- scoring ----------------------------------------------------------------------------------


@dataclass
class Dip:
    symbol: str
    time_utc: str
    t_ms: int
    price: float
    drop_pct: float
    rsi_1h: float
    pct_b_4h: float | None
    btc_change_1h_pct: float | None
    high_risk: bool
    label: str
    stop: float | None = None
    target: float | None = None
    first_hit: str | None = None  # target | stop | none | pending
    result_pct: float | None = None  # net trade result (target / stop / 7d close)
    best_pct: float | None = None
    worst_pct: float | None = None


def score(
    dip: Dip,
    atr_4h: float,
    path_candles: list[Candle],
    strategy: StrategyConfig,
    open_times: list[int] | None = None,
) -> Dip:
    fee = float(fee_fraction(strategy.fees))
    first_pct = min(strategy.exit_alerts.profit_targets_pct)
    if not atr_4h or math.isnan(atr_4h):
        dip.first_hit = "pending"
        return dip
    dip.stop = dip.price - atr_4h * strategy.exit_alerts.stop_atr_multiplier
    dip.target = float(
        target_price(Decimal(str(dip.price)), Decimal(str(first_pct)), Decimal(str(fee)))
    )
    ts = dip.t_ms // 1000
    if open_times is not None:  # slice the 7 days after the dip instead of scanning everything
        lo = bisect_left(open_times, dip.t_ms - 60_000)
        hi = bisect_left(open_times, dip.t_ms + HOLD_SECONDS * 1000)
        path_candles = path_candles[lo:hi]
    path = window(path_candles, ts, HOLD_SECONDS)
    if dip.stop <= 0 or not covers(path, ts, HOLD_SECONDS):
        dip.first_hit = "pending"  # not 7 days of data yet
        return dip
    outcome = evaluate_path(dip.price, dip.stop, dip.target, path, fee)
    dip.first_hit = outcome.first_hit
    dip.best_pct = outcome.max_favourable_pct
    dip.worst_pct = outcome.max_adverse_pct
    if outcome.first_hit == "target":
        dip.result_pct = first_pct
    elif outcome.first_hit == "stop":
        dip.result_pct = net_pct(dip.price, dip.stop, fee)
    else:
        dip.result_pct = outcome.return_pct
    return dip


def make_dip(symbol: str, series: pd.DataFrame, idx: int, rules: DipRules) -> Dip:
    row = series.iloc[idx]
    btc = row["btc_change_1h_pct"]
    guard = rules.btc_crash_guard_pct_1h
    return Dip(
        symbol=symbol,
        time_utc=time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(row["t"]) / 1000)),
        t_ms=int(row["t"]),
        price=float(row["price"]),
        drop_pct=float(row["drop_pct"]),
        rsi_1h=float(row["rsi_1h"]),
        pct_b_4h=None if pd.isna(row["pct_b_4h"]) else float(row["pct_b_4h"]),
        btc_change_1h_pct=None if pd.isna(btc) else round(float(btc), 2),
        high_risk=bool(guard is not None and pd.notna(btc) and btc <= -guard),
        label=classify(row, rules),
    )


# --- aggregation --------------------------------------------------------------------------------


EPISODE_GAP_MS = 24 * 3600 * 1000


def distinct_dips(dips: Iterable[Dip]) -> int:
    """Alerts on the same symbol less than 24h apart are one sell-off, counted once."""
    count = 0
    last: dict[str, int] = {}
    for d in sorted(dips, key=lambda d: d.t_ms):
        if d.symbol not in last or d.t_ms - last[d.symbol] > EPISODE_GAP_MS:
            count += 1
        last[d.symbol] = d.t_ms
    return count


@dataclass
class Summary:
    count: int
    distinct: int
    scored: int
    wins: int
    target_first: int
    stop_first: int
    total_pct: float
    worst_pct: float | None

    @property
    def win_rate(self) -> float | None:
        return self.wins / self.scored * 100 if self.scored else None

    @property
    def avg_pct(self) -> float | None:
        return self.total_pct / self.scored if self.scored else None


def summarize(dips: Iterable[Dip]) -> Summary:
    dips = list(dips)
    done = [d for d in dips if d.result_pct is not None]
    return Summary(
        count=len(dips),
        distinct=distinct_dips(dips),
        scored=len(done),
        wins=sum(d.result_pct > 0 for d in done),
        target_first=sum(d.first_hit == "target" for d in done),
        stop_first=sum(d.first_hit == "stop" for d in done),
        total_pct=sum(d.result_pct for d in done),
        worst_pct=min((d.result_pct for d in done), default=None),
    )


@dataclass
class SymbolData:
    symbol: str
    series: pd.DataFrame
    path: list[Candle]

    def __post_init__(self) -> None:
        self.open_times = [c.open_time_ms for c in self.path]  # for fast 7-day slicing


def analyse(
    data: list[SymbolData],
    strategy: StrategyConfig,
    *,
    start_ms: int,
    near_drop: float,
    near_rsi: float,
) -> tuple[list[Dip], dict[tuple[float, float], Summary]]:
    """(alerted + near-miss dips, threshold grid)."""
    rules = strategy.dip_rules
    cooldown = rules.cooldown_minutes_per_symbol
    dips: list[Dip] = []
    memo: dict[tuple[str, int], Dip] = {}

    def scored(sd: SymbolData, idx: int) -> Dip:
        key = (sd.symbol, idx)
        if key not in memo:
            dip = make_dip(sd.symbol, sd.series, idx, rules)
            memo[key] = score(dip, sd.series["atr_4h"].iloc[idx], sd.path, strategy, sd.open_times)
        return memo[key]

    for sd in data:
        s = sd.series
        in_period = s["t"] >= start_ms
        alerted = [i for i in find_dips(s["t"], passes(s, rules) & in_period, cooldown)]
        near = near_miss_dips(
            s.assign(drop_pct=s["drop_pct"].where(in_period)),
            rules,
            alerted,
            near_drop=near_drop,
            near_rsi=near_rsi,
            cooldown_minutes=cooldown,
        )
        dips += [scored(sd, i) for i in alerted + near]

    grid: dict[tuple[float, float], Summary] = {}
    for drop in GRID_DROPS:
        for rsi in GRID_RSIS:
            picked = []
            for sd in data:
                mask = passes(sd.series, rules, drop=drop, rsi=rsi) & (sd.series["t"] >= start_ms)
                picked += [scored(sd, i) for i in find_dips(sd.series["t"], mask, cooldown)]
            grid[(drop, rsi)] = summarize(picked)
    dips.sort(key=lambda d: d.t_ms)
    return dips, grid


# --- trend filters ------------------------------------------------------------------------------


def _gt(a: pd.Series, b: pd.Series) -> pd.Series:
    """a > b, where missing data on either side counts as False (never passes, as live)."""
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    return (a > b).fillna(False).astype(bool)


TREND_FILTERS = {
    "none": lambda s: pd.Series(True, index=s.index),
    "coin > 200d EMA": lambda s: _gt(s["price"], s["coin_ema200_1d"]),
    "coin 50d > 200d EMA": lambda s: _gt(s["coin_ema50_1d"], s["coin_ema200_1d"]),
    "BTC > 200d EMA": lambda s: _gt(s["btc_price"], s["btc_ema200_1d"]),
    "BTC > 50d EMA": lambda s: _gt(s["btc_price"], s["btc_ema50_1d"]),
    # Same definition as the bot's "trend 1d up" (indicators.trend_label).
    "BTC daily trend up": lambda s: (
        _gt(s["btc_price"], s["btc_ema20_1d"])
        & _gt(s["btc_ema20_1d"], s["btc_ema50_1d"])
        & _gt(s["btc_ema50_1d"], s["btc_ema200_1d"])
    ),
}
FILTER_COOLDOWNS = {"every 3h (current)": None, "one per 24h": 24 * 60}


def compare_filters(
    data: list[SymbolData], strategy: StrategyConfig, *, start_ms: int
) -> list[tuple[str, str, Summary]]:
    """Current thresholds + each trend filter, with the current cooldown and a 24h one."""
    rules = strategy.dip_rules
    memo: dict[tuple[str, int], Dip] = {}
    rows = []
    for name, trend in TREND_FILTERS.items():
        for cooldown_label, cooldown in FILTER_COOLDOWNS.items():
            minutes = rules.cooldown_minutes_per_symbol if cooldown is None else cooldown
            picked = []
            for sd in data:
                s = sd.series
                mask = passes(s, rules) & trend(s) & (s["t"] >= start_ms)
                for idx in find_dips(s["t"], mask, minutes):
                    key = (sd.symbol, idx)
                    if key not in memo:
                        dip = make_dip(sd.symbol, s, idx, rules)
                        memo[key] = score(
                            dip, s["atr_4h"].iloc[idx], sd.path, strategy, sd.open_times
                        )
                    picked.append(memo[key])
            rows.append((name, cooldown_label, summarize(picked)))
    return rows


def render_filters(rows: list[tuple[str, str, Summary]]) -> str:
    lines = [
        "## Trend filters (current drop/RSI thresholds)",
        "",
        "Only alert on a dip when the bigger trend agrees (daily EMAs, closed daily candles only).",
        "",
        "| filter | alerts | distinct dips | win rate | avg | total | target first | stop first |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, cooldown_label, s in rows:
        lines.append(
            f"| {name} · {cooldown_label} | {s.count} | {s.distinct} | {_pct(s.win_rate, False)}"
            f" | {_pct(s.avg_pct)} | {_pct(s.total_pct)} | {s.target_first} | {s.stop_first} |"
        )
    return "\n".join(lines)


# --- reporting ------------------------------------------------------------------------------------


def _pct(v: float | None, signed: bool = True) -> str:
    if v is None:
        return "-"
    return f"{v:+.1f}%" if signed else f"{v:.0f}%"


def render_report(
    dips: list[Dip],
    grid: dict[tuple[float, float], Summary],
    rules: DipRules,
    *,
    days: int,
    symbols: list[str],
    near_drop: float,
    near_rsi: float,
) -> str:
    lines = [
        f"# Missed-dip analysis: last {days} days, {', '.join(symbols)}",
        "",
        f"Current rules: drop ≥ {rules.min_drop_from_high_pct}% from the "
        f"{rules.high_lookback_hours}h high, RSI 1h ≤ {rules.rsi_1h_max}"
        + (", below the lower 4h Bollinger band" if rules.require_lower_bollinger_4h else "")
        + f", cooldown {rules.cooldown_minutes_per_symbol} min.",
        f"Near-miss band: drop ≥ {near_drop}% and RSI ≤ {near_rsi}. Each dip is scored as a"
        " trade: first target after fees vs the ATR stop, else the 7-day close (net of fees).",
        "",
        "## By label",
        "",
        "| label | alerts | distinct dips | scored | win rate | avg | total | target first"
        " | stop first | worst |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, title in LABELS.items():
        group = [d for d in dips if d.label == key]
        if not group and key in ("bollinger", "trend"):
            continue
        s = summarize(group)
        lines.append(
            f"| {title} | {s.count} | {s.distinct} | {s.scored} | {_pct(s.win_rate, False)}"
            f" | {_pct(s.avg_pct)}"
            f" | {_pct(s.total_pct)} | {s.target_first} | {s.stop_first} | {_pct(s.worst_pct)} |"
        )
    lines += [
        "",
        "## Threshold grid",
        "",
        "Each cell: alerts fired (distinct dips) · win rate · average result per trade. "
        "**Bold** = your current setting.",
        "",
        "| min drop \\ max RSI | " + " | ".join(f"≤ {r:g}" for r in GRID_RSIS) + " |",
        "|---|" + "---|" * len(GRID_RSIS),
    ]
    for drop in GRID_DROPS:
        cells = []
        for rsi in GRID_RSIS:
            s = grid[(drop, rsi)]
            text = f"{s.count} ({s.distinct}) · {_pct(s.win_rate, False)} · {_pct(s.avg_pct)}"
            if drop == rules.min_drop_from_high_pct and rsi == rules.rsi_1h_max:
                text = f"**{text}**"
            cells.append(text)
        lines.append(f"| ≥ {drop:g}% | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Total result per cell (sum of net % per trade, equal size):",
        "",
        "| min drop \\ max RSI | " + " | ".join(f"≤ {r:g}" for r in GRID_RSIS) + " |",
        "|---|" + "---|" * len(GRID_RSIS),
    ]
    for drop in GRID_DROPS:
        lines.append(
            f"| ≥ {drop:g}% | "
            + " | ".join(f"{_pct(grid[(drop, r)].total_pct)}" for r in GRID_RSIS)
            + " |"
        )

    near = [d for d in dips if d.label != "alerted" and d.result_pct is not None]
    for title, chosen in (
        ("Best near misses", sorted(near, key=lambda d: -d.result_pct)[:10]),
        ("Worst near misses", sorted(near, key=lambda d: d.result_pct)[:10]),
    ):
        lines += [
            "",
            f"## {title}",
            "",
            "| when (UTC) | symbol | price | drop | RSI 1h | label | first hit | result |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for d in chosen:
            lines.append(
                f"| {d.time_utc} | {d.symbol} | {d.price:g} | {d.drop_pct:.2f}% | {d.rsi_1h:.1f}"
                f" | {LABELS[d.label]} | {d.first_hit} | {_pct(d.result_pct)} |"
            )

    smallest = min((s.scored for s in grid.values() if s.scored), default=0)
    lines += [
        "",
        "## How to read this",
        "",
        "- Small samples swing a lot: a cell with fewer than about 10 scored alerts says little.",
        f"  The smallest non-empty cell here has {smallest}.",
        "- Alerts repeat every cooldown period while a dip stays deep (as the live bot does), so"
        " one sell-off can produce several alerts with similar outcomes. 'Distinct dips' counts"
        " alerts more than 24h apart as separate: that's closer to the real sample size.",
        "- Beware overfitting: the best cell over this period isn't necessarily best next month."
        " Prefer settings that do well together with their neighbouring cells, and re-check over"
        " a longer --days.",
        "- Dips in the last 7 days are 'pending' (not 7 days of data yet) and aren't scored.",
    ]
    return "\n".join(lines)


def cross_check(dips: list[Dip], db_path: Path, start_ms: int) -> list[str]:
    """Compare replayed 'alerted' dips with signals the live bot actually stored."""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "select symbol, ts from signals where ts >= ? order by ts", (start_ms // 1000,)
        ).fetchall()
    finally:
        conn.close()
    replay = [d for d in dips if d.label == "alerted"]
    tolerance_ms = 20 * 60_000
    lines = [f"Live bot signals since start: {len(rows)} · replay 'alerted': {len(replay)}"]
    for symbol, ts in rows:
        match = next(
            (d for d in replay if d.symbol == symbol and abs(d.t_ms - ts * 1000) <= tolerance_ms),
            None,
        )
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
        lines.append(f"  live {symbol} {when} UTC -> {'matched' if match else 'NOT in replay'}")
    live = {(s, ts) for s, ts in rows}
    for d in replay:
        if not any(s == d.symbol and abs(ts * 1000 - d.t_ms) <= tolerance_ms for s, ts in live):
            lines.append(f"  replay {d.symbol} {d.time_utc} UTC -> no live signal (bot down?)")
    return lines


# --- CLI ----------------------------------------------------------------------------------------


async def load_data(
    symbols: list[str], btc_symbol: str, days: int, lookback_hours: int
) -> tuple[list[SymbolData], int]:
    now_ms = int(time.time() * 1000)
    end_ms = now_ms - now_ms % INTERVAL_MS["1h"]  # hour-aligned so re-runs hit the cache
    start_ms = end_ms - days * 86_400_000

    def warm(interval: str) -> int:
        return start_ms - WARMUP_CANDLES * INTERVAL_MS[interval]

    async with httpx.AsyncClient() as http:
        btc15 = await cached_klines(btc_symbol, "15m", warm("15m"), end_ms, http=http)
        btc1d = await cached_klines(btc_symbol, "1d", warm("1d"), end_ms, http=http)
        data = []
        for symbol in symbols:
            c15 = await cached_klines(symbol, "15m", warm("15m"), end_ms, http=http)
            c1h = await cached_klines(symbol, "1h", warm("1h"), end_ms, http=http)
            c4h = await cached_klines(symbol, "4h", warm("4h"), end_ms, http=http)
            c1d = await cached_klines(symbol, "1d", warm("1d"), end_ms, http=http)
            series = rule_series(
                c15, c1h, c4h, btc15, lookback_hours=lookback_hours, c1d=c1d, btc1d=btc1d
            )
            data.append(SymbolData(symbol, series, c15))
    return data, start_ms


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--days", type=int, default=90, help=f"how far back to replay (max {MAX_DAYS})"
    )
    parser.add_argument("--symbols", help="e.g. SOL,LINK (default: config symbols)")
    parser.add_argument("--near-drop", type=float, default=4.0, help="near-miss minimum drop %%")
    parser.add_argument("--near-rsi", type=float, default=45.0, help="near-miss maximum RSI")
    parser.add_argument("--db", type=Path, help="bot database to cross-check live alerts against")
    parser.add_argument(
        "--exits", action="store_true", help="compare exit rules (walk-forward, see --train-end)"
    )
    parser.add_argument(
        "--train-end",
        default="2024-12-31",
        help="with --exits: choose the best setup on data up to this date, test after it",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    days = max(1, min(args.days, MAX_DAYS))
    strategy = load_settings().strategy
    symbols = strategy.symbols
    if args.symbols:
        wanted = {s.strip().upper().removesuffix("USDT") for s in args.symbols.split(",")}
        symbols = [s for s in strategy.symbols if s.removesuffix("USDT") in wanted]
    btc = strategy.context_symbols[0] if strategy.context_symbols else "BTCUSDT"
    print(f"Downloading {days} days of history for {', '.join(symbols)} (+{btc})...")
    data, start_ms = asyncio.run(
        load_data(symbols, btc, days, strategy.dip_rules.high_lookback_hours)
    )
    dips, grid = analyse(
        data, strategy, start_ms=start_ms, near_drop=args.near_drop, near_rsi=args.near_rsi
    )
    report = render_report(
        dips,
        grid,
        strategy.dip_rules,
        days=days,
        symbols=symbols,
        near_drop=args.near_drop,
        near_rsi=args.near_rsi,
    )
    report += "\n\n" + render_filters(compare_filters(data, strategy, start_ms=start_ms))
    if args.exits:
        from bot.evaluation.exits import render_walk_forward, walk_forward

        train_end_ms = int(pd.Timestamp(args.train_end, tz="UTC").timestamp() * 1000) + 86_399_999
        cells = walk_forward(
            data,
            strategy,
            start_ms=start_ms,
            train_end_ms=train_end_ms,
            find_dips=find_dips,
            passes=passes,
        )
        report += "\n\n" + render_walk_forward(cells, train_end_ms)
    if args.db:
        report += "\n\n## Cross-check with the live bot\n\n```\n"
        report += "\n".join(cross_check(dips, args.db, start_ms)) + "\n```\n"
    print(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md = REPORT_DIR / f"missed-dips-{stamp}.md"
    md.write_text(report + "\n")
    pd.DataFrame([asdict(d) for d in dips]).to_csv(md.with_suffix(".csv"), index=False)
    print(f"\nSaved: {md.relative_to(PROJECT_ROOT)} and .csv ({len(dips)} dips)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
