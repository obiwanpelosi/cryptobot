from bot.market.indicators import TimeframeIndicators
from bot.market.snapshot import BtcState, MarketSnapshot
from bot.settings import DipRules
from bot.signals.rules import evaluate, in_cooldown

RULES = DipRules(
    min_drop_from_high_pct=6,
    high_lookback_hours=72,
    rsi_1h_max=35,
    require_lower_bollinger_4h=False,
    btc_crash_guard_pct_1h=3,
    cooldown_minutes_per_symbol=180,
)


def tf(rsi=None, pct_b=None):
    fields = dict.fromkeys(TimeframeIndicators.model_fields)
    fields.update(rsi=rsi, bb_pct_b=pct_b)
    return TimeframeIndicators(**fields)


def snap(drop=7.0, rsi_1h=30.0, pct_b_4h=0.5, btc_1h=-0.5):
    btc = None
    if btc_1h is not None:
        btc = BtcState(
            price=80000,
            change_1h_pct=btc_1h,
            change_24h_pct=0,
            trend_1h=None,
            trend_4h=None,
            trend_1d=None,
        )
    return MarketSnapshot(
        symbol="SOLUSDT",
        price=100,
        as_of=0,
        change_1h_pct=None,
        change_24h_pct=None,
        change_7d_pct=None,
        high_lookback_hours=72,
        recent_high=None,
        recent_low=None,
        drop_from_high_pct=drop,
        rise_from_low_pct=None,
        timeframes={"1h": tf(rsi=rsi_1h), "4h": tf(pct_b=pct_b_4h)},
        supports=[],
        resistances=[],
        btc=btc,
        derivatives=None,
        sentiment=None,
        macro=[],
    )


def rules(**overrides):
    return RULES.model_copy(update=overrides)


def test_fires_when_all_conditions_pass():
    r = evaluate(snap(), RULES)
    assert r.fired and not r.high_risk
    assert [c.name for c in r.checks] == ["drop_from_high", "rsi_1h"]


def test_thresholds_are_inclusive():
    assert evaluate(snap(drop=6.0, rsi_1h=35.0), RULES).fired


def test_each_condition_can_block():
    assert not evaluate(snap(drop=5.99), RULES).fired
    assert not evaluate(snap(rsi_1h=35.01), RULES).fired


def test_lower_bollinger_when_required():
    strict = rules(require_lower_bollinger_4h=True)
    assert not evaluate(snap(pct_b_4h=0.1), strict).fired
    assert evaluate(snap(pct_b_4h=-0.05), strict).fired


def test_null_threshold_disables_condition():
    r = evaluate(snap(rsi_1h=80), rules(rsi_1h_max=None))
    assert r.fired
    assert [c.name for c in r.checks] == ["drop_from_high"]


def test_no_conditions_never_fires():
    assert not evaluate(snap(), rules(min_drop_from_high_pct=None, rsi_1h_max=None)).fired


def test_missing_value_does_not_fire():
    r = evaluate(snap(rsi_1h=None), RULES)
    assert not r.fired
    assert r.checks[1].value is None and not r.checks[1].passed


def test_btc_crash_guard_flags_but_does_not_block():
    r = evaluate(snap(btc_1h=-3.2), RULES)
    assert r.fired and r.high_risk
    assert not evaluate(snap(btc_1h=-2.9), RULES).high_risk
    assert not evaluate(snap(btc_1h=None), RULES).high_risk
    assert not evaluate(snap(btc_1h=-9), rules(btc_crash_guard_pct_1h=None)).high_risk


def test_cooldown_boundary():
    assert not in_cooldown(None, 1000, 180)
    assert in_cooldown(0, 180 * 60 - 1, 180)
    assert not in_cooldown(0, 180 * 60, 180)


# --- trend filters ---------------------------------------------------------------------------


def trend_snap(price=100.0, ema200=90.0, btc_price=80000.0, btc_ema50=70000.0):
    s = snap()
    s.price = price
    s.timeframes["1d"] = tf()
    s.timeframes["1d"].ema200 = ema200
    s.btc.price = btc_price
    s.btc.ema50_1d = btc_ema50
    return s


TREND_RULES = RULES.model_copy(
    update={"require_coin_above_ema200_1d": True, "require_btc_above_ema50_1d": True}
)


def test_trend_filters_off_by_default():
    assert [c.name for c in evaluate(trend_snap(ema200=999), RULES).checks] == [
        "drop_from_high",
        "rsi_1h",
    ]


def test_trend_filters_pass_and_fail():
    r = evaluate(trend_snap(), TREND_RULES)
    assert r.fired
    assert [c.name for c in r.checks][-2:] == ["coin_above_ema200_1d", "btc_above_ema50_1d"]
    assert not evaluate(trend_snap(ema200=110), TREND_RULES).fired  # coin below its 200d EMA
    assert not evaluate(trend_snap(btc_ema50=90000), TREND_RULES).fired  # BTC below its 50d
    assert not evaluate(trend_snap(price=90.0, ema200=90.0), TREND_RULES).fired  # equal isn't above


def test_trend_filters_missing_data_never_pass():
    r = evaluate(trend_snap(ema200=None, btc_ema50=None), TREND_RULES)
    assert not r.fired
    coin, btc = r.checks[-2:]
    assert coin.threshold is None and not coin.passed and not btc.passed


def test_trend_check_in_alert_text():
    from bot.telegram.messages import entry_alert

    r = evaluate(trend_snap(), TREND_RULES)
    text = entry_alert(trend_snap(), r, None)
    assert "Price vs 200d EMA: 100.00 (&gt; 90) ✓" in text
    assert "BTC vs 50d EMA: 80000.00 (&gt; 70000) ✓" in text
    missing = evaluate(trend_snap(ema200=None), TREND_RULES)
    assert "Price vs 200d EMA: 100.00 (&gt; n/a) ✗" in entry_alert(trend_snap(), missing, None)
