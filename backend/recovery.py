"""Recovery & readiness analysis — the console a world-class endurance athlete
would use to decide whether the body is ready to train hard.

Pulls together Garmin's Training Readiness, overnight HRV, resting HR, and Body
Battery; reads each against the athlete's OWN 2-year baseline (what matters most)
and, for rough context, against age/sex population norms; watches for the
overreaching signature; and explains what each number means plus the pitfalls to
avoid (chasing daily HRV, ignoring the trend, etc.).
"""
from __future__ import annotations
import datetime as dt
import statistics
from typing import Optional

from planlib import load_config

import cache


# ---------- population norms (approximate; heavily caveated for HRV) ----------
# RHR: lower is better. (percentile, bpm) with percentile FALLING as bpm rises.
_RHR_NORMS = {
    "M": {"20-29": [(95, 49), (90, 53), (75, 58), (50, 64), (25, 70), (10, 76)],
          "30-39": [(95, 50), (90, 54), (75, 59), (50, 65), (25, 71), (10, 77)],
          "40-49": [(95, 51), (90, 55), (75, 60), (50, 66), (25, 72), (10, 78)],
          "50-59": [(95, 52), (90, 57), (75, 62), (50, 67), (25, 73), (10, 79)],
          "60+":   [(95, 53), (90, 58), (75, 62), (50, 68), (25, 74), (10, 80)]},
    "F": {"20-29": [(95, 54), (90, 58), (75, 62), (50, 68), (25, 74), (10, 80)],
          "30-39": [(95, 54), (90, 58), (75, 63), (50, 68), (25, 74), (10, 80)],
          "40-49": [(95, 55), (90, 59), (75, 64), (50, 69), (25, 75), (10, 81)],
          "50-59": [(95, 55), (90, 60), (75, 64), (50, 69), (25, 75), (10, 82)],
          "60+":   [(95, 56), (90, 60), (75, 65), (50, 70), (25, 76), (10, 82)]},
}
# HRV (overnight, rMSSD-like ms): higher is better. (percentile, ms) rising.
_HRV_NORMS = {
    "M": {"20-29": [(10, 30), (25, 40), (50, 55), (75, 72), (90, 90), (95, 105)],
          "30-39": [(10, 25), (25, 33), (50, 46), (75, 60), (90, 76), (95, 88)],
          "40-49": [(10, 20), (25, 28), (50, 38), (75, 50), (90, 64), (95, 74)],
          "50-59": [(10, 16), (25, 23), (50, 32), (75, 42), (90, 55), (95, 64)],
          "60+":   [(10, 14), (25, 20), (50, 28), (75, 37), (90, 48), (95, 56)]},
    "F": {"20-29": [(10, 28), (25, 38), (50, 52), (75, 68), (90, 86), (95, 100)],
          "30-39": [(10, 24), (25, 32), (50, 44), (75, 58), (90, 73), (95, 84)],
          "40-49": [(10, 19), (25, 27), (50, 37), (75, 48), (90, 62), (95, 72)],
          "50-59": [(10, 16), (25, 22), (50, 31), (75, 41), (90, 53), (95, 62)],
          "60+":   [(10, 13), (25, 19), (50, 27), (75, 36), (90, 47), (95, 55)]},
}


def _age_band(age):
    if not age:
        return "30-39"
    return "20-29" if age < 30 else "30-39" if age < 40 else "40-49" if age < 50 else "50-59" if age < 60 else "60+"


def _pct_hrv(ms, table):
    if ms <= table[0][1]:
        return table[0][0]
    if ms >= table[-1][1]:
        return table[-1][0]
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if v0 <= ms <= v1:
            return round(p0 + (p1 - p0) * (ms - v0) / (v1 - v0))
    return 50


def _pct_rhr(bpm, table):
    # table percentile descends as bpm ascends
    if bpm <= table[0][1]:
        return table[0][0]
    if bpm >= table[-1][1]:
        return table[-1][0]
    for (p0, v0), (p1, v1) in zip(table, table[1:]):
        if v0 <= bpm <= v1:
            return round(p0 + (p1 - p0) * (bpm - v0) / (v1 - v0))
    return 50


def _category(pct):
    if pct >= 90:
        return "Excellent", "green"
    if pct >= 70:
        return "Good", "green"
    if pct >= 40:
        return "Average", "amber"
    if pct >= 20:
        return "Below average", "amber"
    return "Low", "red"


def _athlete(cfg, today):
    ath = cfg.get("athlete") or {}
    sex = (ath.get("sex") or "M").upper()[:1]
    age = None
    if ath.get("birth_date"):
        try:
            b = dt.date.fromisoformat(ath["birth_date"])
            age = today.year - b.year - ((today.month, today.day) < (b.month, b.day))
        except Exception:
            age = None
    return sex, age


