# -*- coding: utf-8 -*-
"""Environmental corrections — turns weather into pace / effort adjustments.

Pulls today's forecast from Open-Meteo (no API key required) for the athlete's
home coordinates, adjusts perceived effort per the Daniels/Jones heat + humidity
model, and hands back:

  - Raw conditions (temp / humidity / wind / heat index)
  - Suggested pace adjustment (seconds per km slower than the target pace)
  - A short read the coach can quote and the adaptive engine can trigger on

Cached for 20 min per lat/lon so repeated dashboards + coach queries don't
hammer the API. Location comes from config.home {lat, lon} (set during
onboarding), falling back to the race's lat_lon; with neither, weather is off.
"""
from __future__ import annotations
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Optional

import cache
import planlib


# -------- config --------
CACHE_TTL_SEC = 20 * 60
OM_URL = "https://api.open-meteo.com/v1/forecast"


# -------- cache --------
_ENV_CACHE: dict[str, dict] = {}      # {"lat,lon": {"at": ts, "data": {...}}}


def _resolve_location(cfg: dict) -> Optional[tuple[float, float]]:
    home = (cfg or {}).get("home") or {}
    if "lat" in home and "lon" in home:
        return (float(home["lat"]), float(home["lon"]))
    race = (cfg or {}).get("race") or {}
    rl = race.get("lat_lon") or race.get("location_latlon")
    if rl and isinstance(rl, (list, tuple)) and len(rl) == 2:
        return (float(rl[0]), float(rl[1]))
    return None


