"""World-class fitness analysis: a Performance Management Chart (Fitness /
Fatigue / Form), VO2max percentile vs age & sex, a VDOT-based "path to your goal and
beyond", an ACWR trend with risk zones, training-status interpretation, and a
plain-English glossary.

Everything degrades gracefully when a field hasn't synced.
"""
from __future__ import annotations
import datetime as dt
import math
from typing import Optional

import json
import os
import planlib
from planlib import load_config, fmt_secs, pace_str, resolve_active_race

import cache as cache_mod


# --- Strava predictions store -----------------------------------------------
# Strava's race-time predictions aren't exposed via public API. The user
# copies the four times from Strava's Fitness page into the "Race predictions"
# card, and we persist them here alongside the last-updated timestamp.
STRAVA_PRED_FILE = os.path.join(cache_mod.CACHE_DIR, "strava_predictions.json")


def _load_strava_predictions_full() -> dict:
    """Return {predictions: [...], updated_at: str|None, source: str|None}."""
    empty = {"predictions": [], "updated_at": None, "source": None}
    if not os.path.exists(STRAVA_PRED_FILE):
        return empty
    try:
        with open(STRAVA_PRED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
    except Exception:
        return empty
    raw = data.get("predictions") or {}
    out = []
    for k, m in (("5K", 5000), ("10K", 10000), ("Half", HALF_M), ("Marathon", 42195)):
        secs = raw.get(k) or raw.get(k.lower())
        if not secs:
            continue
        try:
            secs = int(secs)
        except Exception:
            continue
        out.append({
            "dist": k, "seconds": secs, "time": fmt_secs(secs),
            "pace": pace_str(secs / (m / 1000.0)),
        })
    return {
        "predictions": out,
        "updated_at": data.get("updated_at"),
        "source": data.get("source"),
        "reference": data.get("reference"),
    }


def _load_strava_predictions() -> list:
    """Backwards-compatible: return just the predictions list."""
    return _load_strava_predictions_full()["predictions"]


def save_strava_predictions(times: dict, source: str = "manual",
                             reference: str = None) -> dict:
    """Persist a dict of {dist: seconds}. Accepted keys: 5K, 10K, Half, Marathon
    (case-insensitive). `source` tags where the numbers came from (manual,
    strava-mcp, strava-oauth). `reference` optionally explains the derivation
    (e.g. 'derived from Strava 10K PR 37:00 via Daniels VDOT')."""
    import datetime as _dt
    normed = {}
    for k, v in (times or {}).items():
        if v is None or v == "":
            continue
        try:
            secs = int(v)
        except Exception:
            try:
                parts = [int(p) for p in str(v).split(":")]
                secs = parts[-1] + parts[-2] * 60 + (parts[-3] * 3600 if len(parts) >= 3 else 0)
            except Exception:
                continue
        canon = {"5k": "5K", "10k": "10K", "half": "Half",
                 "half marathon": "Half", "marathon": "Marathon"}.get(str(k).lower(), k)
        normed[canon] = secs
    cache_mod.ensure_dir()
    payload = {
        "predictions": normed,
        "updated_at": _dt.datetime.now().isoformat(),
        "source": source,
    }
    if reference:
        payload["reference"] = reference
    tmp = STRAVA_PRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STRAVA_PRED_FILE)
    full = _load_strava_predictions_full()
    return {"ok": True, **full}


# ---------------- Daniels VDOT model ----------------
def _vdot_from(distance_m: float, seconds: float) -> float:
    """VDOT (effective VO2max for racing) from a race distance + time."""
    t = seconds / 60.0
    v = distance_m / t  # m/min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct


def _time_for_vdot(distance_m: float, vdot: float) -> float:
    """Invert the model: seconds needed at `distance_m` to reach `vdot`."""
    lo, hi = 3 * 60.0, 6 * 3600.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _vdot_from(distance_m, mid) > vdot:  # too fast for this vdot → slow down
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


HALF_M = 21097.5


def _slope_per_day(trend: list) -> float:
    """Least-squares VDOT gain per day over the recent trend (robust to the
    day-to-day dip a build block causes)."""
    pts = trend[-120:]
    if len(pts) < 5:
        return 0.0
    d0 = dt.date.fromisoformat(pts[0]["date"])
    xs = [(dt.date.fromisoformat(p["date"]) - d0).days for p in pts]
    ys = [p["vdot"] for p in pts]
    n = len(xs)
    sx, sy = sum(xs), sum(ys)
    sxx = sum(x * x for x in xs)
    sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    return 0.0 if denom == 0 else (n * sxy - sx * sy) / denom


def _vdot_projection(trend, cur_vdot, today, race_date, cfg):
    """Project VDOT to race day if the plan is followed: a build phase that
    continues the recent fitness slope, then a taper 'freshness reveal' (a fresh
    race beats the training-suppressed predictor). Returns an expected line plus
    a conservative–optimistic band."""
    if not trend or race_date <= today:
        return None
    taper_start = race_date - dt.timedelta(days=14)
    slope = _slope_per_day(trend)
    slope = max(0.0, min(slope, 0.05))                 # clamp to sane gains
    taper_days = max(1, (race_date - taper_start).days)

    # taper "reveal" bump (VDOT-equivalent of racing fresh) — scenario band
    BUMP = {"low": 1.0, "exp": 2.0, "high": 3.0}
    RATE = {"low": slope * 0.5, "exp": slope, "high": slope * 1.5}

    def val(d, key):
        if d <= taper_start:
            return cur_vdot + RATE[key] * (d - today).days
        base = cur_vdot + RATE[key] * (taper_start - today).days
        frac = (d - taper_start).days / taper_days
        return base + BUMP[key] * frac

    series = []
    d = today
    while d <= race_date:
        series.append({"date": d.isoformat(),
                       "vdot": round(val(d, "exp"), 1),
                       "low": round(val(d, "low"), 1),
                       "high": round(val(d, "high"), 1)})
        d += dt.timedelta(days=1)

    race_m = ((cfg.get("race") or {}).get("distance_km") or 21.1) * 1000.0
    race_exp = round(val(race_date, "exp"), 1)
    race_low = round(val(race_date, "low"), 1)
    race_high = round(val(race_date, "high"), 1)
    race_time = _time_for_vdot(race_m, race_exp)

    # which goal does the expected projection reach?
    goals = cfg.get("goals") or {}
    hit = None
    for k in ("A", "B", "C"):
        g = goals.get(k)
        if g and race_time <= g["seconds"]:
            hit = k
            break
    note = _projection_note(race_exp, race_high, race_time, hit, goals)
    return {
        "series": series[1:],                  # exclude today (already the last actual point)
        "race_date": race_date.isoformat(),
        "race_vdot": race_exp, "race_vdot_low": race_low, "race_vdot_high": race_high,
        "race_time": fmt_secs(race_time),
        "race_pace": pace_str(race_time / (race_m / 1000.0)),
        "slope_per_week": round(slope * 7, 2),
        "hits_goal": hit,
        "note": note,
    }