_READY = {
    "WELL_RECOVERED": ("Well recovered", "green", "Your body is ready — a good day to take on a hard session or a long run. Use the green light."),
    "RECOVERY_IN_PROGRESS": ("Recovery in progress", "amber", "Still bouncing back. Keep it easy-to-moderate today and save the hard work for a greener day."),
    "HIGH_RECOVERY_NEEDS": ("High recovery need", "red", "A clear fatigue signal — prioritize easy movement, food and sleep. Don't force quality today."),
    "MODERATE_RECOVERY": ("Moderate recovery", "amber", "Middling readiness. Run by feel — start the warm-up and let the legs tell you whether to push."),
    "LOW_RECOVERY": ("Low recovery", "red", "Under-recovered. Favour rest or very easy Z2 and re-check tomorrow."),
}


def _readiness_read(level, feedback):
    if feedback in _READY:
        return _READY[feedback]
    return {"HIGH": ("Ready", "green", "Recovery markers are strong — clear to train hard."),
            "MODERATE": ("Moderate", "amber", "Middling — run by feel, keep hard work in check."),
            "LOW": ("Back off", "red", "Under-recovered — favour rest or easy today.")}.get(
        (level or "").upper(), ("—", "grey", ""))


def _clean_hrv(v):
    return v if (v is not None and 15 <= v <= 200) else None


