# cryptobot: one async process (Binance streams + Telegram + AI), see requirements.md.

# --- build stage: resolve dependencies with uv -------------------------------------------
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Exact versions from uv.lock, no test tools.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime stage: just Python, the virtualenv and the code (no uv) ------------------------
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app

# Non-root user. UID matches the host user that owns ./data and ./logs.
ARG UID=1000
RUN useradd --system --uid "${UID}" --create-home bot && mkdir -p data logs

COPY --from=build /app/.venv /app/.venv
COPY bot ./bot
# Default config; normally mounted over this at runtime (see docker-compose.yml).
COPY config.yaml ./
RUN chown -R bot:bot /app/data /app/logs
USER bot

# Unhealthy if the heartbeat (written every 10 s) is >3 min old or prices are stale.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD ["python", "-m", "bot.health"]

# Python is PID 1, so `docker stop` (SIGTERM) reaches the bot's clean-shutdown handler.
STOPSIGNAL SIGTERM
CMD ["python", "-m", "bot.main"]