def _projection_note(race_exp, race_high, race_time, hit, goals):
    a = (goals.get("A") or {})
    at = a.get("time", "your A goal")
    if hit == "A":
        return (f"On your current trajectory the plan projects VDOT ≈{race_exp} by race day — enough to run "
                f"{at}. Execute the key race-pace sessions and {at} is real.")
    if hit == "B":
        bt = (goals.get("B") or {}).get("time", "your B goal")
        return (f"The plan projects VDOT ≈{race_exp} → about {fmt_secs(race_time)}, right on your B goal ({bt}). "
                f"{at} needs the optimistic end (≈{race_high}) — nail every race-pace long-run block and it's in range.")
    if hit == "C":
        return (f"Projected VDOT ≈{race_exp} → ~{fmt_secs(race_time)} (C goal). {at} is a stretch from this "
                f"trajectory; the fitness is trending the right way, so keep stacking consistent weeks.")
    return (f"Projected VDOT ≈{race_exp} by race day → ~{fmt_secs(race_time)}. That's short of your goals on the "
            f"current trend — the biggest levers now are consistent volume and hitting the race-pace blocks clean.")


# ---------------- Performance Management Chart (Fitness/Fatigue/Form) ----------------
def _load_medians_by_type(snapshot: dict, today: dt.date) -> dict:
    """Median training_load per plan day-type over the last 60 days of runs.
    Used to project future fitness/fatigue when the actual load isn't known
    yet - what would today's training pattern look like a month from now."""
    acts = (snapshot or {}).get("activities", []) or []
    cutoff = (today - dt.timedelta(days=60)).isoformat()
    buckets: dict[str, list] = {}
    try:
        plan = planlib.load_plan() or {}
        plan_by_date = {}
        for wk in plan.get("weeks", []):
            for dd in wk.get("days", []):
                if dd.get("date"):
                    plan_by_date[dd["date"]] = dd
    except Exception:
        plan_by_date = {}
    for a in acts:
        d = a.get("date"); tl = a.get("training_load")
        if not d or not tl or d < cutoff:
            continue
        pd = plan_by_date.get(d) or {}
        pt = (pd.get("type") or "").lower()
        if not pt:
            # infer from distance if plan didn't schedule this run
            km = a.get("distance_km") or 0
            pt = "long" if km >= 15 else "quality" if km >= 8 else "easy"
        buckets.setdefault(pt, []).append(tl)
    med = {}
    for k, vals in buckets.items():
        vals.sort(); n = len(vals)
        med[k] = vals[n // 2] if n else 0
    # Sensible defaults if a bucket is empty
    med.setdefault("easy", 55)
    med.setdefault("quality", 110)
    med.setdefault("long", 140)
    med.setdefault("race", 180)
    med.setdefault("moderate", med["easy"])
    med.setdefault("steady", med.get("quality", 90))
    med.setdefault("tempo", med.get("quality", 100))
    med.setdefault("rest", 0)
    return med


def _forecast_series(today_ctl: float, today_atl: float, today: dt.date,
                     horizon_days: int, plan: dict, medians: dict) -> list:
    """Project CTL/ATL/TSB forward using the plan's scheduled load estimates."""
    plan_by_date = {}
    for wk in plan.get("weeks", []):
        for dd in wk.get("days", []):
            if dd.get("date"):
                plan_by_date[dd["date"]] = dd
    ctl, atl = today_ctl, today_atl
    out = []
    for i in range(1, horizon_days + 1):
        d = today + dt.timedelta(days=i)
        pd = plan_by_date.get(d.isoformat()) or {}
        est_load = medians.get((pd.get("type") or "rest").lower(), 0)
        tsb = ctl - atl
        ctl = ctl + (est_load - ctl) / 42.0
        atl = atl + (est_load - atl) / 7.0
        out.append({"date": d.isoformat(), "ctl": round(ctl, 1),
                    "atl": round(atl, 1), "tsb": round(tsb, 1),
                    "load": round(est_load), "forecast": True})
    return out


def _trends(series: list) -> dict:
    """Multi-window fitness slopes + fatigue peak so observations can talk
    about building vs plateaued vs regressing without me hand-writing 5 cases."""
    def _delta(days: int):
        if len(series) <= days:
            return None
        return round(series[-1]["ctl"] - series[-days - 1]["ctl"], 1)
    ctl_7 = _delta(7); ctl_28 = _delta(28); ctl_90 = _delta(90)
    atl_last14 = [p["atl"] for p in series[-14:]]
    peak_atl = round(max(atl_last14), 1) if atl_last14 else 0
    ratio = round(series[-1]["atl"] / series[-1]["ctl"], 2) if series[-1]["ctl"] else None
    days_over = sum(1 for p in series[-14:] if p["ctl"] and p["atl"] / p["ctl"] > 1.3)
    days_negative_tsb = sum(1 for p in series[-14:] if p["tsb"] < -10)
    return {
        "ctl_change_7d": ctl_7, "ctl_change_28d": ctl_28, "ctl_change_90d": ctl_90,
        "ctl_rate_per_week": round((ctl_28 / 28.0 * 7) if ctl_28 is not None else 0, 2),
        "peak_atl_14d": peak_atl,
        "atl_ctl_ratio": ratio,
        "days_atl_over_130_pct": days_over,
        "days_negative_tsb_14": days_negative_tsb,
    }


def _observations(series: list, forecast: list, trends: dict, cfg: dict,
                  today: dt.date, medians: dict) -> tuple:
    """Turn the numbers into 3-5 plain-English coach observations plus a
    single load recommendation (hold / cut / build / taper / race-ready)."""
    cur = series[-1]
    obs: list[dict] = []
    rate = trends.get("ctl_rate_per_week") or 0
    ratio = trends.get("atl_ctl_ratio")

    # -- fitness direction ----
    if trends.get("ctl_change_28d") is not None:
        c28 = trends["ctl_change_28d"]
        if c28 > 2.0:
            obs.append({"tone": "good",
                        "title": "Fitness climbing steadily",
                        "detail": f"CTL up {c28:+.1f} over the last 28 days (~{rate:+.2f}/wk). "
                                  f"Volume is compounding — every 2-3 wk block is banking real aerobic base."})
        elif c28 < -1.5:
            obs.append({"tone": "warn",
                        "title": "Fitness slipping",
                        "detail": f"CTL down {c28:+.1f} over the last 28 days. Either a planned dip (taper/travel/illness) "
                                  f"or under-training — check the calendar."})
        else:
            obs.append({"tone": "note",
                        "title": "Fitness plateaued",
                        "detail": f"CTL change {c28:+.1f} in 28d. Holding — good for a maintenance block, less good if you "
                                  f"want to add speed. Consider a small volume bump or an extra quality day."})

    # -- fatigue load ----
    if ratio is not None:
        days_hot = trends.get("days_atl_over_130_pct", 0)
        if ratio > 1.30:
            obs.append({"tone": "warn",
                        "title": "Fatigue running above capacity",
                        "detail": f"ATL/CTL ratio {ratio}. You've been over 1.3× your chronic load for {days_hot} of the "
                                  f"last 14 days — the classic overreach setup. An easier week now converts this into "
                                  f"fitness; another hard week compounds injury risk."})
        elif ratio > 1.10:
            obs.append({"tone": "note",
                        "title": "Constructive overload",
                        "detail": f"ATL/CTL ratio {ratio}. Sitting productively above your baseline — this is the load "
                                  f"pattern that grows fitness. Watch that easy days stay easy so tomorrow's quality lands."})
        else:
            obs.append({"tone": "good",
                        "title": "Balanced load",
                        "detail": f"ATL/CTL ratio {ratio}. Fatigue in the productive zone — room to nudge volume "
                                  f"up if the plan allows and legs feel fresh."})

    # -- form window / recovery need ----
    dneg = trends.get("days_negative_tsb_14", 0)
    if dneg >= 10:
        obs.append({"tone": "note",
                    "title": "Form deep and sustained",
                    "detail": f"TSB has been negative on {dneg} of the last 14 days. Normal in a build block, but a "
                              f"short freshening (2-3 easy days) before the next big quality session pays back."})

    # -- race context ----
    race = (cfg or {}).get("race") or {}
    if race.get("date"):
        try:
            race_date = dt.date.fromisoformat(race["date"])
            days_out = (race_date - today).days
            if 0 < days_out <= 90 and forecast:
                # peak fitness expected in the forecast window
                peak = max(forecast, key=lambda p: p["ctl"])
                race_pt = next((p for p in forecast if p["date"] == race["date"]), None)
                race_tsb = race_pt["tsb"] if race_pt else None
                if race_tsb is not None:
                    tsb_note = ("perfect race-day freshness"
                                if 5 <= race_tsb <= 20 else
                                "under-tapered - not enough freshness"
                                if race_tsb < 5 else
                                "over-tapered - fitness might already be slipping")
                    obs.append({"tone": "good" if 5 <= race_tsb <= 20 else "warn",
                                "title": f"Race-day projection ({days_out}d out)",
                                "detail": f"Following the plan puts you at CTL {race_pt['ctl']}, TSB {race_tsb:+.1f} "
                                          f"({tsb_note}). Peak fitness projects around {peak['date']} at CTL {peak['ctl']}."})
        except Exception:
            pass

    # -- one-line load recommendation ----
    verdict = "hold"; verdict_tone = "good"
    verdict_msg = "Current load is producing fitness without compounding fatigue. Keep executing the plan."
    if ratio is not None and ratio > 1.35 and trends.get("days_atl_over_130_pct", 0) >= 5:
        verdict = "cut"; verdict_tone = "warn"
        verdict_msg = ("Cut the next 7 days by ~20-30% volume (or drop the mid-week quality). Turning fatigue into "
                       "fitness needs recovery, not more work.")
    elif rate < 0.2 and trends.get("ctl_change_90d") is not None and trends["ctl_change_90d"] < 2:
        verdict = "build"; verdict_tone = "note"
        verdict_msg = ("Fitness has stalled. If time allows, add ~10% weekly volume for 3 wk or convert one easy day "
                       "into a moderate/steady session.")
    elif race.get("date"):
        try:
            days_out = (dt.date.fromisoformat(race["date"]) - today).days
            if 0 < days_out <= 14:
                verdict = "taper"; verdict_tone = "note"
                verdict_msg = ("Taper window: keep frequency, cut volume ~40-50%, keep short race-pace touches. "
                               "Fitness lost in 14 days is negligible; freshness gained is huge.")
        except Exception:
            pass

    return obs, {"verdict": verdict, "tone": verdict_tone, "message": verdict_msg}


def performance_chart(snapshot: dict, today: dt.date) -> Optional[dict]:
    acts = (snapshot or {}).get("activities", [])
    loads: dict[str, float] = {}
    for a in acts:
        d, tl = a.get("date"), a.get("training_load")
        if d and tl:
            loads[d] = loads.get(d, 0.0) + tl
    if not loads:
        return None

    start = dt.date.fromisoformat(min(loads))
    days = (today - start).days
    # seed CTL/ATL at the mean daily load so the curve doesn't ramp from zero
    seed = sum(loads.values()) / max(1, (days + 1))
    ctl = atl = seed
    series = []
    prev_ctl = ctl
    prev_atl = atl
    for i in range(days + 1):
        d = start + dt.timedelta(days=i)
        load = loads.get(d.isoformat(), 0.0)
        tsb = prev_ctl - prev_atl              # Form = yesterday's Fitness − Fatigue
        ctl = prev_ctl + (load - prev_ctl) / 42.0
        atl = prev_atl + (load - prev_atl) / 7.0
        series.append({"date": d.isoformat(), "ctl": round(ctl, 1),
                       "atl": round(atl, 1), "tsb": round(tsb, 1), "load": round(load)})
        prev_ctl, prev_atl = ctl, atl

    cur = series[-1]
    tsb = cur["tsb"]
    if tsb > 15:
        form, fcolor, fread = "Very fresh", "green", ("Well-tapered / detrained edge — great for racing, but hold this "
                                                      "too long and fitness slips.")
    elif tsb > 5:
        form, fcolor, fread = "Fresh", "green", "Rested and ready — a good window to race or nail a key session."
    elif tsb >= -10:
        form, fcolor, fread = "Neutral", "green", "Balanced load — the productive training zone where fitness is built."
    elif tsb >= -30:
        form, fcolor, fread = "Fatigued", "amber", ("Carrying real fatigue — normal in a build block, but keep easy days "
                                                     "easy and watch recovery.")
    else:
        form, fcolor, fread = "Overreached", "red", ("Deep fatigue — a down week or extra recovery will turn this load into "
                                                      "fitness. Don't stack more hard work now.")

    # ---- coaching layer: trend, forecast, observations, load verdict ----
    cfg = resolve_active_race(load_config(), today)
    try:
        plan = planlib.load_plan() or {}
    except Exception:
        plan = {}
    medians = _load_medians_by_type(snapshot, today)
    # Forecast reaches race day (or 42 days out, whichever is further).
    horizon = 42
    race_d = (cfg.get("race") or {}).get("date")
    if race_d:
        try:
            horizon = max(horizon, (dt.date.fromisoformat(race_d) - today).days + 3)
        except Exception:
            pass
    horizon = min(horizon, 180)
    forecast = _forecast_series(cur["ctl"], cur["atl"], today, horizon, plan, medians)
    trends = _trends(series)
    obs, verdict = _observations(series, forecast, trends, cfg, today, medians)

    # sample the actual series for the chart (≤120 points)
    step = max(1, len(series) // 120)
    return {
        "series": series[::step],
        "forecast": forecast,
        "current": {"fitness": cur["ctl"], "fatigue": cur["atl"], "form": tsb},
        "form_label": form, "form_color": fcolor, "form_read": fread,
        "trends": trends,
        "observations": obs,
        "load_verdict": verdict,
        "medians_used": medians,
    }


# ---------------- VO2max percentile vs age & sex ----------------
# Percentile → VO2max (ml/kg/min), by sex and age band (Cooper Institute norms, approx).
_NORMS = {
    "M": {
        "20-29": [(10, 39), (25, 43), (50, 48), (75, 53), (90, 57), (95, 60), (99, 66)],
        "30-39": [(10, 35), (25, 41), (50, 44.5), (75, 49), (80, 51.5), (90, 55), (95, 58), (99, 63)],
        "40-49": [(10, 32), (25, 38), (50, 42), (75, 46), (90, 51), (95, 54), (99, 59)],
        "50-59": [(10, 29), (25, 34), (50, 38), (75, 42), (90, 46), (95, 49), (99, 54)],
        "60+":   [(10, 26), (25, 30), (50, 33), (75, 37), (90, 41), (95, 44), (99, 49)],
    },
    "F": {
        "20-29": [(10, 33), (25, 37), (50, 41), (75, 45), (90, 50), (95, 53), (99, 58)],
        "30-39": [(10, 31), (25, 35), (50, 39), (75, 43), (90, 47), (95, 50), (99, 55)],
        "40-49": [(10, 28), (25, 32), (50, 36), (75, 40), (90, 44), (95, 47), (99, 52)],
        "50-59": [(10, 25), (25, 29), (50, 33), (75, 37), (90, 41), (95, 43), (99, 48)],
        "60+":   [(10, 23), (25, 27), (50, 30), (75, 34), (90, 38), (95, 40), (99, 45)],
    },
}
_MEDIAN_BY_AGE = {"M": [(25, 48), (35, 44.5), (45, 42), (55, 38), (65, 33)],
                  "F": [(25, 41), (35, 39), (45, 36), (55, 33), (65, 30)]}


def _age_band(age: int) -> str:
    if age < 30:
        return "20-29"
    if age < 40:
        return "30-39"
    if age < 50:
        return "40-49"
    if age < 60:
        return "50-59"
    return "60+"


def _interp_percentile(table, vo2) -> float:
    # table: list of (percentile, vo2) ascending in vo2
    if vo2 <= table[0][1]:
        return float(table[0][0])
    if vo2 >= table[-1][1]:
        return float(table[-1][0])
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if v0 <= vo2 <= v1:
            return p0 + (p1 - p0) * (vo2 - v0) / (v1 - v0)
    return 50.0


def _category(pct):
    if pct >= 95:
        return "Superior", "green"
    if pct >= 80:
        return "Excellent", "green"
    if pct >= 60:
        return "Good", "green"
    if pct >= 40:
        return "Fair", "amber"
    if pct >= 20:
        return "Below average", "amber"
    return "Poor", "red"


def _fitness_age(sex, vo2):
    anchors = _MEDIAN_BY_AGE.get(sex, _MEDIAN_BY_AGE["M"])
    # anchors are (age, median vo2) with vo2 descending as age rises
    if vo2 >= anchors[0][1]:
        return anchors[0][0]  # cap at youngest band
    if vo2 <= anchors[-1][1]:
        return anchors[-1][0]
    for (a0, v0), (a1, v1) in zip(anchors, anchors[1:]):
        if v1 <= vo2 <= v0:
            return round(a0 + (a1 - a0) * (v0 - vo2) / (v0 - v1))
    return None


def vo2_analysis(snapshot: dict, cfg: dict, today: dt.date) -> Optional[dict]:
    training = (snapshot or {}).get("training") or {}
    vo2 = training.get("vo2max_precise") or training.get("vo2max")
    if not vo2:
        return None
    ath = cfg.get("athlete") or {}
    sex = (ath.get("sex") or "M").upper()[:1]
    age = None
    if ath.get("birth_date"):
        try:
            b = dt.date.fromisoformat(ath["birth_date"])
            age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        except Exception:
            age = None
    band = _age_band(age) if age else "30-39"
    table = _NORMS.get(sex, _NORMS["M"]).get(band)
    pct = round(_interp_percentile(table, vo2))
    cat, color = _category(pct)
    fit_age = _fitness_age(sex, vo2)
    # build a cohort curve for the chart (percentile → vo2)
    curve = [{"p": p, "vo2": v} for p, v in table]
    # ensure the trend ends at the current value
    trend = list((snapshot or {}).get("vo2max_trend", []))
    as_of = training.get("as_of")
    if as_of and (not trend or trend[-1]["date"] < as_of):
        trend.append({"date": as_of, "vo2max": round(vo2, 1)})
    return {
        "vo2max": round(vo2, 1),
        "sex": sex, "age": age, "band": band,
        "percentile": pct, "category": cat, "color": color,
        "fitness_age": fit_age,
        "cohort_curve": curve,
        "trend": trend,
    }


# ---------------- Path to the goal and beyond ----------------
def _race_vdots(snapshot: dict) -> list:
    """The athlete's race-distance efforts as VDOTs (distance-agnostic fitness),
    so we can anchor on what they've actually raced, not just Garmin's predictor."""
    out = []
    for r in (snapshot or {}).get("race_history") or []:
        secs, km = r.get("seconds"), r.get("km")
        if not secs or not km or km < 4.5:
            continue
        out.append({"date": r.get("date"), "vdot": round(_vdot_from(km * 1000.0, secs), 1),
                    "dist": r.get("dist"), "seconds": secs, "km": km, "name": r.get("name")})
    return out


def race_targets(snapshot: dict, cfg: dict, _today: Optional[dt.date] = None) -> Optional[dict]:
    _today = _today or dt.date.today()
    rp = (snapshot or {}).get("race_predictions") or {}
    cur_half = rp.get("sec_half")
    # ---- recalibrated fitness anchor ----
    # Garmin's race predictor is training-suppressed and sits below this athlete's
    # VO2max and real races. Blend predictor + VO2max, then pull toward demonstrated
    # recent racing so the projection reflects proven ability, not the pessimistic floor.
    # Fallback path: if Garmin hasn't emitted a race predictor yet, derive from VO2max
    # and race history instead of bailing out to a silent-empty response.
    _vt = (snapshot or {}).get("vo2max_trend") or []
    _vo2 = next((p["vo2max"] for p in reversed(_vt) if p.get("vo2max")), None)
    if not cur_half:
        if not _vo2 and not ((snapshot or {}).get("race_history") or []):
            return {"has_data": False,
                    "missing": ["race_predictions.sec_half", "vo2max_trend", "race_history"],
                    "read": "No Garmin race prediction, VO2max, or race history yet - projection needs at least one."}
        # Synthesize a half-marathon time from the best available fitness proxy.
        seed_vdot = _vo2 or 0
        rh = _race_vdots(snapshot)
        if rh:
            seed_vdot = max(seed_vdot, max(r["vdot"] for r in rh))
        cur_half = _time_for_vdot(HALF_M, seed_vdot) if seed_vdot else None
        if not cur_half:
            return {"has_data": False,
                    "missing": ["race_predictions.sec_half", "vo2max_trend", "race_history"],
                    "read": "Not enough fitness data to project a half time yet."}
    pred_vdot = _vdot_from(HALF_M, cur_half)
    vo2_vdot = _vo2 if _vo2 else pred_vdot
    train_vdot = 0.6 * vo2_vdot + 0.4 * pred_vdot

    def _dago(d):
        try:
            return (_today - dt.date.fromisoformat(d)).days
        except Exception:
            return 99999
    race_vdots = _race_vdots(snapshot)
    recent = [r for r in race_vdots if _dago(r["date"]) <= 425]
    best_recent = max((r["vdot"] for r in recent), default=None)
    best_ever = max(race_vdots, key=lambda r: r["vdot"], default=None)
    if best_recent is not None:
        anchor = min(max(0.55 * train_vdot + 0.45 * best_recent, pred_vdot), vo2_vdot + 2.0)
    else:
        anchor = train_vdot
    cur_vdot = round(anchor, 1)

    preds = [("5K", 5000, rp.get("sec_5k")), ("10K", 10000, rp.get("sec_10k")),
             ("Half", HALF_M, rp.get("sec_half")), ("Marathon", 42195, rp.get("sec_marathon"))]
    predictions = [{"dist": k, "seconds": v, "time": fmt_secs(v),
                    "pace": pace_str(v / (m / 1000.0)) if v else None}
                   for k, m, v in preds if v]
    # Anchored (blended) predictions: our own recalibrated VDOT applied to each
    # distance. Not training-suppressed because the anchor blends Garmin's
    # predictor with VO2max and recent race VDOTs, then pulls toward what's
    # actually been raced. Uses the same `cur_vdot` computed above.
    predictions_anchored = [
        {"dist": k, "seconds": round(_time_for_vdot(m, cur_vdot)),
         "time": fmt_secs(_time_for_vdot(m, cur_vdot)),
         "pace": pace_str(_time_for_vdot(m, cur_vdot) / (m / 1000.0))}
        for k, m, _ in preds
    ]
    # Strava predictions (manual or MCP-sourced). Includes updated_at + source.
    _strava_full = _load_strava_predictions_full()
    predictions_strava = _strava_full["predictions"]
    strava_meta = {"updated_at": _strava_full.get("updated_at"),
                    "source": _strava_full.get("source"),
                    "reference": _strava_full.get("reference")}

    goals = cfg.get("goals") or {}
    race = cfg.get("race") or {}
    race_m = (race.get("distance_km") or 21.1) * 1000.0
    race_lbl = planlib.race_label(cfg)
    ceiling = None
    if best_ever:
        cv = best_ever["vdot"]
        ceiling = {"vdot": cv, "date": best_ever["date"], "dist": best_ever["dist"],
                   "actual_time": fmt_secs(best_ever["seconds"]), "actual_km": best_ever["km"],
                   "race_time": fmt_secs(_time_for_vdot(race_m, cv))}
    # goal times are quoted at the ACTIVE race distance, so VDOT and pace derive
    # from that distance (a 3:00 marathon and a 1:30 half are different VDOTs).
    cur_at_race = _time_for_vdot(race_m, cur_vdot)   # current fitness → this distance
    # milestones: the plan's B/A goals, then two stretch targets beyond
    rows = []
    def _add(label, seconds, kind):
        v = _vdot_from(race_m, seconds)
        rows.append({
            "label": label, "seconds": seconds, "time": fmt_secs(seconds),
            "pace": pace_str(seconds / (race_m / 1000.0)),
            "vdot": round(v, 1),
            "faster_pct": round(100 * (cur_at_race - seconds) / cur_at_race, 1),
            "vdot_gain_pct": round(100 * (v - cur_vdot) / cur_vdot, 1),
            "sec_per_km_gain": round((cur_at_race - seconds) / (race_m / 1000.0)),
            "kind": kind,
        })
    if goals.get("B"):
        _add(goals["B"]["time"] + " (B goal)", goals["B"]["seconds"], "goal")
    if goals.get("A"):
        _add(goals["A"]["time"] + " (A goal)", goals["A"]["seconds"], "primary")
    # two stretch targets ~3% and ~6% beyond the A goal, rounded to a clean time
    a_secs = (goals.get("A") or {}).get("seconds") or round(cur_at_race)
    snap_to = 60 if race_m >= 20000 else 15
    for pct in (0.03, 0.06):
        t = int(round(a_secs * (1 - pct) / snap_to) * snap_to)
        _add(f"{fmt_secs(t)} (stretch)", t, "stretch")

    primary = next((r for r in rows if r["kind"] == "primary"), None)

    # VDOT trend from race-prediction history
    history = (snapshot or {}).get("race_prediction_history") or []
    trend = [{"date": h["date"], "vdot": round(_vdot_from(HALF_M, h["sec_half"]), 1),
              "sec_half": h["sec_half"]}
             for h in history if h.get("sec_half")]
    # shift the (predictor-based) trend so it lands on the recalibrated anchor —
    # same recalibration applied historically, keeping the chart + projection continuous.
    if trend:
        _off = round(cur_vdot - trend[-1]["vdot"], 1)
        if abs(_off) > 0.05:
            for t in trend:
                t["vdot"] = round(t["vdot"] + _off, 1)
    step = max(1, len(trend) // 90)
    trend_s = trend[::step]
    target_vdot = primary["vdot"] if primary else None
    change_30 = change_90 = None
    if len(trend) >= 2:
        change_30 = round(cur_vdot - trend[-min(len(trend), 30)]["vdot"], 1)
        change_90 = round(cur_vdot - trend[-min(len(trend), 90)]["vdot"], 1)

    projection = None
    race = cfg.get("race") or {}
    if race.get("date"):
        try:
            projection = _vdot_projection(trend, cur_vdot, _today, dt.date.fromisoformat(race["date"]), cfg)
        except Exception:
            projection = None
    if projection is not None:
        projection["ceiling"] = ceiling
        if ceiling and primary and ceiling["vdot"] >= primary["vdot"] - 0.3:
            projection["note"] += (f" And you've already raced VDOT {ceiling['vdot']} "
                                   f"({ceiling['actual_time']} for {ceiling['actual_km']} km, {ceiling['date'][:7]}) — "
                                   f"the ceiling for your goal is proven, so this is a rebuild, not new territory.")

    anchor_note = None
    if best_recent is not None and round(cur_vdot, 1) > round(pred_vdot, 1) + 0.2:
        anchor_note = (f"Fitness read recalibrated to VDOT {round(cur_vdot, 1)} (~{fmt_secs(cur_at_race)}). "
                       f"Garmin's raw predictor ({fmt_secs(cur_half)}) is training-suppressed and sits below both "
                       f"your VO₂max ({round(vo2_vdot, 1)}) and the races you've actually run, so we blend them and "
                       f"credit your recent racing.")

    return {
        "current_half": fmt_secs(cur_at_race),
        "current_half_pace": pace_str(cur_at_race / (race_m / 1000.0)),
        "current_race_dist": race_lbl,
        "current_vdot": round(cur_vdot, 1),
        "garmin_predicted": fmt_secs(cur_half),
        "vo2_vdot": round(vo2_vdot, 1),
        "anchor_note": anchor_note,
        "proven_ceiling": ceiling,
        "predictions": predictions,
        "predictions_anchored": predictions_anchored,
        "predictions_strava": predictions_strava,
        "strava_meta": strava_meta,
        "milestones": rows,
        "primary": primary,
        "vdot_trend": trend_s,
        "target_vdot": target_vdot,
        "start_vdot": trend[0]["vdot"] if trend else None,
        "vdot_change_30d": change_30,
        "vdot_change_90d": change_90,
        "projection": projection,
    }


# ---------------- ACWR trend ----------------
def acwr_series(snapshot: dict, today: dt.date) -> Optional[dict]:
    acts = (snapshot or {}).get("activities", [])
    loads: dict[str, float] = {}
    for a in acts:
        d, tl = a.get("date"), a.get("training_load")
        if d and tl:
            loads[d] = loads.get(d, 0.0) + tl
    if not loads:
        return None
    start = dt.date.fromisoformat(min(loads))
    days = (today - start).days
    daily = [loads.get((start + dt.timedelta(days=i)).isoformat(), 0.0) for i in range(days + 1)]
    series = []
    for i in range(len(daily)):
        if i < 27:
            continue
        acute = sum(daily[i - 6:i + 1])
        chronic = sum(daily[i - 27:i + 1]) / 4.0
        ratio = round(acute / chronic, 2) if chronic else None
        if ratio is not None:
            series.append({"date": (start + dt.timedelta(days=i)).isoformat(), "acwr": ratio})
    training = (snapshot or {}).get("training") or {}
    cur = training.get("acwr") or (series[-1]["acwr"] if series else None)
    step = max(1, len(series) // 90)
    return {
        "series": series[::step] if series else [],
        "current": round(cur, 2) if cur else None,
        "status": training.get("acwr_status"),
        "zones": {"sweet_low": 0.8, "sweet_high": 1.3, "caution_high": 1.5},
    }


_STATUS = {
    "PRODUCTIVE": ("Productive", "green", "Your load is building fitness and VO2max is trending up — keep it rolling."),
    "MAINTAINING": ("Maintaining", "green", "Holding fitness. A touch more load would push you forward again."),
    "PEAKING": ("Peaking", "green", "Fitness and freshness are aligned — you're primed to race."),
    "RECOVERY": ("Recovery", "amber", "A lighter stretch letting adaptation happen — exactly right after a hard block."),
    "UNPRODUCTIVE": ("Unproductive", "amber", "Training hard but fitness isn't responding — check sleep, fuelling and easy-day discipline."),
    "OVERREACHING": ("Overreaching", "red", "Acute load is high vs your baseline — ease off before it turns into strain."),
    "STRAINED": ("Strained", "red", "High strain relative to recent fitness — prioritise recovery now."),
    "DETRAINING": ("Detraining", "red", "Load has dropped and fitness is slipping — get consistent volume back in."),
    "NO_STATUS": ("No status", "grey", "Not enough recent data for a training-status read."),
}


def training_status(snapshot: dict) -> dict:
    tr = (snapshot or {}).get("training") or {}
    code = (tr.get("status_feedback") or "").split("_")[0] or "NO_STATUS"
    label, color, read = _STATUS.get(code, ("—", "grey", ""))
    lb = tr.get("load_balance") or {}
    return {
        "label": label, "color": color, "read": read,
        "raw": tr.get("status_feedback"),
        "vo2max": round(tr.get("vo2max_precise") or tr.get("vo2max"), 1) if (tr.get("vo2max_precise") or tr.get("vo2max")) else None,
        "fitness_trend": tr.get("fitness_trend"),
        "load_focus": (lb.get("feedback") or "").replace("_", " ").title() or None,
    }


GLOSSARY = [
    {"term": "Fitness (CTL)", "text": "Your 42-day rolling training load — the long-term work that makes you fit. Rises slowly; the higher it is, the more your body can do."},
    {"term": "Fatigue (ATL)", "text": "Your 7-day rolling load — recent tiredness. Rises and falls fast."},
    {"term": "Form (TSB)", "text": "Fitness minus Fatigue. Positive = fresh (good for racing); slightly negative = the productive training zone; deeply negative = overreached."},
    {"term": "Acute load", "text": "Training load over the last 7 days — how hard you've gone recently."},
    {"term": "Chronic load", "text": "Your typical load over ~4 weeks — the baseline your body is adapted to."},
    {"term": "ACWR", "text": "Acute ÷ Chronic load. 0.8–1.3 is the sweet spot; above ~1.5 sharply raises injury risk — you're adding load faster than you're adapting."},
    {"term": "VO₂max", "text": "The max oxygen your body can use per kg per minute — the ceiling of your aerobic engine. Higher is better and it's strongly tied to distance performance."},
    {"term": "VDOT", "text": "Your effective racing fitness from actual performances (Daniels). Unlike raw VO₂max it already bakes in running economy, so it maps directly to finish times."},
]


_LB = 2.20462


def _weight_impact(cur_kg: float, c90_kg) -> str:
    cur_lb = round(cur_kg * _LB)
    if c90_kg is None:
        return (f"At {cur_lb} lb. Weight drives running economy — each pound is ~1 s/km at race pace and shifts "
                f"relative VO₂max (it's measured per-kg). Steady weight through the block is a stable platform.")
    c90_lb = round(c90_kg * _LB, 1)
    if c90_kg <= -0.6:
        secs = abs(round(c90_kg * 2))
        vo2 = abs(round(100 * c90_kg / cur_kg, 1))
        return (f"Down {abs(c90_lb)} lb over 90 days — roughly {secs} s/km faster at race pace and about a {vo2}% lift "
                f"in relative VO₂max. Free speed toward your goal, provided it's fat, not fuel or muscle.")
    if c90_kg >= 0.6:
        secs = round(c90_kg * 2)
        return (f"Up {c90_lb} lb over 90 days — roughly {secs} s/km slower at race pace and a small drag on relative "
                f"VO₂max. Worth watching into race week; a lighter frame carries goal pace more easily.")
    return (f"Weight is steady (~{cur_lb} lb) over 90 days — a stable platform. You're driving fitness through "
            f"training, not the scale, which is right mid-build.")


def _weight_forecast(cur_lb: float, goal_lb: float,
                     today: dt.date, race_date: dt.date,
                     plan_weeks: list) -> Optional[dict]:
    """Volume-weighted weight-loss trajectory from today to race day.

    Weekly weight loss is distributed across the remaining plan weeks in
    proportion to each week's running volume — bigger-volume weeks shed more
    (energy expenditure gradient), lighter/taper weeks shed much less. We
    reserve ~60% of the taper block (final 3 weeks) as "hold race weight"
    rather than lose, since dropping into race week undercuts glycogen
    stores and fueling capacity.
    """
    if not plan_weeks or goal_lb is None or cur_lb is None:
        return None
    total_to_lose = cur_lb - goal_lb
    if total_to_lose <= 0:
        return None
    # Forward-looking plan weeks (start_date >= today) up to race day.
    forward = []
    for w in plan_weeks:
        try:
            s = dt.date.fromisoformat(w.get("start_date") or "")
        except Exception:
            continue
        if s >= today and s <= race_date:
            forward.append({"start": s, "km": float(w.get("target_km") or 0),
                            "phase": w.get("phase", "")})
    if not forward:
        return None
    # Determine taper weeks (last 3 before the race) — hold weight through these.
    taper_cut = race_date - dt.timedelta(days=21)
    hold_weight_weeks = {w["start"] for w in forward if w["start"] >= taper_cut}
    # Volume-weight each non-taper week, zero-weight taper weeks.
    def _loss_weight(w):
        if w["start"] in hold_weight_weeks:
            return 0.0
        return max(w["km"], 20.0)   # floor so a cutback week still contributes some
    weights = [_loss_weight(w) for w in forward]
    total_w = sum(weights) or 1.0
    series = []
    running_lb = cur_lb
    series.append({"date": today.isoformat(), "val": round(cur_lb, 1), "phase": "today"})
    for w, wt in zip(forward, weights):
        drop = total_to_lose * (wt / total_w)
        running_lb = max(goal_lb, running_lb - drop)
        series.append({"date": w["start"].isoformat(), "val": round(running_lb, 1),
                       "phase": w["phase"], "km": w["km"], "drop_lb": round(drop, 2)})
    series.append({"date": race_date.isoformat(), "val": round(goal_lb, 1), "phase": "race day"})
    return {
        "start_lb": round(cur_lb, 1),
        "goal_lb": round(goal_lb, 1),
        "to_lose_lb": round(total_to_lose, 1),
        "weeks": len(forward),
        "avg_loss_per_week_lb": round(total_to_lose / max(1, len(forward)), 2),
        "series": series,
        "method": ("Weekly loss distributed proportionally to planned km; final 3 weeks "
                   "(taper) hold race weight. Expect bigger drops on 85-100 km weeks and "
                   "near-zero loss on cutback and taper weeks."),
    }


def weight_analysis(snapshot: dict, today: Optional[dt.date] = None,
                    cfg: Optional[dict] = None, plan: Optional[dict] = None) -> Optional[dict]:
    today = today or dt.date.today()
    w = [x for x in (snapshot or {}).get("weight", []) if x.get("kg")]
    cfg = cfg or {}
    athlete = cfg.get("athlete") or {}
    goal_lb = athlete.get("goal_weight_lb")
    fallback_lb = athlete.get("current_weight_lb_fallback")

    if w:
        cur_kg = w[-1]["kg"]
        last_date = dt.date.fromisoformat(w[-1]["date"])
        def _ago(days):
            cutoff = last_date - dt.timedelta(days=days)
            past = [x for x in w if dt.date.fromisoformat(x["date"]) <= cutoff]
            return past[-1]["kg"] if past else None
        a30, a90 = _ago(30), _ago(90)
        c30_kg = cur_kg - a30 if a30 else None
        c90_kg = cur_kg - a90 if a90 else None
        range_lb = [round(min(x["kg"] for x in w) * _LB, 1), round(max(x["kg"] for x in w) * _LB, 1)]
        step = max(1, len(w) // 90)
        trend = [{"date": x["date"], "val": round(x["kg"] * _LB, 1)} for x in w[::step]]
        cur_lb = round(cur_kg * _LB, 1)
    else:
        # No scale sync yet — use the fallback so we can still draw the forecast.
        if fallback_lb is None:
            return None
        cur_lb = float(fallback_lb)
        cur_kg = cur_lb / _LB
        a30 = a90 = None
        c30_kg = c90_kg = None
        range_lb = [cur_lb, cur_lb]
        trend = []

    runs = [r for r in (snapshot or {}).get("activities", []) if r.get("avg_power") and r["avg_power"] >= 150]
    tail = runs[-8:]
    recent_pwr = round(sum(r["avg_power"] for r in tail) / len(tail)) if tail else None
    wkg = round(recent_pwr / cur_kg, 2) if recent_pwr and cur_kg else None

    # Forecast toward goal weight, aligned with planned training volume.
    forecast = None
    race = cfg.get("race") or {}
    race_date_s = race.get("date")
    try:
        race_date = dt.date.fromisoformat(race_date_s) if race_date_s else None
    except Exception:
        race_date = None
    plan_weeks = (plan or {}).get("weeks") or []
    if race_date and goal_lb is not None:
        forecast = _weight_forecast(cur_lb, float(goal_lb), today, race_date, plan_weeks)

    return {
        "unit": "lb",
        "current": cur_lb,
        "goal": goal_lb,
        "change_30": round(c30_kg * _LB, 1) if c30_kg is not None else None,
        "change_90": round(c90_kg * _LB, 1) if c90_kg is not None else None,
        "range": range_lb,
        "w_per_kg": wkg, "recent_power": recent_pwr,
        "trend": trend,
        "forecast": forecast,
        "impact": _weight_impact(cur_kg, c90_kg),
    }


def analyze_fitness(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    cfg = resolve_active_race(load_config(), today)
    if not snapshot:
        return {"has_data": False, "message": "No fitness data synced yet — refresh from Garmin."}
    # Plan is needed so weight_forecast can allocate loss by weekly volume.
    try:
        from planlib import load_plan
        plan = load_plan()
    except Exception:
        plan = None
    return {
        "has_data": True,
        "form": performance_chart(snapshot, today),
        "vo2": vo2_analysis(snapshot, cfg, today),
        "race": race_targets(snapshot, cfg, today),
        "weight": weight_analysis(snapshot, today=today, cfg=cfg, plan=plan),
        "acwr": acwr_series(snapshot, today),
        "status": training_status(snapshot),
        "glossary": GLOSSARY,
        "lactate_threshold": (snapshot or {}).get("lactate_threshold"),
    }