def analyze_recovery(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    cfg = load_config()
    if not snapshot:
        return {"has_data": False, "message": "No recovery data synced yet — refresh from Garmin."}
    sex, age = _athlete(cfg, today)
    band = _age_band(age)
    bl = cache.load_baselines() or {}

    out = {"has_data": True, "sex": sex, "age": age, "band": band}

    # ---- Training Readiness ----
    lr = snapshot.get("latest_readiness") or {}
    if lr.get("score") is not None:
        label, color, guide = _readiness_read(lr.get("level"), lr.get("feedback"))
        # rank the 0–100 sub-scores to find what's holding readiness back
        subs = [(n, v) for n, v in [("Recovery time", lr.get("recovery_pct")),
                                    ("Sleep", lr.get("sleep_score"))] if v is not None]
        subs.sort(key=lambda x: x[1])
        limiter = None
        if subs and subs[0][1] < 85:
            limiter = f"**{subs[0][0]}** ({subs[0][1]}) is what's capping today's readiness — improving it lifts the score most."
        elif subs and all(v >= 90 for _, v in subs):
            limiter = "Every driver is strong — nothing's holding your readiness back today."
        rec_min = lr.get("recovery_time_min")
        rec_h = round(rec_min / 60) if rec_min else (0 if rec_min == 0 else None)
        rec_read = None
        if rec_h is not None:
            if rec_h <= 1:
                rec_read = "You're recovered — Garmin rates you primed for a hard/beneficial effort."
            elif rec_h <= 8:
                rec_read = (f"~{rec_h} h until you're fully primed for hard work. Easy running is fine now; "
                            f"a quality session would be slightly compromised.")
            elif rec_h <= 16:
                rec_read = (f"~{rec_h} h of recovery left. Keep today easy — a hard effort now would mostly bank "
                            f"fatigue. Save quality for when this clears.")
            else:
                rec_read = (f"~{rec_h} h of recovery left — you're still absorbing recent load. Hard work should wait; "
                            f"easy Z2 or rest today actually speeds this down.")
        out["readiness"] = {
            "score": lr.get("score"), "level": lr.get("level"),
            "label": label, "color": color, "guidance": guide, "limiter": limiter,
            "recovery_pct": lr.get("recovery_pct"), "sleep_score": lr.get("sleep_score"),
            "recovery_hours": rec_h, "recovery_read": rec_read,
            "acwr_pct": lr.get("acwr_pct"), "acute_load": lr.get("acute_load"),
            "hrv_weekly": _clean_hrv(lr.get("hrv_weekly_avg")),
        }
    rt = [r for r in snapshot.get("readiness_trend", []) if r.get("score") is not None]
    out["readiness_series"] = [{"date": r["date"], "score": r["score"]} for r in rt]

    # ---- HRV ----
    hrv_hist = [h for h in snapshot.get("hrv", []) if h.get("last_night_avg") is not None]
    if hrv_hist:
        latest = hrv_hist[-1]
        lo = latest.get("balanced_low") or (bl.get("hrv") or {}).get("balanced_low") or (snapshot.get("hrv_baseline") or {}).get("low")
        hi = latest.get("balanced_upper") or (bl.get("hrv") or {}).get("balanced_high") or (snapshot.get("hrv_baseline") or {}).get("upper")
        last = latest["last_night_avg"]
        avg7 = latest.get("weekly_avg") or round(statistics.mean(h["last_night_avg"] for h in hrv_hist[-7:]))
        vals = [h["last_night_avg"] for h in hrv_hist]
        best = (bl.get("hrv") or {}).get("all_time_best") or max(vals)
        state = "in"
        if lo and hi:
            state = "below" if last < lo else "above" if last > hi else "in"
        pct = _pct_hrv(avg7, _HRV_NORMS.get(sex, _HRV_NORMS["M"]).get(band))
        cat, ccolor = _category(pct)
        # 7-night rolling average — the trend that matters, not nightly noise
        rolling = []
        for i in range(len(vals)):
            w = vals[max(0, i - 6):i + 1]
            rolling.append(round(sum(w) / len(w), 1))
        out["hrv"] = {
            "last": last, "avg7": avg7, "low": lo, "high": hi, "best": best,
            "status": latest.get("status"), "state": state,
            "percentile": pct, "category": cat, "cat_color": ccolor,
            "series": [{"date": h["date"], "hrv": h["last_night_avg"], "roll": rolling[i]}
                       for i, h in enumerate(hrv_hist)],
            "read": _hrv_read(last, lo, hi, state, pct, sex, band),
        }

    # ---- Resting HR ----
    rhr_hist = [r for r in snapshot.get("rhr_trend", []) if r.get("rhr")]
    rhr_bl = bl.get("rhr") or {}
    if rhr_hist:
        last = round(rhr_hist[-1]["rhr"])
        rested = rhr_bl.get("recent_rested")
        base = rhr_bl.get("recent_baseline")
        elevated = rhr_bl.get("recent_elevated")
        best = rhr_bl.get("all_time_low") or round(min(r["rhr"] for r in rhr_hist))
        pct = _pct_rhr(last, _RHR_NORMS.get(sex, _RHR_NORMS["M"]).get(band))
        cat, ccolor = _category(pct)
        rstate = "excellent" if (rested and last <= rested + 2) else "elevated" if (base and last > base + 4) else "normal"
        out["rhr"] = {
            "last": last, "rested": rested, "baseline": base, "elevated": elevated, "best": best,
            "percentile": pct, "category": cat, "cat_color": ccolor, "state": rstate,
            "all_time_high": rhr_bl.get("all_time_high"),
            "series": [{"date": r["date"], "rhr": round(r["rhr"])} for r in rhr_hist],
            "read": _rhr_read(last, rested, base, rstate, pct, sex, band),
        }

    # ---- Body Battery ----
    bb = [b for b in snapshot.get("body_battery", []) if b.get("high") is not None]
    if bb:
        latest = bb[-1]
        out["body_battery"] = {
            "high": latest.get("high"), "low": latest.get("low"),
            "charged": latest.get("charged"), "drained": latest.get("drained"),
            "series": [{"date": b["date"], "high": b.get("high"), "low": b.get("low"),
                        "charged": b.get("charged")} for b in bb],
        }

    # ---- Time on feet + non-activity stress (daily + weekly) ----
    wd = [w for w in (snapshot.get("wellness_daily") or []) if w.get("date")]
    if wd:
        out["time_on_feet"] = _time_on_feet(wd)
        out["stress"] = _stress_breakdown(wd)

    # ---- overreaching watch ----
    out["overreaching"] = _overreaching(out.get("hrv"), out.get("rhr"), lr)

    # ---- tiles ----
    out["tiles"] = _tiles(out)
    out["glossary"] = _GLOSSARY
    out["pitfalls"] = _PITFALLS
    return out


def _iso_to_date(iso: str):
    try:
        return dt.date.fromisoformat(iso[:10])
    except Exception:
        return None


def _week_key(iso: str):
    """ISO week Monday date as YYYY-MM-DD, or None."""
    d = _iso_to_date(iso)
    if not d:
        return None
    return (d - dt.timedelta(days=d.weekday())).isoformat()


def _sec_to_min(s):
    return round((s or 0) / 60)


def _time_on_feet(wd: list[dict]) -> dict:
    """Time-on-feet daily + weekly view.

    Uses Garmin's per-day intensity buckets directly (they partition your
    awake time so they don't double-count):
      - workout / intense  = highlyActiveSeconds (vigorous, >~6 METs; mostly
        runs, hard cycling, structured workouts)
      - walking / moderate = activeSeconds       (1.5–6 METs; normal walking
        around the house, errands, easy movement — this is your non-activity
        step time)
      - sedentary          = sedentarySeconds    (awake but stationary)
    Steps are surfaced alongside so you can see whether high walking minutes
    line up with high step counts (they should)."""
    daily = []
    for w in wd:
        workout_s = w.get("highly_active_s") or 0     # vigorous intensity — workouts
        walking_s = w.get("active_s") or 0            # moderate — non-activity walking
        act = workout_s + walking_s                   # total on-feet
        awake = act + (w.get("sedentary_s") or 0)
        daily.append({
            "date": w.get("date"),
            "feet_min": _sec_to_min(act),
            "workout_min": _sec_to_min(workout_s),
            "walking_min": _sec_to_min(walking_s),
            "sedentary_min": _sec_to_min(w.get("sedentary_s")),
            "awake_min": _sec_to_min(awake),
            "steps": w.get("total_steps"),
            "distance_km": round((w.get("distance_m") or 0) / 1000.0, 2),
            "moderate_min": w.get("moderate_min") or 0,
            "vigorous_min": w.get("vigorous_min") or 0,
            "floors_up": w.get("floors_up") or 0,
        })

    # weekly rollup (Monday-anchored, average per day inside each week)
    buckets: dict = {}
    for r in daily:
        wk = _week_key(r["date"])
        if not wk:
            continue
        b = buckets.setdefault(wk, {"days": 0, "feet_min": 0, "workout_min": 0,
                                    "walking_min": 0, "sedentary_min": 0,
                                    "steps": 0, "distance_km": 0.0,
                                    "moderate_min": 0, "vigorous_min": 0})
        b["days"] += 1
        b["feet_min"] += r["feet_min"]; b["workout_min"] += r["workout_min"]
        b["walking_min"] += r["walking_min"]; b["sedentary_min"] += r["sedentary_min"]
        b["steps"] += (r["steps"] or 0); b["distance_km"] += (r["distance_km"] or 0)
        b["moderate_min"] += (r["moderate_min"] or 0); b["vigorous_min"] += (r["vigorous_min"] or 0)
    weekly = []
    for wk, b in sorted(buckets.items()):
        n = max(1, b["days"])
        weekly.append({
            "week": wk, "days": b["days"],
            "avg_feet_min": round(b["feet_min"] / n),
            "avg_workout_min": round(b["workout_min"] / n),
            "avg_walking_min": round(b["walking_min"] / n),
            "avg_sedentary_min": round(b["sedentary_min"] / n),
            "total_steps": b["steps"],
            "avg_steps": round(b["steps"] / n),
            "total_distance_km": round(b["distance_km"], 1),
            "total_moderate_min": b["moderate_min"],
            "total_vigorous_min": b["vigorous_min"],
            "total_intensity_min": b["moderate_min"] + 2 * b["vigorous_min"],  # WHO weighting
        })

    latest = daily[-1] if daily else {}
    avg7_feet = round(sum(d["feet_min"] for d in daily[-7:]) / max(1, len(daily[-7:])))
    insights = _feet_insights(daily)
    return {
        "daily": daily,
        "weekly": weekly,
        "today_feet_min": latest.get("feet_min"),
        "today_steps": latest.get("steps"),
        "today_workout_min": latest.get("workout_min"),
        "today_walking_min": latest.get("walking_min"),
        "today_sedentary_min": latest.get("sedentary_min"),
        "avg7_feet_min": avg7_feet,
        "read": _feet_read(latest, avg7_feet),
        "insights": insights,
    }


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs)) if xs else None


