"""One live AI call with the current strong model, to check your OpenRouter setup.

    uv run python -m bot.ai.check SOL
"""

from __future__ import annotations

import asyncio
import sys

from bot.ai import compare
from bot.ai.models import active_model
from bot.settings import PROJECT_ROOT, load_settings
from bot.storage.db import Repo, make_engine


def main() -> int:
    symbol = sys.argv[1] if len(sys.argv) > 1 else "SOL"
    settings = load_settings()
    repo = Repo(make_engine(PROJECT_ROOT / "data" / "bot.db"))
    model = active_model(repo, settings.strategy.ai, "strong")
    return asyncio.run(compare.main(["--live", symbol, "--models", model, "--yes", "--full"]))


if __name__ == "__main__":
    sys.exit(main())
