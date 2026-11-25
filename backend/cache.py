"""Local JSON cache of the Garmin snapshot with a fetched-at timestamp."""
from __future__ import annotations
import json
import os
import datetime as dt
from typing import Optional

from paths import CACHE_DIR
CACHE_FILE = os.path.join(CACHE_DIR, "garmin_snapshot.json")
BASELINE_FILE = os.path.join(CACHE_DIR, "baselines.json")


def load_baselines() -> Optional[dict]:
    if not os.path.exists(BASELINE_FILE):
        return None
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_baselines(data: dict) -> None:
    ensure_dir()
    tmp = BASELINE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, default=str)
    os.replace(tmp, BASELINE_FILE)


def ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def load() -> Optional[dict]:
    if not os.path.exists(CACHE_FILE):
        return None
    try:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save(snapshot: dict) -> None:
    ensure_dir()
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, default=str)
    os.replace(tmp, CACHE_FILE)


def age_seconds(snapshot: Optional[dict]) -> Optional[float]:
    if not snapshot or "fetched_at" not in snapshot:
        return None
    try:
        t = dt.datetime.fromisoformat(snapshot["fetched_at"])
        return (dt.datetime.now() - t).total_seconds()
    except Exception:
        return None