def _split_weekday_weekend(daily: list[dict], key: str):
    wk, we = [], []
    for r in daily:
        d = _iso_to_date(r.get("date") or "")
        if not d:
            continue
        v = r.get(key)
        if v is None:
            continue
        (we if d.weekday() >= 5 else wk).append(v)
    return _mean(wk), _mean(we), len(wk), len(we)


def _feet_insights(daily: list[dict]) -> dict:
    """Trend observations + coaching cues from the last 21 days of on-feet data."""
    recent = daily[-21:] if len(daily) > 21 else daily[:]
    if not recent:
        return {}

    # --- Weekday vs weekend ---
    wk_feet, we_feet, wk_n, we_n = _split_weekday_weekend(recent, "feet_min")
    wk_walk, we_walk, _, _ = _split_weekday_weekend(recent, "walking_min")
    wk_sed,  we_sed,  _, _ = _split_weekday_weekend(recent, "sedentary_min")

    # --- Zone share of awake time (last 7d avg) ---
    last7 = recent[-7:]
    tot_workout = sum(d["workout_min"] for d in last7)
    tot_walking = sum(d["walking_min"] for d in last7)
    tot_sed = sum(d["sedentary_min"] for d in last7)
    awake = tot_workout + tot_walking + tot_sed
    if awake > 0:
        zones = {
            "workout_pct":  round(100 * tot_workout / awake),
            "walking_pct":  round(100 * tot_walking / awake),
            "sedentary_pct": round(100 * tot_sed / awake),
        }
    else:
        zones = {}

    # --- High-feet outliers (14d) ---
    fm = [d["feet_min"] for d in recent[-14:] if d.get("feet_min") is not None]
    high_days = []
    if fm:
        mean_fm = sum(fm) / len(fm)
        std_fm = (sum((x - mean_fm) ** 2 for x in fm) / len(fm)) ** 0.5
        thresh = mean_fm + max(30, 1.2 * std_fm)
        for d in recent[-14:]:
            if (d.get("feet_min") or 0) >= thresh:
                high_days.append({"date": d["date"], "feet_min": d["feet_min"],
                                  "steps": d.get("steps"), "delta_vs_avg": round(d["feet_min"] - mean_fm)})

    # --- Sedentary streak flag (14d)  — days >75% of awake seated ---
    sedentary_heavy = 0
    for d in recent[-14:]:
        awake_d = (d.get("feet_min") or 0) + (d.get("sedentary_min") or 0)
        if awake_d > 0 and (d.get("sedentary_min") or 0) / awake_d >= 0.75:
            sedentary_heavy += 1

    # --- Suggestions (recovery-oriented) ---
    tips = []
    if zones and zones.get("sedentary_pct", 0) >= 65:
        tips.append(f"You're sedentary about {zones['sedentary_pct']}% of your awake time — break long sits with a 3–5 min walk each hour; this alone shifts autonomic tone toward recovery.")
    if wk_walk is not None and we_walk is not None:
        if we_walk - wk_walk >= 45:
            tips.append(f"Weekends bring {we_walk - wk_walk} more minutes of walking than weekdays — nice buffer, and it helps clear post-long-run stiffness.")
        elif wk_walk - we_walk >= 45:
            tips.append(f"Weekend walking dips {wk_walk - we_walk} min below weekdays — add one 30-min easy walk on Sat/Sun to keep the parasympathetic-recovery habit going.")
    if wk_sed is not None and wk_sed >= 600:
        tips.append(f"Weekday sedentary time averages {round(wk_sed/60,1)}h — desk-heavy days undo some of what training builds. A stand-up + 5-min walk each hour is the highest-leverage change.")
    if sedentary_heavy >= 4:
        tips.append(f"{sedentary_heavy} of the last 14 days were 'chair days' (>75% sedentary). A daily 20–30 min post-work walk would offset that without adding training load.")
    # Post-workout recovery walk cue
    hard_and_sed = 0
    for d in recent[-14:]:
        if (d.get("workout_min") or 0) >= 45 and (d.get("walking_min") or 0) < 60:
            hard_and_sed += 1
    if hard_and_sed >= 3:
        tips.append("On hard-workout days, walking minutes are low — a 15–20 min easy walk 2h post-run boosts blood flow and calms sympathetic drive, without stealing recovery.")
    if not tips:
        tips.append("On-feet balance looks solid — keep the current rhythm.")

    return {
        "weekday_avg_feet_min": wk_feet, "weekend_avg_feet_min": we_feet,
        "weekday_avg_walking_min": wk_walk, "weekend_avg_walking_min": we_walk,
        "weekday_avg_sedentary_min": wk_sed, "weekend_avg_sedentary_min": we_sed,
        "weekday_days": wk_n, "weekend_days": we_n,
        "zones_last7": zones,
        "high_days": high_days,
        "sedentary_heavy_days": sedentary_heavy,
        "tips": tips,
    }