def _fetch_open_meteo(lat: float, lon: float) -> dict:
    q = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,wind_gusts_10m,precipitation,is_day",
        "hourly": "temperature_2m,relative_humidity_2m,apparent_temperature",
        "temperature_unit": "celsius",
        "wind_speed_unit": "kmh",
        "precipitation_unit": "mm",
        "timezone": "auto",
        "forecast_days": "1",
    }
    url = f"{OM_URL}?{urllib.parse.urlencode(q)}"
    req = urllib.request.Request(url, headers={"User-Agent": "running-coach/1.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        return json.loads(r.read().decode("utf-8"))


# -------- physiology --------
def _heat_index_c(temp_c: float, rh_pct: float) -> float:
    """Rothfusz-esque approximation of the perceived-temperature index.
    Below 26 C we don't add a humidity penalty (feels close to actual)."""
    if temp_c < 26:
        return temp_c
    t = temp_c * 9 / 5 + 32  # Rothfusz is defined in F
    r = rh_pct
    hi_f = (-42.379 + 2.04901523 * t + 10.14333127 * r - 0.22475541 * t * r
            - 0.00683783 * t * t - 0.05481717 * r * r + 0.00122874 * t * t * r
            + 0.00085282 * t * r * r - 0.00000199 * t * t * r * r)
    return round((hi_f - 32) * 5 / 9, 1)


def _pace_adjust_sec_per_km(heat_index_c: float, wind_kmh: float) -> tuple[int, str]:
    """Distance-runner heat penalty (Daniels-style, converted to metric).
    Kicks in above ~13 C dew-point equivalent. Wind above ~30 km/h adds a
    small extra headwind cost that averages across a loop.

    Returns (seconds_per_km_slower, tier), tier in {"none","mild","warm","hot","dangerous"}.
    """
    hi = heat_index_c or 0
    if hi < 12: base = 0
    elif hi < 15: base = 3
    elif hi < 18: base = 6
    elif hi < 21: base = 10
    elif hi < 24: base = 15
    elif hi < 27: base = 22
    elif hi < 30: base = 30
    elif hi < 33: base = 42
    else: base = 55
    wind_bump = max(0, int((wind_kmh - 30) / 10) * 3) if wind_kmh else 0
    total = base + wind_bump
    if hi >= 30: tier = "dangerous"
    elif hi >= 26: tier = "hot"
    elif hi >= 21: tier = "warm"
    elif hi >= 15: tier = "mild"
    else: tier = "none"
    return total, tier


def _hr_drift_bpm(heat_index_c: float) -> int:
    """Rough cardiac-drift add-on: HR rises ~1 bpm per 1 C above 18 C at effort."""
    if heat_index_c is None or heat_index_c < 18:
        return 0
    return int(round(heat_index_c - 18))


def _read(tier: str, adj_sec: int, hi: float, temp: float, rh: float, wind: float,
          precip: float, drift: int) -> str:
    if tier == "none":
        return (f"Cool conditions ({temp:.0f} C, {rh:.0f}% RH). Run to pace - no "
                f"heat adjustment needed.")
    if tier == "mild":
        return (f"Mild bump ({temp:.0f} C, {rh:.0f}% RH, feels {hi:.0f}). Expect "
                f"~{adj_sec}s/km slower at the same effort; HR may sit ~{drift} bpm higher.")
    if tier == "warm":
        return (f"Warm ({temp:.0f} C / {rh:.0f}% RH, feels {hi:.0f}). Add ~{adj_sec}s/km "
                f"to easy and long targets; HR drift ~{drift} bpm. Bring fluids.")
    if tier == "hot":
        return (f"Hot ({temp:.0f} C / {rh:.0f}% RH, feels {hi:.0f}). Run to HR ceiling, "
                f"not pace - expect ~{adj_sec}s/km slower; keep hard efforts short and "
                f"the long run flexible. Hydrate + sodium.")
    return (f"Dangerous heat ({temp:.0f} C / {rh:.0f}% RH, feels {hi:.0f}). Move the "
            f"workout indoors, split it, or move it earlier/later. If you go: cap effort "
            f"at easy, add ~{adj_sec}s/km, and abort if HR spikes.")


def today(cfg: Optional[dict] = None, force: bool = False) -> dict:
    """Return a small dict the frontend and coach both consume."""
    import paths
    if paths.DEMO:
        import demo
        return demo.weather()
    cfg = cfg or planlib.load_config() or {}
    loc = _resolve_location(cfg)
    if loc is None:
        return {"has_data": False, "error": "No home location set (add it in Settings)."}
    lat, lon = loc
    key = f"{lat:.3f},{lon:.3f}"
    now = time.time()
    if not force and key in _ENV_CACHE and (now - _ENV_CACHE[key]["at"]) < CACHE_TTL_SEC:
        return _ENV_CACHE[key]["data"]
    try:
        raw = _fetch_open_meteo(lat, lon)
    except Exception as e:  # noqa: BLE001
        # Silent-fail with a readable error - Coach shouldn't die on weather.
        return {"has_data": False, "error": f"{type(e).__name__}: {e}",
                "lat": lat, "lon": lon}
    cur = raw.get("current") or {}
    temp = float(cur.get("temperature_2m") or 0)
    rh = float(cur.get("relative_humidity_2m") or 0)
    wind = float(cur.get("wind_speed_10m") or 0)
    gust = float(cur.get("wind_gusts_10m") or 0)
    precip = float(cur.get("precipitation") or 0)
    is_day = bool(cur.get("is_day"))
    hi = _heat_index_c(temp, rh)
    adj_sec, tier = _pace_adjust_sec_per_km(hi, wind)
    drift = _hr_drift_bpm(hi)
    out = {
        "has_data": True,
        "lat": lat, "lon": lon, "timezone": raw.get("timezone"),
        "as_of": cur.get("time"),
        "temp_c": round(temp, 1),
        "humidity_pct": round(rh),
        "apparent_c": round(cur.get("apparent_temperature") or hi, 1),
        "heat_index_c": hi,
        "wind_kmh": round(wind, 1),
        "gust_kmh": round(gust, 1),
        "precip_mm": round(precip, 1),
        "is_day": is_day,
        "tier": tier,
        "pace_adjust_s_per_km": adj_sec,
        "hr_drift_bpm": drift,
        "read": _read(tier, adj_sec, hi, temp, rh, wind, precip, drift),
    }
    _ENV_CACHE[key] = {"at": now, "data": out}
    return out


def context_line(cfg: Optional[dict] = None) -> str:
    """One-line summary for the coach's LLM system prompt so answers factor
    today's weather. Empty string on fetch failure or cool conditions."""
    d = today(cfg)
    if not d.get("has_data") or d.get("tier") == "none":
        return ""
    return (f"CONDITIONS: {d['temp_c']} C / {d['humidity_pct']}% RH (feels "
            f"{d['heat_index_c']}). {d['read']}")
