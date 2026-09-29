"""Heartbeat file for the Docker healthcheck.

The bot's reporter loop writes data/heartbeat every 10 s. `python -m bot.health` exits 1
(unhealthy) if the heartbeat is missing, older than 3 minutes, or says the price stream is stale.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from bot.settings import PROJECT_ROOT

HEARTBEAT_PATH = PROJECT_ROOT / "data" / "heartbeat"
MAX_AGE_SECONDS = 180


def write_heartbeat(path: Path = HEARTBEAT_PATH, *, stream_stale: bool, now: float | None = None):
    now = time.time() if now is None else now
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": now, "stream_stale": stream_stale}))
    tmp.replace(path)  # atomic, so the healthcheck never reads a half-written file


def check(
    path: Path = HEARTBEAT_PATH, *, max_age: float = MAX_AGE_SECONDS, now: float | None = None
) -> tuple[bool, str]:
    now = time.time() if now is None else now
    try:
        data = json.loads(path.read_text())
        age = now - float(data["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return False, "no heartbeat yet (starting up, or the bot isn't running)"
    if age > max_age:
        return False, f"heartbeat is {age:.0f}s old (the bot's main loop is stuck or stopped)"
    if data.get("stream_stale"):
        return False, "running, but no price data for over 5 minutes"
    return True, f"ok (heartbeat {age:.0f}s ago)"


def main() -> int:
    ok, message = check()
    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