def _feet_read(latest: dict, avg7: int) -> str:
    fm = latest.get("feet_min") or 0
    sm = latest.get("sedentary_min") or 0
    wm = latest.get("workout_min") or 0
    if fm == 0:
        return "No movement data synced yet for today."
    parts = [f"{fm} min on feet today ({round(fm/60,1)}h)"]
    if wm:
        parts.append(f"{wm} min of that in a workout")
    if sm:
        parts.append(f"{sm} min sedentary")
    delta = fm - avg7
    if abs(delta) >= 30:
        parts.append(f"{'+' if delta > 0 else ''}{delta} min vs your 7-day average ({avg7})")
    return " · ".join(parts) + "."


def _stress_breakdown(wd: list[dict]) -> dict:
    """Non-activity stress daily + weekly.

    The four stress buckets (rest/low/med/high) already exclude time Garmin
    classes as an activity — they represent your autonomic state while awake
    and not training. Rest is parasympathetic-dominant recovery time; low is
    normal alertness; medium and high mean sympathetic drive is up. Total
    non-activity time = the four buckets summed."""
    daily = []
    for w in wd:
        rest = w.get("rest_stress_s") or 0
        low = w.get("low_stress_s") or 0
        med = w.get("med_stress_s") or 0
        high = w.get("high_stress_s") or 0
        total = rest + low + med + high
        # non-activity avg = time-weighted mean of bucket midpoints (rest≈15,
        # low≈38, med≈63, high≈88 on the 0–100 stress scale)
        na_avg = None
        if total > 0:
            na_avg = round((rest * 15 + low * 38 + med * 63 + high * 88) / total)
        daily.append({
            "date": w.get("date"),
            "rest_min": _sec_to_min(rest),
            "low_min": _sec_to_min(low),
            "med_min": _sec_to_min(med),
            "high_min": _sec_to_min(high),
            "total_min": _sec_to_min(total),
            "avg_stress": w.get("avg_stress"),
            "max_stress": w.get("max_stress"),
            "non_activity_avg": na_avg,
        })

    buckets: dict = {}
    for r in daily:
        wk = _week_key(r["date"])
        if not wk:
            continue
        b = buckets.setdefault(wk, {"days": 0, "rest_min": 0, "low_min": 0,
                                    "med_min": 0, "high_min": 0, "avg_sum": 0,
                                    "avg_n": 0, "na_sum": 0, "na_n": 0})
        b["days"] += 1
        b["rest_min"] += r["rest_min"]; b["low_min"] += r["low_min"]
        b["med_min"] += r["med_min"]; b["high_min"] += r["high_min"]
        if r["avg_stress"] is not None:
            b["avg_sum"] += r["avg_stress"]; b["avg_n"] += 1
        if r["non_activity_avg"] is not None:
            b["na_sum"] += r["non_activity_avg"]; b["na_n"] += 1
    weekly = []
    for wk, b in sorted(buckets.items()):
        n = max(1, b["days"])
        weekly.append({
            "week": wk, "days": b["days"],
            "avg_rest_min": round(b["rest_min"] / n),
            "avg_low_min": round(b["low_min"] / n),
            "avg_med_min": round(b["med_min"] / n),
            "avg_high_min": round(b["high_min"] / n),
            "avg_stress": round(b["avg_sum"] / b["avg_n"]) if b["avg_n"] else None,
            "avg_non_activity": round(b["na_sum"] / b["na_n"]) if b["na_n"] else None,
        })

    latest = daily[-1] if daily else {}
    avg7 = [d for d in daily[-7:] if d.get("non_activity_avg") is not None]
    avg7_val = round(sum(d["non_activity_avg"] for d in avg7) / len(avg7)) if avg7 else None
    insights = _stress_insights(daily)
    return {
        "daily": daily,
        "weekly": weekly,
        "today_avg_stress": latest.get("avg_stress"),
        "today_non_activity_avg": latest.get("non_activity_avg"),
        "today_rest_min": latest.get("rest_min"),
        "today_low_min": latest.get("low_min"),
        "today_med_min": latest.get("med_min"),
        "today_high_min": latest.get("high_min"),
        "avg7_non_activity": avg7_val,
        "read": _stress_read(latest, avg7_val),
        "insights": insights,
    }


