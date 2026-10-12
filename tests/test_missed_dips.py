import httpx
import pandas as pd
import pytest

from bot.evaluation import missed_dips as md
from bot.evaluation.history import fetch_klines
from bot.exchange.models import INTERVAL_MS, Candle
from bot.market import indicators as ind
from bot.settings import load_strategy

STRATEGY = load_strategy()
RULES = STRATEGY.dip_rules  # drop >= 6, RSI <= 35, cooldown 180 min, lookback 72h
T0 = 1_790_000_000_000 - 1_790_000_000_000 % INTERVAL_MS["4h"]


def candles(interval, closes, start=T0, spread=0.002):
    step = INTERVAL_MS[interval]
    out = []
    for i, c in enumerate(closes):
        o = start + i * step
        prev = closes[i - 1] if i else c
        out.append(
            Candle(
                o,
                prev,
                max(prev, c) * (1 + spread),
                min(prev, c) * (1 - spread),
                c,
                1.0,
                o + step - 1,
            )
        )
    return out


def flat(interval, n, price=100.0, start=T0):
    return candles(interval, [price] * n, start, spread=0.001)


# --- rule values -------------------------------------------------------------------------------


def test_no_look_ahead_rsi_uses_only_closed_candles():
    closes_1h = [100 + (i % 5) for i in range(60)] + [60.0]  # last hour crashes
    c1h = candles("1h", closes_1h)
    n15 = len(closes_1h) * 4
    c15 = flat("15m", n15)
    series = md.rule_series(c15, c1h, flat("4h", 20), flat("15m", n15), lookback_hours=72)
    crash_open = c1h[-1].open_time_ms
    crash_close_time = crash_open + INTERVAL_MS["1h"]
    expected_before = ind.round_sig(ind.rsi(pd.Series(closes_1h[:-1])).iloc[-1], 4)
    before = series[series["t"] < crash_close_time]["rsi_1h"].iloc[-1]
    assert before == expected_before  # the crash candle isn't visible until it closes
    at_close = series[series["t"] == crash_close_time]["rsi_1h"]
    assert at_close.empty or at_close.iloc[0] < before


def test_drop_from_recent_high():
    c1h = candles("1h", [100.0] * 10 + [110.0] + [100.0] * 10, spread=0.0)
    n15 = 21 * 4
    c15 = candles("15m", [100.0] * (n15 - 1) + [99.0], spread=0.0)
    series = md.rule_series(c15, c1h, flat("4h", 10), flat("15m", n15), lookback_hours=72)
    last = series.iloc[-1]
    assert last["price"] == 99.0
    assert last["drop_pct"] == pytest.approx(ind.round_sig((110 - 99) / 110 * 100, 3))


# --- dips and labels ---------------------------------------------------------------------------


def test_cooldown_groups_closes_into_dips():
    step = INTERVAL_MS["15m"]
    times = pd.Series([T0 + i * step for i in range(40)])
    mask = pd.Series([False] * 40)
    for i in (2, 4, 18, 30):  # 30 min apart = same dip; 4h apart = new dip
        mask[i] = True
    assert md.find_dips(times, mask, 180) == [2, 18, 30]


@pytest.mark.parametrize(
    "drop, rsi, label",
    [
        (6.0, 35.0, "alerted"),
        (5.99, 35.0, "drop_too_small"),
        (6.0, 35.01, "rsi_too_high"),
        (5.0, 40.0, "both"),
    ],
)
def test_classify_edges(drop, rsi, label):
    row = pd.Series({"drop_pct": drop, "rsi_1h": rsi, "pct_b_4h": 0.5})
    assert md.classify(row, RULES) == label


def test_disabled_rule_is_ignored():
    rules = RULES.model_copy(update={"rsi_1h_max": None})
    row = pd.Series({"drop_pct": 7.0, "rsi_1h": 80.0, "pct_b_4h": 0.5})
    assert md.classify(row, rules) == "alerted"
    series = pd.DataFrame({"drop_pct": [7.0, 5.0], "rsi_1h": [80.0, 10.0], "pct_b_4h": [0.5, 0.5]})
    assert md.passes(series, rules).tolist() == [True, False]


def test_missing_values_never_pass():
    series = pd.DataFrame({"drop_pct": [None], "rsi_1h": [20.0], "pct_b_4h": [None]})
    assert md.passes(series, RULES).tolist() == [False]


