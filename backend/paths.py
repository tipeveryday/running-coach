"""Where the app keeps per-user data. Everything personal (config, plan,
Garmin cache, coach state) lives under one git-ignored folder:

  data/        your real setup (default)
  demo-data/   RC_DEMO=1 — synthetic athlete, regenerated on start

Override with RC_DATA_DIR.
"""
from __future__ import annotations
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO = os.environ.get("RC_DEMO") == "1"
DATA_DIR = os.environ.get("RC_DATA_DIR") or os.path.join(ROOT, "demo-data" if DEMO else "data")
CACHE_DIR = os.environ.get("CACHE_DIR") or os.path.join(DATA_DIR, "cache")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
PLAN_PATH = os.path.join(DATA_DIR, "plan.json")