def _stress_insights(daily: list[dict]) -> dict:
    """Trend observations + coaching cues from the last 21 days of stress data."""
    recent = daily[-21:] if len(daily) > 21 else daily[:]
    if not recent:
        return {}

    # --- Weekday vs weekend (non-activity avg 0–100) ---
    wk_na, we_na, wk_n, we_n = _split_weekday_weekend(recent, "non_activity_avg")
    wk_high, we_high, _, _  = _split_weekday_weekend(recent, "high_min")
    wk_rest, we_rest, _, _  = _split_weekday_weekend(recent, "rest_min")

    # --- Zone share of non-activity time (last 7d avg) ---
    last7 = recent[-7:]
    tot_rest = sum(d["rest_min"] for d in last7)
    tot_low  = sum(d["low_min"]  for d in last7)
    tot_med  = sum(d["med_min"]  for d in last7)
    tot_high = sum(d["high_min"] for d in last7)
    tot = tot_rest + tot_low + tot_med + tot_high
    if tot > 0:
        zones = {
            "rest_pct":   round(100 * tot_rest / tot),
            "low_pct":    round(100 * tot_low  / tot),
            "medium_pct": round(100 * tot_med  / tot),
            "high_pct":   round(100 * tot_high / tot),
        }
    else:
        zones = {}

    # --- High-stress outliers (14d): days > baseline + 10 on non-activity avg ---
    na_vals = [d["non_activity_avg"] for d in recent[-14:] if d.get("non_activity_avg") is not None]
    high_days = []
    baseline = None
    if na_vals:
        baseline = round(sum(na_vals) / len(na_vals))
        thresh = baseline + 10
        for d in recent[-14:]:
            v = d.get("non_activity_avg")
            if v is None:
                continue
            if v >= thresh or (d.get("high_min") or 0) >= 90:
                high_days.append({"date": d["date"], "non_activity_avg": v,
                                  "high_min": d.get("high_min"), "delta_vs_avg": v - baseline})

    # --- Rest-tier deficiency (14d) ---
    rest_avg = _mean([d.get("rest_min") for d in recent[-14:]])
    high_avg = _mean([d.get("high_min") for d in recent[-14:]])

    # --- Suggestions ---
    tips = []
    if zones:
        if zones.get("high_pct", 0) >= 15:
            tips.append(f"About {zones['high_pct']}% of your awake, non-workout time is in the HIGH stress zone — sustained sympathetic drive. Two brief breath-work windows a day (4-7-8 or box breathing, 5 min each) noticeably drop this within a week.")
        if zones.get("rest_pct", 0) < 25:
            tips.append(f"Only {zones.get('rest_pct',0)}% of your awake time is in the REST tier — the parasympathetic recovery zone. Aim for 2–3 daily 'quiet windows' (reading, light stretch, no screens) to build capacity here — this is where HRV rebuilds.")
        elif zones.get("rest_pct", 0) >= 45:
            tips.append(f"Rest-tier share is strong ({zones['rest_pct']}%) — your nervous system has real recovery time. Protect this.")

    if wk_na is not None and we_na is not None:
        if we_na - wk_na >= 6:
            tips.append(f"Weekends run ~{we_na - wk_na} pts hotter on stress than weekdays — often social/family/travel load. Consider whether this offsets the training-week recovery you're chasing.")
        elif wk_na - we_na >= 6:
            tips.append(f"Weekdays run ~{wk_na - we_na} pts hotter than weekends — desk/work load is the main stressor. Weekends are doing their job; try inserting one weekday 'mini reset' (20-min walk, no phone) mid-afternoon.")

    if high_avg is not None and high_avg >= 90:
        tips.append(f"You're averaging {round(high_avg/60,1)}h/day in the high-stress zone — that's a lot of sympathetic time. Alcohol, caffeine after 2 pm, and late-evening screens all push high-tier minutes up; trimming one for 2 weeks usually moves this most.")
    if rest_avg is not None and rest_avg < 45:
        tips.append(f"Only ~{rest_avg} min/day of rest-tier time on average — build in short recovery bookends: 10 min quiet before breakfast and after dinner. Small doses, done daily, outperform occasional long ones.")

    if len(high_days) >= 4:
        tips.append(f"{len(high_days)} of the last 14 days ran hot vs your baseline — check whether they cluster (a stressful project week) or scatter (lifestyle habit) before deciding what to change.")

    if not tips:
        tips.append("Stress balance looks well-managed — the mix of rest / low / medium time is where recovery lives.")

    return {
        "weekday_avg_non_activity": wk_na, "weekend_avg_non_activity": we_na,
        "weekday_avg_high_min":     wk_high, "weekend_avg_high_min":     we_high,
        "weekday_avg_rest_min":     wk_rest, "weekend_avg_rest_min":     we_rest,
        "weekday_days": wk_n, "weekend_days": we_n,
        "zones_last7": zones,
        "baseline_non_activity": baseline,
        "high_days": high_days,
        "avg14_rest_min": rest_avg, "avg14_high_min": high_avg,
        "tips": tips,
    }


