"""Test-suite setup: use a fixed strategy config, independent of your own config.yaml."""

import os
from pathlib import Path

os.environ["BOT_CONFIG"] = str(Path(__file__).parent / "fixtures" / "strategy.yaml")
