# cryptobot

An advisory Telegram bot that sends dip-buying alerts for Binance spot SOL/USDT and LINK/USDT.
It never places an order on its own. See [requirements.md](requirements.md) for the full spec
and the build phases.

## Setup

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 automatically.

```bash
uv sync                      # create .venv and install dependencies
cp .env.example .env         # then fill in your API keys (.env is git-ignored)
```

Strategy parameters are in `config.yaml`.

## Run

```bash
uv run python -m bot.main    # logs to the console and to logs/bot.log
uv run pytest                # tests
uv run ruff check .          # lint
```

Deploying from a `requirements.txt`: `uv export --no-dev > requirements.txt`.
