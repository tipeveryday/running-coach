"""FastAPI app: serves the dashboard, exposes Garmin data + coach endpoints,
and refreshes from Garmin on demand (cached to local JSON so the UI is fast
and works offline).

Run:  uv run python backend/app.py          (your data, in data/)
      uv run python backend/app.py --demo   (synthetic demo athlete)
Then open http://127.0.0.1:8000
"""
from __future__ import annotations
import os
import sys
import datetime as dt
import threading
import traceback

sys.path.insert(0, os.path.dirname(__file__))

# Lightweight .env loader (no external dep). Reads KEY=VALUE lines from a .env
# file in the project root and populates os.environ for any keys not already
# set. Runs BEFORE anything reads os.environ.
def _load_env_file():
    for path in (os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"),
                 os.path.join(os.path.dirname(__file__), ".env")):
        if not os.path.exists(path):
            continue
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip(chr(34)).strip(chr(39))
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception:
            pass
        break

_load_env_file()

# CLI flags (handy when env vars are awkward, e.g. on Windows):
#   --demo            synthetic athlete, no Garmin (same as RC_DEMO=1)
#   --port 8001       same as PORT=8001
if "--demo" in sys.argv:
    os.environ["RC_DEMO"] = "1"
if "--port" in sys.argv:
    os.environ["PORT"] = sys.argv[sys.argv.index("--port") + 1]
if "--today" in sys.argv:                         # pretend it's this date (YYYY-MM-DD)
    os.environ["COACH_TODAY"] = sys.argv[sys.argv.index("--today") + 1]
if "--offline" in sys.argv:                       # cached data only, never log in to Garmin
    os.environ["RC_OFFLINE"] = "1"
if "--data-dir" in sys.argv:
    os.environ["RC_DATA_DIR"] = sys.argv[sys.argv.index("--data-dir") + 1]

# Windows consoles default to cp1252 and choke on Unicode in log lines.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Body, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import cache
import planlib
import coach
import insights as insightslib
import sleep as sleeplib
import baselines as baselib
import fitness as fitnesslib
import activity as activitylib
import recovery as recoverylib
import paths
import plangen
from garmin_client import GarminData

BASE = os.path.dirname(os.path.dirname(__file__))
FRONTEND = os.path.join(BASE, "frontend")

app = FastAPI(title="Running Coach")
if paths.DEMO:
    import demo
    _gd = demo.DemoGarmin()
else:
    _gd = GarminData()
_refresh_lock = threading.Lock()
_refresh_state = {"running": False, "last_error": None, "last_finished": None}

# ---------------- optional password gate ----------------
# Off by default: the app binds to 127.0.0.1 and is meant for one person on
# their own machine. Set APP_PASSWORD in .env to require a login (e.g. when you
# expose it on your LAN). The cookie is an HMAC of the password under a random
# server-side secret: it can't be reversed to the password, and changing the
# password signs every browser out.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
AUTH_COOKIE = "rc_auth"
_COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"
# Without a password the app only answers your own machine. Set this to 1 to
# allow other devices anyway (not recommended: anyone on the network sees your data).
ALLOW_NO_PASSWORD = os.environ.get("RC_ALLOW_NO_PASSWORD") == "1"
# Extra host names the app may be reached by (comma-separated, "*" = any).
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()}
# API paths that stay open so the login screen can function
_AUTH_EXEMPT = {"/api/login", "/api/logout", "/api/auth"}
# Until onboarding has written a config + plan, only these API paths work.
_SETUP_OPEN = {"/api/setup", "/api/setup/preview", "/api/health", "/api/auth", "/api/login", "/api/logout"}