def test_near_misses_exclude_the_run_up_to_an_alert():
    step = INTERVAL_MS["15m"]
    series = pd.DataFrame(
        {
            "t": [T0 + i * step for i in range(30)],
            "drop_pct": [4.5] * 2 + [7.0] * 2 + [0.0] * 20 + [5.0] * 2 + [0.0] * 4,
            "rsi_1h": [33.0] * 30,
            "pct_b_4h": [0.5] * 30,
        }
    )
    alerted = md.find_dips(series["t"], md.passes(series, RULES), 180)
    near = md.near_miss_dips(series, RULES, alerted, near_drop=4, near_rsi=45, cooldown_minutes=180)
    assert alerted == [2]
    assert near == [24]  # index 0 is the run-up to the alert at index 2, so it's excluded


# --- scoring ---------------------------------------------------------------------------


def dip_at(price=100.0, t=T0):
    return md.Dip(
        symbol="SOLUSDT",
        time_utc="",
        t_ms=t,
        price=price,
        drop_pct=7.0,
        rsi_1h=30.0,
        pct_b_4h=0.1,
        btc_change_1h_pct=0.0,
        high_risk=False,
        label="alerted",
    )


def path_after(t, prices):
    return candles("15m", prices, start=t, spread=0.0)


def test_score_target_stop_and_pending():
    n = 7 * 96
    up = path_after(T0, [100.0] * 10 + [111.0] * (n - 10))
    d = md.score(dip_at(), 2.0, up, STRATEGY)  # stop 96, target ~110.22
    assert d.first_hit == "target" and d.result_pct == 10.0

    down = path_after(T0, [100.0] * 10 + [95.0] * (n - 10))
    d = md.score(dip_at(), 2.0, down, STRATEGY)
    assert d.first_hit == "stop" and d.result_pct == pytest.approx((0.999**2 * 96 / 100 - 1) * 100)

    short = path_after(T0, [100.0] * 50)
    assert md.score(dip_at(), 2.0, short, STRATEGY).first_hit == "pending"


def test_grid_current_cell_matches_alerted():
    n = 7 * 96 + 400
    step = INTERVAL_MS["15m"]
    drops = [0.0] * n
    for i, v in ((10, 6.5), (40, 4.5), (80, 8.0)):
        drops[i] = v
    series = pd.DataFrame(
        {
            "t": [T0 + (i + 1) * step for i in range(n)],
            "price": [100.0] * n,
            "drop_pct": drops,
            "rsi_1h": [30.0] * n,
            "pct_b_4h": [0.5] * n,
            "atr_4h": [2.0] * n,
            "btc_change_1h_pct": [0.0] * n,
        }
    )
    sd = md.SymbolData("SOLUSDT", series, path_after(T0 + step, [100.0] * n))
    dips, grid = md.analyse([sd], STRATEGY, start_ms=T0, near_drop=4, near_rsi=45)
    alerted = [d for d in dips if d.label == "alerted"]
    assert len(alerted) == 2 == grid[(6.0, 35.0)].count
    assert grid[(4.0, 35.0)].count == 3
    assert grid[(8.0, 35.0)].count == 1
    assert [d.label for d in dips].count("drop_too_small") == 1


def test_report_renders():
    dips = [md.score(dip_at(), 2.0, path_after(T0, [111.0] * 700), STRATEGY)]
    grid = {(d, r): md.summarize(dips) for d in md.GRID_DROPS for r in md.GRID_RSIS}
    text = md.render_report(
        dips, grid, RULES, days=90, symbols=["SOLUSDT"], near_drop=4, near_rsi=45
    )
    assert "Alerted (current rules) | 1 | 1 | 1 | 100%" in text
    assert "**1 (1) · 100% · +10.0%**" in text


def test_distinct_dips_merge_repeats_within_24h():
    hour = 3_600_000
    dips = [dip_at(t=T0), dip_at(t=T0 + 3 * hour), dip_at(t=T0 + 30 * hour)]
    other = dip_at(t=T0 + hour)
    other.symbol = "LINKUSDT"
    assert md.distinct_dips(dips + [other]) == 3


# --- downloading -----------------------------------------------------------------------


async def test_fetch_klines_pages_without_gaps_or_duplicates():
    step = INTERVAL_MS["1h"]
    total = 2500
    calls = []

    def handler(request):
        start = int(request.url.params["startTime"])
        limit = int(request.url.params["limit"])
        calls.append(start)
        first = (start - T0) // step
        rows = [
            [T0 + i * step, "1", "2", "0.5", "1.5", "10", T0 + (i + 1) * step - 1]
            for i in range(first, min(first + limit, total))
        ]
        return httpx.Response(200, json=rows)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        out = await fetch_klines(
            "SOLUSDT", "1h", T0, T0 + total * step, http=http, now_ms=T0 + (total + 5) * step
        )
    assert len(out) == total == len({c.open_time_ms for c in out})
    assert len(calls) == 3
    assert all(b.open_time_ms - a.open_time_ms == step for a, b in zip(out, out[1:], strict=False))


