"""Smoke tests: every API endpoint answers in demo mode, and the plan
generator handles each race distance.

    uv run --group dev python -m pytest tests      (or just: uv run python tests/test_smoke.py)
"""
import datetime as dt
import os
import sys
import tempfile

os.environ["RC_DEMO"] = "1"
os.environ["RC_DATA_DIR"] = tempfile.mkdtemp(prefix="rc-test-")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import app  # noqa: E402
import demo  # noqa: E402
import planlib  # noqa: E402
import plangen  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

demo.ensure_demo_data(app._today())
client = TestClient(app.app)

GET_ENDPOINTS = [
    "/api/health", "/api/setup", "/api/config", "/api/plan", "/api/dashboard", "/api/coach/today",
    "/api/coach/last-run", "/api/coach/progress", "/api/coach/sleep", "/api/coach/fitness",
    "/api/fitness/stamina", "/api/fitness/heat", "/api/fitness/tolerance", "/api/coach/activity",
    "/api/coach/recovery", "/api/coach/routines", "/api/log", "/api/coach/recommendations",
    "/api/niggles", "/api/environment/today",
]


def test_endpoints():
    for path in GET_ENDPOINTS:
        r = client.get(path)
        assert r.status_code == 200, (path, r.status_code, r.text[:200])


def test_activity_stream():
    aid = app.get_snapshot()["activities"][-1]["id"]
    r = client.get(f"/api/activity/{aid}/stream")
    assert r.status_code == 200 and r.json()["has_data"]


def test_chat_rules():
    for msg in ["what should I run today?", "paces", "how did my last run go?"]:
        r = client.post("/api/coach/chat", json={"message": msg})
        assert r.status_code == 200 and r.json().get("kind"), msg


def test_plan_generator_all_distances():
    race = (dt.date.today() + dt.timedelta(weeks=16)).isoformat()
    for dist, goal in [("5k", "24:00"), ("10k", "50:00"), ("half", "1:50:00"), ("marathon", "3:45:00")]:
        cfg, plan = plangen.generate({"distance": dist, "race_date": race, "goal_time": goal,
                                      "current_km": 30, "peak_km": 50, "runs_per_week": 5})
        days = [d for w in plan["weeks"] for d in w["days"]]
        assert days[-1]["date"] >= race or any(d["type"] == "race" for d in days)
        assert any(d["type"] == "race" and d["date"] == race for d in days), dist
        for d in days:   # every workout parses into blueprint phases
            if d["type"] == "quality":
                assert planlib.build_phases(d, cfg), (dist, d["title"])


def test_setup_rejects_bad_input():
    r = client.post("/api/setup/preview", json={"distance": "half", "race_date": "2000-01-01", "goal_time": "1:50:00"})
    assert r.status_code == 400


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