def _session_secret() -> bytes:
    """AUTH_SALT if set, else a random secret created once in the data folder."""
    if os.environ.get("AUTH_SALT"):
        return os.environ["AUTH_SALT"].encode()
    path = os.path.join(paths.DATA_DIR, ".session_secret")
    try:
        with open(path, "rb") as f:
            key = f.read().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32).encode()
    try:
        os.makedirs(paths.DATA_DIR, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except OSError:
        pass   # keep it in memory only: sessions end when the server restarts
    return key


def _auth_token(password: str) -> str:
    return hmac.new(_session_secret(), password.encode(), hashlib.sha256).hexdigest() if password else ""


AUTH_TOKEN = _auth_token(APP_PASSWORD)


def _is_authed(request: Request) -> bool:
    if not APP_PASSWORD:
        return True
    return hmac.compare_digest(request.cookies.get(AUTH_COOKIE, "").encode(), AUTH_TOKEN.encode())


# Brute-force guard for /api/login: 5 wrong passwords per client per 15 minutes,
# and 50 across all clients, then 429 until the window rolls on.
_LOGIN_WINDOW_S = 15 * 60
_LOGIN_MAX_PER_CLIENT = 5
_LOGIN_MAX_TOTAL = 50
_login_failures: dict = {}
_login_lock = threading.Lock()


def _login_retry_after(client: str) -> int:
    """Seconds this client must wait before another attempt (0 = go ahead)."""
    now = time.monotonic()
    with _login_lock:
        for k in list(_login_failures):
            _login_failures[k] = [t for t in _login_failures[k] if now - t < _LOGIN_WINDOW_S]
            if not _login_failures[k]:
                del _login_failures[k]
        mine = _login_failures.get(client, [])
        every = sorted(t for v in _login_failures.values() for t in v)
        if len(mine) >= _LOGIN_MAX_PER_CLIENT:
            return int(_LOGIN_WINDOW_S - (now - mine[0])) + 1
        if len(every) >= _LOGIN_MAX_TOTAL:
            return int(_LOGIN_WINDOW_S - (now - every[0])) + 1
    return 0


def _login_failed(client: str) -> None:
    with _login_lock:
        _login_failures.setdefault(client, []).append(time.monotonic())


# ---------------- request checks + security headers ----------------
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def _is_loopback_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _host_name(value: str) -> str:
    """'Example.com:8000' -> 'example.com', '[::1]:8000' -> '::1'."""
    v = (value or "").strip().lower()
    if v.startswith("["):
        return v[1:v.index("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _host_allowed(request: Request) -> bool:
    """Blocks DNS rebinding: a web page that points its own domain at 127.0.0.1
    and then reads the API. Without a password only loopback names (plus
    ALLOWED_HOSTS) are answered; with one, the cookie already protects the API."""
    if "*" in ALLOWED_HOSTS:
        return True
    host = _host_name(request.headers.get("host", ""))
    if host in ALLOWED_HOSTS:
        return True
    if APP_PASSWORD and not ALLOWED_HOSTS:
        return True
    return host in _LOOPBACK_NAMES or host.endswith(".localhost")


def _client_allowed(request: Request) -> bool:
    """With no password, only this machine may connect (however the server was started)."""
    if APP_PASSWORD or ALLOW_NO_PASSWORD or request.client is None:
        return True
    return _is_loopback_ip(request.client.host)


def _cross_site_write(request: Request) -> bool:
    """Refuse state-changing requests sent by other websites (CSRF), e.g. a page
    you visit posting to http://127.0.0.1:8000/api/refresh."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    site = request.headers.get("sec-fetch-site")
    if site:
        return site not in ("same-origin", "none")
    origin = request.headers.get("origin")
    if origin is None:
        return False   # not a browser (curl, scripts)
    netloc = urlsplit(origin).netloc.lower()
    expected = {(request.headers.get(h) or "").lower() for h in ("host", "x-forwarded-host")}
    return not netloc or netloc not in expected


def _content_security_policy() -> str:
    """Scripts only from this server, plus the inline theme snippet in index.html (by hash)."""
    try:
        with open(os.path.join(FRONTEND, "index.html"), encoding="utf-8") as f:
            html = f.read()
    except OSError:
        html = ""
    hashes = " ".join("'sha256-%s'" % base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
                      for body in re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    return ("default-src 'self'; "
            f"script-src 'self' {hashes}; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


_CSP = _content_security_policy()


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if not _client_allowed(request):
        return JSONResponse({"error": "No APP_PASSWORD is set, so only this computer can open the app."},
                            status_code=403)
    if not _host_allowed(request):
        return JSONResponse({"error": "Host not allowed. Add it to ALLOWED_HOSTS in .env."}, status_code=403)
    if _cross_site_write(request):
        return JSONResponse({"error": "cross_site_request"}, status_code=403)
    # Only the data API is gated; the static shell (HTML/JS/CSS) is harmless and
    # renders the login screen. All real data lives behind /api/*.
    if path.startswith("/api/") and path not in _AUTH_EXEMPT and not _is_authed(request):
        return JSONResponse({"error": "auth_required"}, status_code=401)
    if path.startswith("/api/") and path not in _SETUP_OPEN and not planlib.is_configured():
        return JSONResponse({"error": "setup_required"}, status_code=409)
    response = await call_next(request)
    # The frontend is unbundled ES modules (/static/ui/*.js) with no version in
    # their URLs, so make browsers revalidate (cheap 304 via ETag) instead of
    # heuristically reusing a stale copy.
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if not path.startswith("/static/design/"):   # the design specimens load fonts/icons from CDNs
        response.headers["Content-Security-Policy"] = _CSP
    return response


@app.post("/api/login")
def api_login(request: Request, payload: dict = Body(default={})):
    if not APP_PASSWORD:
        return JSONResponse({"ok": True})
    client = request.client.host if request.client else "unknown"
    wait = _login_retry_after(client)
    if wait:
        return JSONResponse({"ok": False, "error": "Too many attempts. Try again in a few minutes."},
                            status_code=429, headers={"Retry-After": str(wait)})
    password = (payload or {}).get("password")
    if isinstance(password, str) and hmac.compare_digest(password.encode(), APP_PASSWORD.encode()):
        resp = JSONResponse({"ok": True})
        resp.set_cookie(AUTH_COOKIE, AUTH_TOKEN, httponly=True, samesite="strict",
                        secure=_COOKIE_SECURE or request.url.scheme == "https",
                        max_age=60 * 60 * 24 * 30)
        return resp
    _login_failed(client)
    return JSONResponse({"ok": False, "error": "Incorrect password."}, status_code=401)


@app.post("/api/logout")
def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(AUTH_COOKIE)
    return resp


@app.get("/api/auth")
def api_auth(request: Request):
    return {"authenticated": _is_authed(request), "password_required": bool(APP_PASSWORD)}


def _today() -> dt.date:
    # Allow overriding "today" for testing via env. COACH_TZ (e.g.
    # "America/New_York") pins the athlete's local date when the server's clock
    # is in another zone; otherwise the machine's local date is used.
    override = os.environ.get("COACH_TODAY")
    if override:
        try:
            return dt.date.fromisoformat(override)
        except Exception:
            pass
    tz = os.environ.get("COACH_TZ")
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(tz)).date()
        except Exception:
            pass
    return dt.date.today()


def do_refresh() -> dict:
    with _refresh_lock:
        _refresh_state["running"] = True
        _refresh_state["last_error"] = None
        try:
            snap = _gd.build_snapshot(_today())
            cache.save(snap)
            _refresh_state["last_finished"] = dt.datetime.now().isoformat(timespec="seconds")
            return snap
        except Exception as e:  # noqa: BLE001
            _refresh_state["last_error"] = f"{type(e).__name__}: {e}"
            traceback.print_exc()
            raise
        finally:
            _refresh_state["running"] = False


def get_snapshot() -> dict | None:
    return cache.load()


# ---------------- onboarding / setup ----------------
def _setup_suggestions() -> dict:
    """Prefill hints for the setup form from whatever Garmin has synced."""
    snap = get_snapshot() or {}
    out = {}
    lt = snap.get("lactate_threshold") or {}
    if lt.get("hr"):
        out["lthr"] = lt["hr"]
    w = snap.get("weight") or []
    if w and w[-1].get("kg"):
        out["weight_lb"] = round(w[-1]["kg"] * 2.20462)
    vols = [x.get("km") or 0 for x in (snap.get("weekly_volume") or [])[-5:-1]]
    if vols:
        out["current_km"] = round(sum(vols) / len(vols))
    mx = [a.get("max_hr") for a in snap.get("activities") or [] if a.get("max_hr")]
    if mx:
        out["max_hr"] = int(max(mx))
    return out


@app.get("/api/setup")
def api_setup_get():
    cfg = planlib.load_config() if planlib.is_configured() else {}
    return {"configured": planlib.is_configured(), "demo": paths.DEMO,
            "profile": cfg.get("profile"), "suggestions": _setup_suggestions(),
            "distances": {k: {"km": v["km"], "name": v["name"]} for k, v in plangen.DISTANCES.items()}}


def _plan_summary(cfg: dict, plan: dict) -> dict:
    # bar height = training volume, so the race itself doesn't make race week look like a peak
    weeks = [{"week": w["week"], "start_date": w["start_date"], "phase": w["phase"],
              "km": round(sum(d.get("distance_km") or 0 for d in w["days"] if d["type"] != "race")),
              "long_km": max((d.get("distance_km") or 0 for d in w["days"] if d["type"] in ("long", "race")), default=0),
              "quality": [d["title"] for d in w["days"] if d["type"] == "quality"]}
             for w in plan["weeks"]]
    return {"weeks": weeks, "goals": cfg["goals"], "paces": cfg["paces"],
            "hr_zones": cfg["hr_zones"]["zones"], "peak_km": max(w["km"] for w in weeks)}


@app.post("/api/setup/preview")
def api_setup_preview(payload: dict = Body(...)):
    try:
        cfg, plan = plangen.generate(payload, _today())
    except (ValueError, KeyError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, **_plan_summary(cfg, plan)}


@app.post("/api/setup")
def api_setup_save(payload: dict = Body(...)):
    try:
        cfg, plan = plangen.generate(payload, _today())
    except (ValueError, KeyError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    plangen.save(cfg, plan)
    return {"ok": True, **_plan_summary(cfg, plan)}


# ---------------- API ----------------
@app.get("/healthz")
def healthz():
    """Unauthenticated liveness probe (not under /api/, so the gate doesn't block it)."""
    return {"ok": True}


@app.get("/api/health")
def health():
    snap = get_snapshot()
    return {
        "ok": True,
        "has_cache": snap is not None,
        "fetched_at": (snap or {}).get("fetched_at"),
        "cache_age_seconds": cache.age_seconds(snap),
        "refresh": _refresh_state,
        "tokenstore": _gd.tokenstore,
        "tokenstore_exists": os.path.exists(os.path.expanduser(_gd.tokenstore)),
    }


@app.get("/api/config")
def api_config():
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    cfg["countdown"] = planlib.weeks_to_race(_today())
    return cfg


@app.get("/api/plan")
def api_plan():
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    plan = planlib.load_plan()
    enriched = planlib.enrich_plan(plan, cfg)
    enriched["countdown"] = planlib.weeks_to_race(_today())
    enriched["paces"] = planlib.derived_paces(cfg, get_snapshot())
    return enriched


@app.get("/api/dashboard")
def api_dashboard():
    snap = get_snapshot()
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    if snap is None:
        return {
            "has_data": False,
            "message": ("No Garmin data yet. Press Refresh to pull your data." if _garmin_connected()
                        else GARMIN_CONNECT_HELP),
            "countdown": planlib.weeks_to_race(_today()),
            "config": cfg,
        }
    return {
        "has_data": True,
        "fetched_at": snap.get("fetched_at"),
        "cache_age_seconds": cache.age_seconds(snap),
        "countdown": planlib.weeks_to_race(_today()),
        "config": cfg,
        "paces": planlib.derived_paces(cfg, snap),
        "snapshot": snap,
    }


GARMIN_CONNECT_HELP = ("Garmin isn't connected yet. In a terminal, run  uv run python garmin_login.py  "
                       "in the project folder, then press Refresh.")


def _garmin_connected() -> bool:
    if paths.DEMO:
        return True
    ts = os.path.expanduser(_gd.tokenstore)
    return os.path.isdir(ts) and any(f.endswith(".json") for f in os.listdir(ts))


@app.post("/api/refresh")
def api_refresh():
    if not _garmin_connected():
        return JSONResponse({"ok": False, "error": GARMIN_CONNECT_HELP, "code": "garmin_not_connected"}, status_code=400)
    if _refresh_state["running"]:
        return JSONResponse({"ok": False, "message": "Refresh already in progress."}, status_code=409)
    try:
        snap = do_refresh()
        return {"ok": True, "fetched_at": snap.get("fetched_at"),
                "errors": snap.get("errors", []),
                "counts": {
                    "runs": len(snap.get("activities", [])),
                    "weeks": len(snap.get("weekly_volume", [])),
                    "sleep_nights": len(snap.get("sleep", [])),
                    "hrv_nights": len(snap.get("hrv", [])),
                    "vo2_points": len(snap.get("vo2max_trend", [])),
                }}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)


@app.get("/api/coach/today")
def api_coach_today():
    return coach.todays_verdict(get_snapshot(), _today(), stream_provider=_activity_stream)


_feel_cache: dict = {}


def _activity_feel(activity_id):
    if activity_id in _feel_cache:
        return _feel_cache[activity_id]
    fr = _gd.activity_feel(activity_id)
    _feel_cache[activity_id] = fr
    return fr


@app.get("/api/day/{date}")
def api_day_context(date: str):
    """Full context for one calendar day — that morning's sleep + recovery, what the
    coach's read was, and the athlete's post-run Feel / Perceived-Effort tags."""
    return coach.day_context(get_snapshot(), date, feel_provider=_activity_feel)


@app.get("/api/coach/last-run")
def api_coach_last_run(offset: int = 0):
    a = coach.analyze_last_run(get_snapshot(), stream_provider=_activity_stream, offset=offset)
    if a is None:
        return {"has_data": False, "message": "No recent run found. Refresh from Garmin after your next run."}
    a["has_data"] = True
    return a


@app.get("/api/coach/progress")
def api_coach_progress():
    return coach.progress(get_snapshot(), _today())


@app.get("/api/coach/sleep")
def api_coach_sleep():
    return sleeplib.analyze_sleep(get_snapshot(), _today())


@app.get("/api/coach/fitness")
def api_coach_fitness():
    return fitnesslib.analyze_fitness(get_snapshot(), _today())


_raw_stream_cache: dict = {}


def _raw_stream(activity_id):
    if activity_id in _raw_stream_cache:
        return _raw_stream_cache[activity_id]
    s = _gd.activity_stream(activity_id)
    _raw_stream_cache[activity_id] = s
    return s


@app.get("/api/fitness/stamina")
def api_fitness_stamina():
    """Garmin Real-Time Stamina: the depletion curve for the most recent run
    (available + potential stamina, 0–100%) plus a trend of how much stamina was
    left at the finish of recent runs. Lazily fetches recent activity streams."""
    snap = get_snapshot() or {}
    runs = [a for a in snap.get("activities", []) if a.get("date") and a.get("id")]
    runs.sort(key=lambda r: r.get("start") or r.get("date"), reverse=True)
    trend, curve = [], None
    for r in runs[:8]:
        try:
            s = _raw_stream(r["id"])
        except Exception:
            continue
        series = [p for p in (s.get("series") or []) if p.get("stamina") is not None]
        if len(series) < 5:
            continue
        stam = [p["stamina"] for p in series]
        pot = [p["potential"] for p in series if p.get("potential") is not None]
        trend.append({
            "date": r.get("date"), "name": r.get("name"), "distance_km": r.get("distance_km"),
            "start_potential": round(max(pot)) if pot else None,
            "end_available": round(stam[-1]), "end_potential": round(pot[-1]) if pot else None,
            "min_available": round(min(stam)),
        })
        if curve is None:
            step = max(1, len(series) // 120)
            curve = {"date": r.get("date"), "name": r.get("name"),
                     "series": [{"t": p["t"], "stamina": p["stamina"], "potential": p.get("potential")}
                                for p in series[::step]]}
    trend.reverse()
    return {"has_data": bool(curve), "curve": curve, "trend": trend}


@app.get("/api/fitness/heat")
def api_heat_acclimation():
    """Heat-acclimation trend (0–100%). Garmin exposes it per-day inside training
    status, so we sample it weekly over the last ~3 months to draw the trend."""
    g = _gd.client()
    end = _today()
    pts, cur = [], None
    d = end
    for _ in range(13):
        try:
            ts = g.get_training_status(d.isoformat())
            h = ((ts or {}).get("mostRecentVO2Max") or {}).get("heatAltitudeAcclimation") or {}
            hp = h.get("heatAcclimationPercentage")
            if hp is not None:
                pts.append({"date": d.isoformat(), "heat_pct": hp})
                if cur is None:
                    cur = {"heat_pct": hp, "trend": h.get("heatTrend"),
                           "alt_pct": h.get("altitudeAcclimation")}
        except Exception:
            pass
        d = d - dt.timedelta(days=7)
    pts.reverse()
    return {"has_data": bool(pts), "current": cur, "series": pts}


@app.get("/api/fitness/tolerance")
def api_running_tolerance():
    """Running Tolerance — a newer-Garmin metric (empty on watches that don't record impact load).
    Garmin tracks weekly cumulative Impact Load against a personal tolerance ceiling
    to guard against ramping faster than the body adapts."""
    g = _gd.client()
    end = _today()
    start = end - dt.timedelta(days=120)
    try:
        rows = g.get_running_tolerance(start.isoformat(), end.isoformat()) or []
    except Exception as e:  # noqa: BLE001
        return {"has_data": False, "error": f"{type(e).__name__}: {e}"}
    weeks = []
    for r in rows:
        load = r.get("totalImpactLoad") or 0
        tol = r.get("tolerance") or 0
        if not tol:
            continue
        weeks.append({
            "date": r.get("calendarDate"),
            "week_start": r.get("startOfWeek"), "week_end": r.get("endOfWeek"),
            "impact_load": round(load), "tolerance": round(tol),
            "distance_km": round((r.get("totalDistance") or 0) / 1000.0, 1),
            "ratio": round(load / tol, 2) if tol else None,
        })
    if not weeks:
        return {"has_data": False}
    tstr = end.isoformat()
    # Headline off the last COMPLETE week (week_end before today); the in-progress
    # week is week-to-date and would understate. Fall back to the latest row.
    complete = [w for w in weeks if (w.get("week_end") or "") < tstr]
    head = complete[-1] if complete else weeks[-1]
    in_prog = weeks[-1] if (weeks[-1].get("week_end") or "") >= tstr and weeks[-1] is not head else None
    ratio = head["ratio"] or 0
    if ratio >= 1.0:
        tone, verdict = "red", "over tolerance"
        read = (f"Last full week's impact load ({head['impact_load']:,}) ran above your current tolerance "
                f"({head['tolerance']:,}) — you loaded the tissues faster than they adapt, the classic setup for "
                f"bone/tendon niggles. Bank an easier week and let tolerance catch up before the next build.")
    elif ratio >= 0.8:
        tone, verdict = "amber", "near capacity"
        read = (f"Last full week's impact load ({head['impact_load']:,}) sat close to your tolerance "
                f"({head['tolerance']:,}). Fine for a planned build week — just hold form and avoid stacking big "
                f"downhills or hard surfaces on top.")
    else:
        tone, verdict = "green", "within capacity"
        read = (f"Last full week's impact load ({head['impact_load']:,}) sat comfortably under your tolerance "
                f"({head['tolerance']:,}) — room to add volume without outrunning your body's adaptation.")
    return {"has_data": True, "current": head, "this_week": in_prog, "series": weeks,
            "tone": tone, "verdict": verdict, "read": read,
            "note": ("Tolerance keeps calibrating over your first few weeks on a watch that records impact load, "
                     "so read the trend, not one week.")}


@app.get("/api/coach/activity")
def api_coach_activity():
    return activitylib.analyze_activity(get_snapshot(), _today())


_stream_cache: dict = {}


def _steady_window(series):
    """The steady portion of a run for the durability read: drop the warm-up ramp
    (HR still rising to steady state) and cut off at the onset of any late strides
    or finishing surges — those spike HR at the end and would masquerade as
    cardiac drift on an otherwise-easy run."""
    pts = [p for p in series if p.get("pace_s") and p.get("hr")]
    if len(pts) < 30:
        return pts
    paces = sorted(p["pace_s"] for p in pts)
    med = paces[len(paces) // 2]
    cut = med * 0.82                                       # clearly faster than the run's norm = a surge
    n = len(pts)
    end = n
    for i in range(int(n * 0.55), n):                     # scan only the back portion for strides
        if pts[i]["pace_s"] < cut:
            end = i
            break
    win = pts[int(n * 0.12):end]                          # also drop the warm-up ramp
    return win if len(win) >= 24 else pts


def _has_sustained_work(series):
    """True only if the run contains a genuinely sustained hard block (tempo /
    interval rep ≥ ~2.5 min) — as opposed to short strides. Gates the work-drift
    read so an easy run finished with strides doesn't trigger it."""
    pts = [p for p in series if p.get("pace_s")]
    if len(pts) < 20:
        return False
    paces = sorted(p["pace_s"] for p in pts)
    med = paces[len(paces) // 2]
    cut = med * 0.88
    ts = [p.get("t") for p in series if p.get("t") is not None]
    span = (max(ts) - min(ts)) if len(ts) >= 2 else len(pts) * 10
    dt_pt = max(1.0, span / max(1, len(series)))
    best = cur = 0
    for p in pts:
        if p["pace_s"] < cut:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best * dt_pt >= 150


def _has_late_strides(series):
    """A steady run that finishes with short strides/surges (not a sustained work
    block) — so the durability read excluded them and should say so."""
    pts = [p for p in series if p.get("pace_s")]
    if len(pts) < 20 or _has_sustained_work(series):
        return False
    paces = sorted(p["pace_s"] for p in pts)
    med = paces[len(paces) // 2]
    late = pts[int(len(pts) * 0.6):]
    return any(p["pace_s"] < med * 0.82 for p in late)


def _decoupling(series):
    """Aerobic decoupling / cardiac drift: on a steady effort, does HR climb while
    pace holds? Compares output:HR efficiency in the first vs second half. >5% =
    the aerobic system is being stretched; a durability limiter."""
    pts = [p for p in series if p.get("pace_s") and 200 < p["pace_s"] < 700 and p.get("hr")]
    if len(pts) < 24:
        return None
    pts = pts[max(1, len(pts) // 20):]                 # drop the warm-up ramp
    speeds = [1000.0 / p["pace_s"] for p in pts]
    mean_sp = sum(speeds) / len(speeds)
    cv = (sum((s - mean_sp) ** 2 for s in speeds) / len(speeds)) ** 0.5 / mean_sp
    if cv > 0.12:                                       # not a steady effort (intervals) → skip
        return None
    h = len(pts) // 2
    a, b = pts[:h], pts[h:]

    def ef(chunk):
        sp = sum(1000.0 / p["pace_s"] for p in chunk) / len(chunk)
        hr = sum(p["hr"] for p in chunk) / len(chunk)
        return sp / hr, sp, hr

    e1, sp1, hr1 = ef(a)
    e2, sp2, hr2 = ef(b)
    if not e1:
        return None
    pct = round(100 * (e1 - e2) / e1, 1)
    hr_rise = round(hr2 - hr1)
    if pct < 5:
        verdict, tone = "Strong durability", "green"
    elif pct < 8:
        verdict, tone = "Mild drift", "amber"
    else:
        verdict, tone = "Significant decoupling", "red"
    return {
        "pct": pct, "verdict": verdict, "tone": tone, "hr_rise": hr_rise,
        "pace_first": round(1000.0 / sp1), "pace_second": round(1000.0 / sp2),
        "hr_first": round(hr1), "hr_second": round(hr2),
        "split_t": pts[h]["t"],
    }


def _work_drift(series):
    """For structured workouts (intervals/tempo) that aren't steady overall, run
    the cardiac-drift read on just the hard 'work' points: did HR creep up while
    pace held from the first reps to the last? Used when whole-run _decoupling
    finds no steady block."""
    pts = [p for p in series if p.get("pace_s") and 120 < p["pace_s"] < 650 and p.get("hr")]
    if len(pts) < 24:
        return None
    use_gap = all(p.get("gap_s") for p in pts)
    keyf = (lambda p: p["gap_s"]) if use_gap else (lambda p: p["pace_s"])
    vals = sorted(keyf(p) for p in pts)
    cut = vals[int(len(vals) * 0.45)]                      # fastest ~45% = the work
    work = [p for p in pts if keyf(p) <= cut]
    if len(work) < 16 or len(work) > 0.75 * len(pts):      # need a real, bounded hard block
        return None
    work.sort(key=lambda p: p.get("t") or 0)
    h = len(work) // 2
    a, b = work[:h], work[h:]

    def ef(chunk):
        sp = sum(1000.0 / p["pace_s"] for p in chunk) / len(chunk)
        hr = sum(p["hr"] for p in chunk) / len(chunk)
        return sp / hr, sp, hr

    e1, sp1, hr1 = ef(a)
    e2, sp2, hr2 = ef(b)
    if not e1:
        return None
    pct = round(100 * (e1 - e2) / e1, 1)
    if pct < 3:
        verdict, tone = "Held it strong", "green"
    elif pct < 6:
        verdict, tone = "Mild late drift", "amber"
    else:
        verdict, tone = "Faded late", "red"
    return {"pct": pct, "verdict": verdict, "tone": tone, "hr_rise": round(hr2 - hr1),
            "pace_first": round(1000.0 / sp1), "pace_second": round(1000.0 / sp2),
            "hr_first": round(hr1), "hr_second": round(hr2), "split_t": work[h].get("t"),
            "scope": "work"}


def _activity_stream(activity_id: int) -> dict:
    """Fetch + enrich an activity's pace/HR/GAP stream (zone tags + decoupling),
    cached. Shared by the stream endpoint and the coach's work-portion analysis."""
    if activity_id in _stream_cache:
        return _stream_cache[activity_id]
    s = _gd.activity_stream(activity_id)
    if s.get("has_data"):
        zones = (planlib.load_config().get("hr_zones") or {}).get("zones") or []

        def zone_of(hr):
            if not hr:
                return None
            for z in zones:
                if z["low"] <= hr <= z["high"]:
                    return z["zone"]
            return zones[-1]["zone"] if zones else None

        for p in s["series"]:
            p["zone"] = zone_of(p.get("hr"))
        s["zone_defs"] = zones
        # Durability read: first try steady-effort decoupling on the steady window
        # (warm-up + late strides removed). Only fall to the work-drift read when
        # there's a genuinely sustained hard block, so an easy-with-strides run
        # isn't flagged by its finishing strides.
        d = _decoupling(_steady_window(s["series"]))
        if d is None and _has_sustained_work(s["series"]):
            d = _work_drift(s["series"])
        if d and d.get("scope") != "work" and _has_late_strides(s["series"]):
            d["strides_excluded"] = True
        s["drift"] = d
        try:
            s["splits"] = _gd.activity_splits(activity_id)
        except Exception:
            s["splits"] = []
        # rich, type-specific coaching insights for this run
        try:
            snap = get_snapshot() or {}
            run = next((a for a in snap.get("activities", []) if a.get("id") == activity_id), None)
            if run:
                rd = run.get("date")
                cfg = planlib.resolve_active_race(
                    planlib.load_config(), dt.date.fromisoformat(rd) if rd else None)
                plan = planlib.load_plan()
                day = planlib.get_day(plan, rd)
                day = planlib.resolve_day(day, plan, cfg) if day else None
                s["insights"] = insightslib.build_insights(run, day, cfg, snap, stream=s)
        except Exception:
            traceback.print_exc()
            s["insights"] = []
        _stream_cache[activity_id] = s
    return s


@app.get("/api/activity/{activity_id}/stream")
def api_activity_stream(activity_id: int):
    """On-demand pace/HR time-series for the workout-phase chart, tagged by zone,
    with an aerobic-decoupling read."""
    return _activity_stream(activity_id)


@app.get("/api/coach/recovery")
def api_coach_recovery():
    return recoverylib.analyze_recovery(get_snapshot(), _today())


_baseline_lock = threading.Lock()


def _baselines_stale(days: int = 30) -> bool:
    b = cache.load_baselines()
    if not b or not b.get("built_at"):
        return True
    try:
        return (dt.datetime.now() - dt.datetime.fromisoformat(b["built_at"])).days >= days
    except Exception:
        return True


def _rebuild_baselines() -> None:
    if not _baseline_lock.acquire(blocking=False):
        return  # a build is already running
    try:
        data = baselib.build_baselines(_gd.client(), _today(),
                                       log=lambda *a: print("[baselines]", *a))
        cache.save_baselines(data)
        print("[baselines] rebuilt and cached")
    except Exception as e:  # noqa: BLE001
        print(f"[baselines] rebuild failed: {e}")
    finally:
        _baseline_lock.release()


@app.post("/api/baselines/rebuild")
def api_baselines_rebuild():
    threading.Thread(target=_rebuild_baselines, daemon=True).start()
    return {"ok": True, "message": "Rebuilding your 2-year HRV/RHR baselines in the background (~30–60s). "
                                   "Refresh the Sleep tab shortly."}


# ---------- Garmin sync (from the Calendar UI) ----------
_sync_lock = threading.Lock()
_sync_state = {"running": False, "last": None}


def _do_garmin_sync(weeks: int, kinds: tuple):
    import garmin_sync
    from io import StringIO
    buf = StringIO()
    _orig_print = print
    try:
        # capture the tool's stdout into a buffer so the UI can show a log
        import builtins
        builtins.print = lambda *a, **k: (_orig_print(*a, **k, file=buf), _orig_print(*a, **k))[1]
        garmin_sync.push(weeks_ahead=weeks, dry=False, kinds=kinds)
        _sync_state["last"] = {"ok": True, "at": dt.datetime.now().isoformat(timespec="seconds"),
                                "log": buf.getvalue()[-4000:]}
    except Exception as e:  # noqa: BLE001
        _sync_state["last"] = {"ok": False, "at": dt.datetime.now().isoformat(timespec="seconds"),
                                "error": f"{type(e).__name__}: {e}",
                                "log": buf.getvalue()[-4000:]}
    finally:
        builtins.print = _orig_print
        _sync_state["running"] = False


@app.get("/api/garmin/sync/preview")
def api_garmin_sync_preview(weeks: int = 2, kinds: str = "run,strength,mobility"):
    import garmin_sync
    k = tuple(x.strip() for x in kinds.split(",") if x.strip())
    items = garmin_sync.workouts_for_window(weeks_ahead=weeks, kinds=k)
    return {"weeks": weeks, "kinds": list(k), "count": len(items),
            "by_kind": {kd: sum(1 for _, x, _ in items if x == kd) for kd in ("run", "strength", "mobility")},
            "items": [{"date": d, "kind": kd, "title": wo.workoutName} for d, kd, wo in items]}


@app.post("/api/garmin/sync/push")
def api_garmin_sync_push(payload: dict = Body(...)):
    weeks = int(payload.get("weeks") or 2)
    kinds = payload.get("kinds") or ["run", "strength", "mobility"]
    if not _sync_lock.acquire(blocking=False):
        return JSONResponse({"ok": False, "message": "Sync already running."}, status_code=409)
    _sync_state["running"] = True
    _sync_state["last"] = None

    def _run():
        try:
            _do_garmin_sync(weeks, tuple(kinds))
        finally:
            _sync_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "message": f"Syncing {weeks} week(s) to Garmin in the background..."}


@app.get("/api/garmin/sync/status")
def api_garmin_sync_status():
    return {"running": _sync_state["running"], "last": _sync_state["last"]}


@app.post("/api/coach/chat")
def api_coach_chat(payload: dict = Body(...)):
    msg = payload.get("message", "")
    apply_confirmed = payload.get("confirmed")   # {routine, args} when user clicks Confirm
    history = payload.get("history") or []
    import routines as routines_mod
    # Wire on-demand Garmin providers so tools like activity_splits can pull
    # per-lap data without importing app.py.
    routines_mod.PROVIDERS["splits"] = _gd.activity_splits
    routines_mod.PROVIDERS["stream"] = _activity_stream
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    snap = get_snapshot()
    r = routines_mod.dispatch(msg, snap, cfg, _today(), apply_confirmed=apply_confirmed, history=history)
    if r:
        return r
    # legacy text-only fallback (uses coach.chat rule-based)
    return coach.chat(msg, snap, _today())


@app.get("/api/coach/routines")
def api_coach_routines():
    import routines as routines_mod
    return {"routines": routines_mod.routines_index()}


@app.get("/api/log")
def api_log():
    """Strava-style training log: activities grouped by ISO week (Mon-anchored),
    with per-day buckets and weekly summary metrics (total time, elevation,
    training load, HR-zone split, longest run) plus per-day annotations
    (planned/missed/unplanned/race/PR)."""
    from collections import defaultdict
    snap = get_snapshot() or {}
    acts = snap.get("all_activities") or snap.get("activities") or []
    def kind_of(t):
        t = (t or "").lower()
        if "run" in t: return "run"
        if any(k in t for k in ("strength", "weight", "lifting", "crossfit")): return "strength"
        if any(k in t for k in ("yoga", "mobility", "stretch", "pilates", "breath", "meditation")): return "mobility"
        if "hik" in t or "walk" in t: return "hike"
        if "cycl" in t or "bike" in t: return "bike"
        if "swim" in t: return "swim"
        if "hiit" in t or "cardio" in t or "workout" in t: return "workout"
        return "other"
    # ---- plan lookup for planned/missed/unplanned annotations ----
    plan_by_date = {}
    try:
        plan = planlib.load_plan() or {}
        for wk in plan.get("weeks", []):
            for dd in wk.get("days", []):
                if dd.get("date"):
                    plan_by_date[dd["date"]] = dd
    except Exception:
        plan_by_date = {}
    today_iso = _today().isoformat()
    # ---- PR flags: longest run + fastest pace per distance band ----
    runs_all = [a for a in acts if "run" in ((a.get("type") or "").lower()) and (a.get("distance_km") or 0) > 0]
    pr_dates = set()
    if runs_all:
        longest = max(runs_all, key=lambda r: r.get("distance_km") or 0)
        if longest.get("date"):
            pr_dates.add(longest["date"])
    def _band(km):
        if 4 <= km < 6: return "5k"
        if 8 <= km < 12: return "10k"
        if 18 <= km < 24: return "half"
        if 40 <= km < 44: return "marathon"
        return None
    band_best = {}
    for r in runs_all:
        b = _band(r.get("distance_km") or 0)
        pace = r.get("avg_pace_s_per_km")
        if not b or not pace:
            continue
        cur = band_best.get(b)
        if not cur or pace < cur["pace"]:
            band_best[b] = {"pace": pace, "date": r.get("date")}
    for info in band_best.values():
        if info.get("date"):
            pr_dates.add(info["date"])
    _RUN_TYPES = {"easy", "quality", "long", "race", "moderate", "steady", "tempo"}
    # ---- group by week ----
    weeks = defaultdict(lambda: {
        "days": defaultdict(list),
        "total_km": 0.0, "total_min": 0, "total_time_s": 0.0,
        "elev_gain_m": 0.0, "training_load": 0.0,
        "longest_run": None, "pace_num": 0.0, "pace_den": 0.0,
        "hr_zones_s": {f"z{i}": 0.0 for i in range(1, 6)},
    })
    for a in acts:
        date_iso = a.get("date")
        if not date_iso:
            continue
        try:
            d0 = dt.date.fromisoformat(date_iso)
        except Exception:
            continue
        k = kind_of(a.get("type"))
        dist_km = float(a.get("distance_km") or 0)
        dur_s = float(a.get("moving_s") or a.get("duration_s") or 0)
        pace_s = a.get("avg_pace_s_per_km")
        entry = {
            "id": a.get("id"),
            "kind": k,
            "type": a.get("type"),
            "name": a.get("name") or "",
            "distance_km": round(dist_km, 2),
            "duration_s": round(dur_s),
            "duration_min": round(dur_s / 60),
            "avg_hr": a.get("avg_hr"),
            "avg_pace": planlib.pace_str(pace_s) if k == "run" else None,
            "elev_gain_m": a.get("elev_gain_m"),
            "training_load": a.get("training_load"),
            "is_race": ((a.get("event_type") or "").lower() == "race"),
            "is_pr": (k == "run" and date_iso in pr_dates),
        }
        ws = (d0 - dt.timedelta(days=d0.weekday())).isoformat()
        wk = weeks[ws]
        wk["days"][date_iso].append(entry)
        wk["total_time_s"] += dur_s
        wk["total_min"] += int(dur_s // 60)
        if a.get("elev_gain_m"):
            try:
                wk["elev_gain_m"] += float(a.get("elev_gain_m") or 0)
            except Exception:
                pass
        if a.get("training_load"):
            try:
                wk["training_load"] += float(a.get("training_load") or 0)
            except Exception:
                pass
        for zi in range(1, 6):
            wk["hr_zones_s"][f"z{zi}"] += float((a.get("hr_zones_s") or {}).get(f"z{zi}") or 0)
        if k == "run":
            wk["total_km"] += dist_km
            if pace_s and dist_km:
                wk["pace_num"] += pace_s * dist_km
                wk["pace_den"] += dist_km
            lr = wk["longest_run"]
            if not lr or dist_km > (lr.get("distance_km") or 0):
                wk["longest_run"] = {
                    "date": date_iso,
                    "distance_km": round(dist_km, 2),
                    "pace": planlib.pace_str(pace_s),
                    "name": a.get("name") or "",
                }
    out = []
    for ws in sorted(weeks.keys(), reverse=True):
        wk = weeks[ws]
        wstart = dt.date.fromisoformat(ws)
        days = []
        for i in range(7):
            d0 = wstart + dt.timedelta(days=i)
            key = d0.isoformat()
            acts_here = wk["days"].get(key, [])
            pday = plan_by_date.get(key) or {}
            planned_type = (pday.get("type") or "").lower() or None
            is_run_planned = planned_type in _RUN_TYPES
            has_logged_run = any(a["kind"] == "run" for a in acts_here)
            missed = bool(is_run_planned and not has_logged_run and key < today_iso)
            unplanned_run = bool(has_logged_run and planned_type and not is_run_planned)
            days.append({
                "date": key,
                "activities": acts_here,
                "planned_type": planned_type,
                "missed": missed,
                "unplanned_run": unplanned_run,
            })
        avg_pace_s = round(wk["pace_num"] / wk["pace_den"], 1) if wk["pace_den"] else None
        out.append({
            "week_start": ws,
            "week_end": (wstart + dt.timedelta(days=6)).isoformat(),
            "total_km": round(wk["total_km"], 2),
            "total_min": wk["total_min"],
            "total_time_s": round(wk["total_time_s"]),
            "elev_gain_m": round(wk["elev_gain_m"]),
            "training_load": round(wk["training_load"]),
            "longest_run": wk["longest_run"],
            "avg_run_pace": planlib.pace_str(avg_pace_s),
            "hr_zones_s": {z: round(v) for z, v in wk["hr_zones_s"].items()},
            "days": days,
        })
    return {"weeks": out}


@app.get("/api/coach/recommendations")
def api_recs():
    """Return the adaptive-coach's current pending recs, recent decisions
    and learned rule weights."""
    import adaptive
    snap = get_snapshot() or {}
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    adaptive.refresh(snap, cfg, _today())
    morning = adaptive.morning_read_status(snap, _today())
    return adaptive.list_recs(morning=morning)


@app.post("/api/coach/recommendations/refresh")
def api_recs_refresh():
    """Force a re-evaluation. Useful right after a Garmin refresh."""
    import adaptive
    snap = get_snapshot() or {}
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    adaptive.refresh(snap, cfg, _today())
    morning = adaptive.morning_read_status(snap, _today())
    return adaptive.list_recs(morning=morning)


@app.post("/api/coach/recommendations/{rec_id}/decide")
def api_recs_decide(rec_id: str, payload: dict = Body(...)):
    """Accept or reject a rec. Body: {decision: "accepted"|"rejected", reason?: str}
    On accept we run the underlying plan routine in apply mode."""
    import adaptive, routines as routines_mod
    decision = (payload.get("decision") or "").lower()
    reason = payload.get("reason") or ""
    if decision not in ("accepted", "rejected"):
        return {"error": "decision must be 'accepted' or 'rejected'"}
    snap = get_snapshot() or {}
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    today = _today()

    def _applier(routine_name, args):
        r = routines_mod.REGISTRY.get(routine_name)
        if not r:
            return {"error": f"unknown routine {routine_name}"}
        if r.kind == "plan":
            return r.handler(args or {}, snap, cfg, today, apply=True)
        return r.handler(args or {}, snap, cfg, today)

    return adaptive.decide(rec_id, decision, reason, _applier, snap=snap)


@app.get("/api/coach/recommendations/learning")
def api_recs_learning():
    """What has the coach learned from your accept/reject history?"""
    import adaptive
    return adaptive.learning_summary()


# -------- niggle log --------
@app.get("/api/niggles")
def api_niggles(status: str = None):
    import niggles
    return {"niggles": niggles.list_all(status)}


@app.post("/api/niggles")
def api_niggle_create(payload: dict = Body(...)):
    import niggles
    return niggles.create(
        body_part=payload.get("body_part") or "",
        severity=payload.get("severity") or 1,
        side=payload.get("side"),
        started_date=payload.get("started_date"),
        aggravated_by=payload.get("aggravated_by") or "",
        notes=payload.get("notes") or "",
    )


@app.post("/api/niggles/{niggle_id}/update")
def api_niggle_update(niggle_id: str, payload: dict = Body(...)):
    import niggles
    return niggles.update(niggle_id,
                          severity=payload.get("severity"),
                          notes=payload.get("notes"),
                          aggravated_by=payload.get("aggravated_by"),
                          note=payload.get("note"))


@app.post("/api/niggles/{niggle_id}/resolve")
def api_niggle_resolve(niggle_id: str, payload: dict = Body(default=None)):
    import niggles
    return niggles.resolve(niggle_id, (payload or {}).get("note") or "")


@app.post("/api/niggles/{niggle_id}/reopen")
def api_niggle_reopen(niggle_id: str, payload: dict = Body(...)):
    import niggles
    return niggles.reopen(niggle_id, payload.get("severity") or 1,
                          payload.get("note") or "")


@app.delete("/api/niggles/{niggle_id}")
def api_niggle_delete(niggle_id: str):
    import niggles
    return niggles.delete(niggle_id)


# -------- Strava race predictions (manual entry) --------
@app.get("/api/strava/predictions")
def api_strava_get():
    import fitness
    return {"predictions": fitness._load_strava_predictions()}


@app.post("/api/strava/predictions")
def api_strava_save(payload: dict = Body(...)):
    import fitness
    return fitness.save_strava_predictions(
        payload.get("times") or payload,
        source=payload.get("source") or "manual",
        reference=payload.get("reference"),
    )


# -------- environment / weather --------
@app.get("/api/environment/today")
def api_env_today(force: bool = False):
    import environment
    cfg = planlib.resolve_active_race(planlib.load_config(), _today())
    return environment.today(cfg, force=force)


# ---------------- static frontend ----------------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


def _safe_bg_refresh():
    try:
        do_refresh()
        print("[startup] background Garmin refresh complete")
    except Exception as e:  # noqa: BLE001
        print(f"[startup] background refresh failed: {e}")


if __name__ == "__main__":
    import uvicorn
    # Auto-refresh in the background on startup if the cache is missing or stale (>6h),
    # so the dashboard stays current on its own without blocking the server start.
    if paths.DEMO:
        demo.ensure_demo_data(_today())
        print("  Demo mode: synthetic athlete, no Garmin login. Data in demo-data/.")
    _offline = paths.DEMO or os.environ.get("RC_OFFLINE") == "1"   # cached data only, no Garmin
    _snap = cache.load()
    _age = cache.age_seconds(_snap)
    if not _offline and not _garmin_connected():
        print("  Garmin not connected yet: run  uv run python garmin_login.py  (or start with --demo).")
    elif not _offline and (_snap is None or (_age is not None and _age > 6 * 3600)):
        threading.Thread(target=_safe_bg_refresh, daemon=True).start()
    # Build the long-history HRV/RHR baselines if we've never built them or they're
    # stale (>30 days). Runs in the background so it never blocks startup.
    if not _offline and _garmin_connected() and _baselines_stale():
        threading.Thread(target=_rebuild_baselines, daemon=True).start()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")   # set HOST=0.0.0.0 in a container
    if not APP_PASSWORD and not ALLOW_NO_PASSWORD and not (host == "localhost" or _is_loopback_ip(host)):
        sys.exit(f"\n  Refusing to listen on {host} without APP_PASSWORD: anyone on your network could read "
                 "your health data.\n  Set APP_PASSWORD in .env (or RC_ALLOW_NO_PASSWORD=1 if you really mean it).\n")
    print(f"\n  Running Coach → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
