# Crypto Dip-Trading Alert Bot — Requirements

## 1. Overview

A 24/7 Telegram bot that monitors Binance spot markets **SOL/USDT** and **LINK/USDT**, detects dip-buying opportunities using technical indicators and market context, advises on entry size, tracks open positions, and alerts on profit targets and stop-losses. An AI model reviews each signal and gives a recommendation with reasoning.

**The bot is advisory.** The user makes every trading decision. The bot must never place an order without an explicit user confirmation, and order placement is an optional later phase.

### Trading style the bot supports

- Buy on price dips, sell into strength.
- Holding period: roughly 1 day to 1 week.
- Profit targets: typically 10–20%.
- Always bought with USDT and sold back to USDT.



### Non-goals

- No high-frequency or scalping strategies.
- No futures/margin trading (futures data is used as *context only*).
- No withdrawals, ever.
- No claims of guaranteed or predicted returns in any message.

---



## 2. Tech stack


| Concern           | Choice                                                                           |
| ----------------- | -------------------------------------------------------------------------------- |
| Language          | Python 3.11+                                                                     |
| Binance           | `python-binance` (or `ccxt`), REST + WebSocket streams                           |
| Telegram          | `python-telegram-bot` v21+ (async)                                               |
| Data / indicators | `pandas`, `numpy`, `ta` library (or hand-written indicator functions with tests) |
| Storage           | SQLite via `SQLAlchemy` or `sqlite3`                                             |
| Config            | `.env` + `pydantic-settings`, plus a `config.yaml` for strategy parameters       |
| Scheduling        | `asyncio` tasks (optionally `APScheduler`)                                       |
| AI models         | Provider-agnostic client layer (Anthropic first; others pluggable)               |
| Logging           | Python `logging` with rotating file handler                                      |
| Deployment        | Linux VPS, run under `systemd` or Docker                                         |


Everything runs in one async process. Keep modules small and testable.

---



## 3. Project structure (suggested)

```
crypto-bot/
├── REQUIREMENTS.md
├── .env.example
├── config.yaml
├── requirements.txt
├── bot/
│   ├── main.py              # entry point, wires everything together
│   ├── settings.py          # env + yaml config loading and validation
│   ├── exchange/
│   │   ├── client.py        # Binance REST wrapper (prices, klines, balances, trades)
│   │   └── streams.py       # WebSocket price streams with auto-reconnect
│   ├── market/
│   │   ├── indicators.py    # RSI, EMA, MACD, Bollinger, ATR, volume ratios, S/R levels
│   │   ├── context.py       # BTC trend, funding, open interest, fear & greed, news
│   │   └── snapshot.py      # builds the structured market snapshot per symbol
│   ├── signals/
│   │   ├── rules.py         # deterministic dip / exit trigger rules
│   │   └── sizing.py        # position sizing and stop-loss suggestion
│   ├── ai/
│   │   ├── base.py          # provider interface
│   │   ├── anthropic_client.py
│   │   ├── prompts.py       # system prompt + context templates
│   │   └── schema.py        # pydantic models for AI JSON output
│   ├── positions/
│   │   ├── tracker.py       # open positions, P/L monitoring, target/stop alerts
│   │   └── sync.py          # optional: sync fills from Binance trade history
│   ├── telegram/
│   │   ├── handlers.py      # commands and button callbacks
│   │   └── messages.py      # message formatting
│   ├── storage/
│   │   ├── db.py
│   │   └── models.py
│   └── evaluation/
│       └── outcomes.py      # scores past signals for paper-trading review
└── tests/
```

---



## 4. Configuration



### `.env` (secrets — never committed)

```
BINANCE_API_KEY=
BINANCE_API_SECRET=
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_CHAT_IDS=123456789        # comma-separated; bot ignores everyone else
ANTHROPIC_API_KEY=
# OPENAI_API_KEY=                           # optional, for comparing providers
```