# --- trend filters ------------------------------------------------------------------------


def test_daily_ema_uses_closed_days_only():
    days = 260
    closes = [100.0] * (days - 1) + [1000.0]  # the last day jumps
    c1d = candles("1d", closes, start=T0)
    jump_close_time = c1d[-1].open_time_ms + INTERVAL_MS["1d"]
    n15 = days * 96
    c15 = flat("15m", n15)
    series = md.rule_series(
        c15,
        flat("1h", days * 24),
        flat("4h", days * 6),
        flat("15m", n15),
        lookback_hours=72,
        c1d=c1d,
        btc1d=c1d,
    )
    before = series[series["t"] < jump_close_time]["coin_ema20_1d"].iloc[-1]
    assert before == pytest.approx(100.0)  # the jump day isn't visible until it closes


def test_trend_filter_masks():
    s = pd.DataFrame(
        {
            "price": [110.0, 90.0],
            "coin_ema50_1d": [105.0, 95.0],
            "coin_ema200_1d": [100.0, 100.0],
            "btc_price": [50.0, 50.0],
            "btc_ema20_1d": [48.0, 52.0],
            "btc_ema50_1d": [46.0, 51.0],
            "btc_ema200_1d": [40.0, 45.0],
        }
    )
    f = md.TREND_FILTERS
    assert f["none"](s).tolist() == [True, True]
    assert f["coin > 200d EMA"](s).tolist() == [True, False]
    assert f["coin 50d > 200d EMA"](s).tolist() == [True, False]
    assert f["BTC > 200d EMA"](s).tolist() == [True, True]
    assert f["BTC daily trend up"](s).tolist() == [True, False]
    missing = s.assign(btc_ema200_1d=[None, None])
    assert f["BTC > 200d EMA"](missing).tolist() == [False, False]  # no data never passes


def test_compare_filters_none_matches_alerted_and_filters_reduce():
    n = 7 * 96 + 400
    step = INTERVAL_MS["15m"]
    drops = [0.0] * n
    for i in (10, 80):
        drops[i] = 7.0
    series = pd.DataFrame(
        {
            "t": [T0 + (i + 1) * step for i in range(n)],
            "price": [100.0] * n,
            "drop_pct": drops,
            "rsi_1h": [30.0] * n,
            "pct_b_4h": [0.5] * n,
            "atr_4h": [2.0] * n,
            "btc_change_1h_pct": [0.0] * n,
            "coin_ema20_1d": [1.0] * n,
            "coin_ema50_1d": [1.0] * n,
            "coin_ema200_1d": [1.0] * n,
            "btc_price": [50.0] * n,
            "btc_ema20_1d": [60.0] * n,
            "btc_ema50_1d": [60.0] * n,
            "btc_ema200_1d": [60.0] * n,
        }
    )
    sd = md.SymbolData("SOLUSDT", series, path_after(T0 + step, [100.0] * n))
    rows = {(name, cd): s for name, cd, s in md.compare_filters([sd], STRATEGY, start_ms=T0)}
    assert rows[("none", "every 3h (current)")].count == 2
    assert rows[("coin > 200d EMA", "every 3h (current)")].count == 2  # price above
    assert rows[("BTC > 200d EMA", "every 3h (current)")].count == 0  # BTC below
    assert rows[("none", "one per 24h")].count == 1  # 70 x 15m apart < 24h
    assert "## Trend filters" in md.render_filters(
        list(md.compare_filters([sd], STRATEGY, start_ms=T0))
    )


def test_fast_slicing_gives_the_same_score():
    n = 3 * 7 * 96
    prices = [100.0 + (i % 50) * 0.3 - (i // 300) * 2 for i in range(n)]
    path = path_after(T0, prices)
    times = [c.open_time_ms for c in path]
    for start in (0, 200, 700):
        t = T0 + start * INTERVAL_MS["15m"]
        slow = md.score(dip_at(price=prices[start], t=t), 2.0, path, STRATEGY)
        fast = md.score(dip_at(price=prices[start], t=t), 2.0, path, STRATEGY, times)
        assert (fast.first_hit, fast.result_pct, fast.worst_pct) == (
            slow.first_hit,
            slow.result_pct,
            slow.worst_pct,
        )
