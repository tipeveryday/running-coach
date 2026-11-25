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
# Look like a browser on this machine: with no APP_PASSWORD the app only answers loopback.
client = TestClient(app.app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50000))

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


# ---------------- security ----------------
def test_no_password_mode_only_answers_this_machine():
    # DNS rebinding: a page whose own domain resolves to 127.0.0.1 must not read the API
    assert client.get("/api/dashboard", headers={"Host": "evil.example:8000"}).status_code == 403
    other_device = TestClient(app.app, base_url="http://127.0.0.1:8000", client=("192.168.1.50", 50000))
    assert other_device.get("/api/dashboard").status_code == 403


def test_blocks_cross_site_writes():
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"}):
        assert client.post("/api/refresh", headers=headers).status_code == 403, headers
    same = {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"}
    assert client.post("/api/coach/chat", json={"message": "paces"}, headers=same).status_code == 200


def test_security_headers():
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self' 'sha256-" in csp and "frame-ancestors 'none'" in csp
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"


def test_password_login_and_rate_limit():
    saved = app.APP_PASSWORD, app.AUTH_TOKEN
    app.APP_PASSWORD, app.AUTH_TOKEN = "correct horse", app._auth_token("correct horse")
    app._login_failures.clear()
    try:
        phone = TestClient(app.app, base_url="http://127.0.0.1:8000", client=("10.0.0.9", 50000))
        assert phone.get("/api/dashboard").status_code == 401
        for _ in range(app._LOGIN_MAX_PER_CLIENT):
            assert phone.post("/api/login", json={"password": "wrong"}).status_code == 401
        r = phone.post("/api/login", json={"password": "correct horse"})
        assert r.status_code == 429 and int(r.headers["retry-after"]) > 0
        app._login_failures.clear()
        assert phone.post("/api/login", json={"password": "correct horse"}).status_code == 200
        assert phone.get("/api/dashboard").status_code == 200
    finally:
        app.APP_PASSWORD, app.AUTH_TOKEN = saved
        app._login_failures.clear()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok ", name)