### `config.yaml` (strategy parameters — all tunable without code changes)

```yaml
symbols: [SOLUSDT, LINKUSDT]
context_symbols: [BTCUSDT]

mode: paper            # paper | live-advisory | live-execution (later phase)

timeframes: [15m, 1h, 4h, 1d]
history_candles: 500   # per timeframe, loaded on startup

dip_rules:
  min_drop_from_high_pct: 6      # drop from recent high
  high_lookback_hours: 72
  rsi_1h_max: 35
  require_lower_bollinger_4h: false
  btc_crash_guard_pct_1h: 3      # if BTC fell more than this in 1h, flag as high-risk
  cooldown_minutes_per_symbol: 180

exit_alerts:
  profit_targets_pct: [10, 15, 20]
  stop_atr_multiplier: 2.0       # stop = entry - (ATR_4h * multiplier)
  trailing_after_pct: 15         # optional trailing stop suggestion once past this

sizing:
  risk_per_trade_pct: 1.5        # max % of total balance lost if stop is hit
  max_position_pct: 30           # never suggest more than this % of balance in one trade
  min_order_usdt: 10

fees:
  taker_pct: 0.1                 # include in all P/L calculations

ai:
  enabled: false                 # turned on in Phase 6
  provider: anthropic
  light_model: claude-haiku-4-5-20251001   # routine checks
  strong_model: claude-sonnet-5            # entry and hold/sell decisions
  max_calls_per_hour: 20
  timeout_seconds: 60
```

Model names must come from config, never hardcoded.

---



## 5. Functional requirements



### 5.1 Binance data layer

- Connect with a **read-only** API key (trading disabled, withdrawals disabled, IP-restricted).
- On startup, load historical klines for every symbol and timeframe in config.
- Subscribe to WebSocket kline/ticker streams for live updates; keep in-memory candle buffers current.
- Auto-reconnect streams with exponential backoff; alert the user on Telegram if data is stale for more than 5 minutes.
- Respect Binance rate limits (track request weight; back off on 429/418).
- Fetch spot balances (USDT, SOL, LINK) on demand and cache for 30 seconds.
- Fetch public futures data for context: funding rate and open interest for SOL and LINK.



### 5.2 Indicator engine

Computed per symbol, per timeframe, and refreshed on every closed candle:

- RSI (14)
- EMA 20 / 50 / 200 and trend direction
- MACD (12, 26, 9) and histogram direction
- Bollinger Bands (20, 2) and price position within them
- ATR (14), absolute and as % of price
- Volume vs 20-period average (ratio)
- % change over 1h, 24h, 7d
- Distance from recent high/low (lookback from config)
- Simple support/resistance levels from recent swing highs/lows

Indicator functions must be pure functions with unit tests against known values.

### 5.3 Market context

- **BTC state:** trend on 1h/4h/1d, 1h and 24h change. SOL and LINK correlate strongly with BTC.
- **Derivatives:** funding rate and open interest change (24h) for each symbol.
- **Sentiment:** Crypto Fear & Greed Index (alternative.me public API), refreshed hourly.
- **Macro calendar (optional):** manually maintained list in config of upcoming Fed meetings / CPI dates.
- **News (optional, Phase 6+):** recent headlines via the AI provider's web search tool.



### 5.4 Market snapshot

A single structured object (serialisable to JSON) per symbol combining 5.2 and 5.3. This is what rules evaluate and what the AI receives. Keep it compact: summarised values, not raw candle arrays.

### 5.5 Dip signal rules (deterministic)

- Evaluate on each closed 15m candle.
- A signal fires when all enabled `dip_rules` conditions are met.
- Respect the per-symbol cooldown to avoid alert spam.
- If the BTC crash guard triggers, still send the alert but mark it clearly as high-risk.
- Every signal is saved to the database with its full snapshot, whether or not the user acts.



### 5.6 Position sizing

When a signal fires, suggest:

- **Stop-loss price:** `entry - ATR_4h * stop_atr_multiplier`
- **Position size (USDT):** `(total_balance * risk_per_trade_pct / 100) / ((entry - stop) / entry)`
- Cap at `max_position_pct` of total balance and at available USDT.
- If the result is below `min_order_usdt`, say so instead of suggesting a trade.
- Show the potential loss in USDT if the stop is hit, and potential gain at each profit target, **after fees**.



### 5.7 Telegram interface

**Security:** the bot only responds to chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS`; all others are ignored and logged.

**Commands**


| Command                          | Action                                                                      |
| -------------------------------- | --------------------------------------------------------------------------- |
| `/start`                         | Welcome message, current mode, list of commands                             |
| `/price`                         | Current SOL, LINK, BTC prices and 24h change                                |
| `/balance`                       | USDT, SOL, LINK balances and total value in USDT                            |
| `/analysis <SYMBOL>`             | Indicator summary for the symbol (and AI view if enabled)                   |
| `/positions`                     | Open positions with entry, current price, P/L % and USDT, stop, next target |
| `/enter <SYMBOL> <USDT> [price]` | Manually record a position (price defaults to current)                      |
| `/close <ID> [price]`            | Mark a position as closed and record realised P/L                           |
| `/history`                       | Last 20 closed trades                                                       |
| `/stats`                         | Win rate, average gain/loss, total P/L, signal accuracy                     |
| `/settings`                      | Show current config values                                                  |
| `/pause` / `/resume`             | Pause or resume signal alerts (monitoring of open positions continues)      |


**Entry alert message** must include: symbol, price, why it triggered (the rule values), BTC context, suggested size, stop price, targets, potential loss/gain after fees, AI recommendation and confidence (when enabled), and a high-risk flag if applicable.

**Entry alert buttons:**

- `Enter with suggested amount`
- `Custom amount` → bot asks for a USDT amount, validates it against balance, then confirms
- `Ignore`

In advisory modes, tapping Enter records the position in the tracker; the user places the actual order on Binance themselves. Every entry requires a confirmation step ("Record SOL entry of 120 USDT at 142.35? Yes / No").

### 5.8 Position tracker

- Monitor every open position on each price update.
- Alert once at each profit target (10%, 15%, 20% by default), calculated after fees.
- At each target, request an AI **hold vs take profit** recommendation (when enabled) and include it in the alert, with buttons `Take profit` / `Hold` / `Set trailing stop`.
- Alert when price reaches the stop-loss, and when it comes within 2% of it.
- Optional trailing stop suggestion once past `trailing_after_pct`.
- **Optional sync:** read the user's Binance trade history (read-only) to detect real buy/sell fills and offer to link them to tracked positions, so recorded prices match actual fills.



### 5.9 AI analysis layer (Phase 6)

- Provider-agnostic interface: `analyze_entry(snapshot, portfolio, history) -> EntryAdvice` and `analyze_exit(position, snapshot, history) -> ExitAdvice`.
- Use `light_model` for routine re-checks and `strong_model` for entry and target-hit decisions.
- The AI is **only called when a rule fires or a target/stop is hit**, never on a timer loop.
- **System prompt** contains: the user's strategy (dip-buying, 1–7 day holds, 10–20% targets, risk rules), instructions to be conservative and honest about uncertainty, and a requirement to respond with JSON only.
- **Context** passed each call: the market snapshot for the symbol, BTC context, derivatives and sentiment data, the rule values that triggered, the suggested size and stop, current portfolio, and a short summary of the user's last 10–20 trades on that symbol with outcomes.
- **Output schema** (validated with pydantic):
  ```json
  {
    "action": "enter | wait | skip | hold | take_profit | partial_take_profit",
    "confidence": 0.0,
    "reasoning": "2-4 sentences",
    "key_risks": ["..."],
    "suggested_stop": 0.0,
    "suggested_size_adjustment": "none | reduce | increase",
    "time_horizon": "hours | 1-3 days | 3-7 days"
  }
  ```
- If the response is invalid JSON, retry once; if it still fails, send the alert without AI advice and log the error.
- Enforce `max_calls_per_hour`; log every prompt, response, model name, latency and token usage to the database.
- The AI may adjust advice but never overrides the stop-loss or sizing caps in config.



### 5.10 Evaluation and paper trading

- In `paper` mode, the bot behaves fully but every message is prefixed with `[PAPER]` and positions are simulated.
- For every signal (acted on or not), record outcomes at +24h, +72h and +7d: return %, maximum favourable move, maximum adverse move, and whether target or stop would have hit first.
- Compare AI recommendations against a **baseline rule** (enter on every rule signal) so the user can see whether the AI adds value.
- `/stats` shows these comparisons, and when two AI providers/models are configured, compares them side by side.

---



## 6. Data model (minimum)

- **signals**: id, symbol, timestamp, price, snapshot_json, rule_values_json, high_risk flag, ai_advice_json, user_action (entered/ignored/none)
- **positions**: id, symbol, entry_time, entry_price, amount_usdt, quantity, stop_price, targets_hit, status (open/closed), exit_time, exit_price, realised_pnl_usdt, realised_pnl_pct, fees_usdt, linked_signal_id, is_paper
- **ai_calls**: id, timestamp, provider, model, purpose, prompt, response, latency_ms, tokens_in, tokens_out, valid_json
- **signal_outcomes**: signal_id, horizon, return_pct, max_favourable_pct, max_adverse_pct, first_hit (target/stop/none)
- **alerts_sent**: id, position_id or signal_id, alert_type, timestamp (prevents duplicate alerts)

---



## 7. Non-functional requirements

- **Security:** secrets only in `.env`; `.env` in `.gitignore`; Binance key with no withdrawal permission and IP whitelist; Telegram chat ID allowlist.
- **Reliability:** survives restarts (state in SQLite), reconnects streams automatically, sends a Telegram notice on startup, shutdown and unhandled errors.
- **Accuracy:** all P/L includes fees; prices use Binance's symbol precision/step size.
- **Observability:** structured logs for every signal, alert, AI call and error.
- **Testability:** indicators, rules and sizing are pure functions with unit tests; exchange and AI clients are mockable.
- **Messaging:** every AI-based message ends with a short note that it's not financial advice and the decision is the user's.

---



## 8. Build phases (implement in order; stop and confirm after each)

1. **Binance read-only connection** — print live SOL/LINK/BTC prices and balances in the terminal.
  *Done when:* prices stream live and balances match the Binance app.
2. **Telegram skeleton** — `/start`, `/price`, `/balance`, chat ID allowlist.
  *Done when:* commands work from the phone and other users are ignored.
3. **Indicator engine + snapshot** — `/analysis <SYMBOL>`.
  *Done when:* values match Binance/TradingView charts within reasonable tolerance; tests pass.
4. **Rule-based dip alerts + sizing** — alerts with buttons, cooldowns, BTC crash guard, signal logging.
  *Done when:* alerts fire correctly in paper mode and nothing spams.
5. **Position tracker** — record entries, profit target and stop alerts, `/positions`, `/close`, `/history`.
  *Done when:* a simulated position triggers each alert exactly once.
6. **AI layer** — entry and exit advice with JSON validation, rate limits and logging.
  *Done when:* alerts include AI advice, and failures degrade gracefully to rule-only alerts.
7. **Evaluation** — signal outcomes, `/stats`, baseline and model comparison.
8. **Deployment** — VPS setup, `systemd` service or Docker, log rotation, auto-restart.
9. **(Optional, later)** Trade sync from Binance history; then order execution with explicit confirmation, spot-only key, and a hard per-trade USDT limit.

Recommended: run in `paper` mode for 2–4 weeks after Phase 7 before relying on it with real money.