def _stress_read(latest: dict, avg7: int | None) -> str:
    na = latest.get("non_activity_avg")
    if na is None:
        return "No stress data synced yet for today."
    if na < 25:
        label = "mostly rest-tier — parasympathetic dominant, well recovered"
    elif na < 45:
        label = "low overall — normal alertness with plenty of rest time"
    elif na < 60:
        label = "moderate — sympathetic drive up; watch sleep and easy days"
    else:
        label = "elevated — sustained sympathetic drive, factor this into training decisions"
    line = f"Non-activity stress {na}/100 · {label}."
    if avg7 is not None and abs(na - avg7) >= 5:
        line += f" 7-day baseline {avg7}."
    return line


def _hrv_read(last, lo, hi, state, pct, sex, band):
    peer = f"Among {'women' if sex == 'F' else 'men'} aged {band}, that's roughly the {pct}th percentile — but HRV varies enormously between people, so your own balanced range ({lo}–{hi} ms) is what matters, not the peer number."
    if state == "below":
        return f"Last night dipped below your balanced range ({lo}–{hi} ms) — an early fatigue flag. If it stays low for several days, pull back. {peer}"
    if state == "above":
        return f"Above your usual band ({lo}–{hi} ms) — deeply parasympathetic and well recovered; a good day to absorb hard work. {peer}"
    return f"Sitting inside your balanced range ({lo}–{hi} ms) — normal, recovered. Watch the 7-day trend, not any single night. {peer}"


