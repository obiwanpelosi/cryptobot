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


## Deploying

The bot runs 24/7 on a VPS in Docker (`Dockerfile`, `docker-compose.yml`): it restarts after
crashes and reboots, rotates its logs, and backs up its database nightly.
Step-by-step guide: [deploy/DEPLOY.md](deploy/DEPLOY.md).

```bash
docker compose up -d --build     # build and start
docker compose ps                # running? healthy?
docker compose logs -f           # live logs
deploy/update.sh                 # on the server: pull + rebuild + restart
```