def _rhr_read(last, rested, base, state, pct, sex, band):
    peer = f"That's about the {pct}th percentile for {'women' if sex == 'F' else 'men'} aged {band} — a genuinely athletic resting heart rate."
    if state == "excellent":
        return f"{last} bpm is right at your rested floor (~{rested}) — a strong recovery signal; the body has fully stood down. {peer}"
    if state == "elevated":
        return f"{last} bpm is elevated for you (baseline ~{base}). An overnight RHR this high usually means incomplete recovery — hard training, short sleep, alcohol, heat or an oncoming illness. {peer}"
    return f"{last} bpm is around your normal baseline (~{base}). Nothing to flag. {peer}"


def _overreaching(hrv, rhr, lr):
    hstate = (hrv or {}).get("state")
    rstate = (rhr or {}).get("state")
    if hstate == "below" and rstate == "elevated":
        return {"tone": "red", "title": "Overreaching signature — HRV down and resting HR up together",
                "detail": "This is the classic combined signal that fatigue is outrunning recovery. Take today easy or off, prioritize sleep and fuel, and re-check tomorrow before any hard work. One or two soft days now prevents a forced week off later."}
    if (lr.get("level") or "").upper() == "LOW" or hstate == "below" or rstate == "elevated":
        return {"tone": "amber", "title": "One marker is off — keep an eye on the trend",
                "detail": "A single soft signal isn't alarming — it's often just a poor night or a hard session catching up. Favour an easy day, sleep well, and watch whether it recovers within 24–48 h. Only a multi-day drift warrants cutting load."}
    return {"tone": "green", "title": "Recovery markers agree — you're in a good place",
            "detail": "HRV, resting HR and readiness are aligned, so your body can take on the training you've got planned. Keep protecting sleep and easy-day discipline to hold it here."}


def _tiles(out):
    r = out.get("readiness") or {}
    h = out.get("hrv") or {}
    rh = out.get("rhr") or {}
    bb = out.get("body_battery") or {}
    return {
        "readiness": r.get("score"), "readiness_color": r.get("color"),
        "hrv_avg7": h.get("avg7"), "hrv_status": h.get("status"), "hrv_state": h.get("state"),
        "rhr": rh.get("last"), "rhr_state": rh.get("state"),
        "bb_recharge": bb.get("charged"),
        "acwr_pct": r.get("acwr_pct"),
    }


_GLOSSARY = [
    {"term": "Training Readiness", "text": "Garmin's overnight blend of sleep, HRV, recovery time, stress and recent load into a 0–100 'can I go hard today?' score. Treat it as a strong hint, then confirm with how the warm-up feels."},
    {"term": "HRV (overnight)", "text": "Beat-to-beat variation in your heart rate while you sleep — a window on your nervous system. Higher and stable = well recovered; a drop below your personal range flags fatigue or illness. It's deeply individual, so compare to YOUR baseline, not other people."},
    {"term": "Balanced range", "text": "Garmin's personalized HRV band, learned from ~3 weeks of your nights. Inside it = normal for you; below = a recovery flag. This adapts as your fitness changes."},
    {"term": "Resting HR", "text": "Your lowest overnight heart rate. It falls as fitness improves and rises with fatigue, illness, alcohol, heat or dehydration — one of the simplest, most reliable recovery signals."},
    {"term": "Body Battery", "text": "A 0–100 energy gauge that charges with rest/sleep and drains with stress and activity. A big overnight recharge and a high morning peak mean you slept well and stress was low."},
    {"term": "ACWR", "text": "Acute-to-chronic workload ratio — recent load vs what you're adapted to. 0.8–1.3 is the sweet spot; spiking above ~1.5 sharply raises injury risk."},
]


_PITFALLS = [
    "Don't chase a single night's HRV — act on the 7-day trend, not daily noise.",
    "A green readiness score is permission, not an order: still start hard days with a proper warm-up and bail if the legs are flat.",
    "Rising resting HR + falling HRV together is the real warning — either one alone is usually just a rough night.",
    "Don't compare your HRV number to other people's — the between-person spread is huge; your own baseline is the only fair yardstick.",
    "Recovery tech can't fix under-fuelling or under-sleeping — the metrics are a mirror, not a substitute for the basics.",
]
