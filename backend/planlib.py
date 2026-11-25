"""Loads config.json + plan.json and derives race countdown, today's session,
resolved bail-out options, and Garmin-validated pace guidance.
"""
from __future__ import annotations
import json
import os
import re
import datetime as dt
from typing import Any, Optional


def _pace_one(s: str) -> int:
    s = s.strip()
    m, sec = s.split(":")
    return int(m) * 60 + int(sec)


def _pace_range(s: str):
    """'4:03' -> (243,243,243) ; '4:10–4:03' -> (mid, slow, fast)."""
    s = s.replace("–", "-").replace("—", "-").strip().rstrip("-")
    if "-" in s:
        a, b = s.split("-", 1)
        pa, pb = _pace_one(a), _pace_one(b)
        return round((pa + pb) / 2), max(pa, pb), min(pa, pb)
    p = _pace_one(s)
    return p, p, p


def _rec_secs(num: str, unit) -> int:
    if ":" in num:
        m, sec = num.split(":")
        return int(m) * 60 + int(sec)
    n = float(num)
    return round(n * 60) if (unit and unit.startswith("m")) else round(n)


def _phase_zone(kind: str, pace, cfg: dict) -> int:
    if kind == "recovery":
        return 1
    if kind in ("warmup", "cooldown"):
        return 2
    if pace is None:
        return 2
    p = cfg.get("paces", {})
    if pace >= (p.get("easy", {}).get("sec_low", 295) - 5):
        return 2
    if pace <= (p.get("fivek", {}).get("sec", 225) + 4):
        return 5
    if pace <= (p.get("tenk", {}).get("sec", 237) + 4):
        return 4
    if pace <= (p.get("threshold", {}).get("sec", 243) + 4):
        return 4
    return 3   # HMP / steady


def build_phases(day: dict, cfg: dict) -> list:
    """Expand a prescribed workout into ordered phases (warmup / intervals /
    recoveries / cooldown) with target paces + estimated durations, for the
    'prescribed workout' blueprint chart."""
    dtype = day.get("type")
    if dtype in ("rest", "strength") or not dtype:
        return []
    paces = cfg.get("paces", {})
    easy = paces.get("easy", {})
    ez = round((easy.get("sec_low", 295) + easy.get("sec_high", 325)) / 2)
    phases = []

    def add(label, kind, pace, secs, km=None, lo=None, hi=None):
        phases.append({"label": label, "kind": kind, "pace_s": pace,
                       "secs": max(1, round(secs)), "km": km,
                       "pace_lo": lo or pace, "pace_hi": hi or pace,
                       "zone": _phase_zone(kind, pace, cfg)})

    _PK = {"hmp": "hmp", "mp": "mp", "steady": "steady", "threshold": "threshold", "easy": "easy"}
    _LAB = {"hmp": "HMP", "mp": "MP", "steady": "steady", "threshold": "threshold", "easy": "easy"}
    segs = day.get("segments")
    if segs:
        for seg in segs:
            eff = seg.get("effort", "easy")
            pk = _PK.get(eff, "easy")
            p = paces.get(pk, {})
            pace = p.get("sec") or round((p.get("sec_low", 295) + p.get("sec_high", 325)) / 2)
            km = seg.get("km", 0)
            add(f"{km} km {_LAB.get(eff, eff)}", eff if eff != "easy" else "steady", pace, km * pace, km)
        return phases

    reps = day.get("reps")
    if not reps:
        pk = day.get("pace_key") or "easy"
        p = paces.get(pk, easy)
        pace = p.get("sec") or round((p.get("sec_low", 295) + p.get("sec_high", 325)) / 2)
        dist = day.get("distance_km") or 0
        if dist:
            add(f"{dist} km", "steady", pace, dist * pace, dist)
        # strides tacked onto an easy run (e.g. "4–6×20s strides") — show them
        strides = day.get("strides")
        if strides:
            m = re.search(r"(\d+)(?:\s*[–—-]\s*(\d+))?\s*[×x]\s*(\d+)\s*s", strides)
            if m:
                n_str = int(m.group(2) or m.group(1))
                dur = int(m.group(3))
                rep_pace = paces.get("rep", {}).get("sec", 210)
                for i in range(n_str):
                    add(f"{dur}s stride", "interval", rep_pace, dur)
                    if i < n_str - 1:
                        add("jog", "recovery", pace, 45)
        return phases

    for tok in [t.strip() for t in reps.split("·")]:
        if not tok:
            continue
        tl = tok.lower()
        m = re.search(r"([\d.]+)\s*km\s*warm", tl)
        if m:
            km = float(m.group(1)); add("Warm-up", "warmup", ez, km * ez, km); continue
        m = re.search(r"([\d.]+)\s*km\s*cool", tl)
        if m:
            km = float(m.group(1)); add("Cool-down", "cooldown", ez, km * ez, km); continue
        # alternations, e.g. "5×(800m @4:03/km / 800m @4:09/km) continuous" —
        # two efforts back to back with no standing recovery between them.
        m = re.search(r"(\d+)\s*[×x]\s*\(\s*(\d+)\s*m\s*@\s*([0-9:.]+).*?/\s*(\d+)\s*m\s*@\s*([0-9:.]+)", tl)
        if m:
            n = int(m.group(1))
            a_m, b_m = int(m.group(2)), int(m.group(4))
            a_p, a_lo, a_hi = _pace_range(m.group(3))
            b_p, b_lo, b_hi = _pace_range(m.group(5))
            for _ in range(n):
                add(f"{a_m} m", "interval", a_p, a_m / 1000 * a_p, a_m / 1000, a_lo, a_hi)
                add(f"{b_m} m", "mp", b_p, b_m / 1000 * b_p, b_m / 1000, b_lo, b_hi)
            continue
        # set-based blocks, e.g. "2×(1 km @4:03/km + 2 km @4:09/km) w/ 2 min jog"
        m = re.search(r"(\d+)\s*[×x]\s*\(\s*([\d.]+)\s*km\s*@\s*([0-9:.]+)[^+]*?\+\s*([\d.]+)\s*km\s*@\s*([0-9:.]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n = int(m.group(1))
            a_k, b_k = float(m.group(2)), float(m.group(4))
            a_p, a_lo, a_hi = _pace_range(m.group(3))
            b_p, b_lo, b_hi = _pace_range(m.group(5))
            r = _rec_secs(m.group(6), m.group(7) or "min")
            for i in range(n):
                add(f"{a_k} km", "interval", a_p, a_k * a_p, a_k, a_lo, a_hi)
                add(f"{b_k} km", "mp", b_p, b_k * b_p, b_k, b_lo, b_hi)
                if i < n - 1:
                    add("jog", "recovery", ez, r)
            continue
        m = re.search(r"(\d+)\s*[×x]\s*(\d+)\s*m\s*@\s*([0-9:.–—-]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n = int(m.group(1)); dm = int(m.group(2)); mid, lo, hi = _pace_range(m.group(3)); r = _rec_secs(m.group(4), m.group(5))
            for i in range(n):
                add(f"{dm} m", "interval", mid, dm / 1000 * mid, dm / 1000, lo, hi)
                if i < n - 1:
                    add("jog", "recovery", ez, r)
            continue
        m = re.search(r"(\d+)\s*[×x]\s*([\d.]+)\s*km\s*@\s*([0-9:.–—-]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n = int(m.group(1)); dk = float(m.group(2)); mid, lo, hi = _pace_range(m.group(3)); r = _rec_secs(m.group(4), m.group(5))
            for i in range(n):
                add(f"{dk} km", "interval", mid, dk * mid, dk, lo, hi)
                if i < n - 1:
                    add("jog", "recovery", ez, r)
            continue
        m = re.search(r"(\d+)\s*[×x]\s*(\d+)\s*min\s*@\s*([0-9:.–—-]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n = int(m.group(1)); mins = int(m.group(2)); mid, lo, hi = _pace_range(m.group(3)); r = _rec_secs(m.group(4), m.group(5) or "min")
            for i in range(n):
                add(f"{mins} min", "interval", mid, mins * 60, None, lo, hi)
                if i < n - 1:
                    add("jog", "recovery", ez, r)
            continue
        m = re.search(r"(\d+)\s*min\s*fartlek", tl)
        if m:
            mins = int(m.group(1)); pace = paces.get("tenk", {}).get("sec", 237)
            add(f"{mins} min fartlek", "interval", pace, mins * 60); continue
        m = re.search(r"(\d+)\s*min\s*(?:continuous\s*)?@\s*([0-9:.–—-]+)", tl)
        if m:
            mins = int(m.group(1)); mid, lo, hi = _pace_range(m.group(2))
            add(f"{mins} min tempo", "interval", mid, mins * 60, None, lo, hi); continue
        m = re.search(r"([\d.]+)\s*km\s*all-out", tl)
        if m:
            km = float(m.group(1)); pace = paces.get("tenk", {}).get("sec", 237)
            add(f"{km} km race", "race", pace, km * pace, km); continue
    return phases

from paths import CONFIG_PATH, PLAN_PATH


def is_configured() -> bool:
    """True once onboarding has written a config + plan."""
    return os.path.exists(CONFIG_PATH) and os.path.exists(PLAN_PATH)


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config() -> dict:
    return load_json(CONFIG_PATH)


def load_plan() -> dict:
    return load_json(PLAN_PATH)


def active_race(cfg: dict, today: Optional[dt.date] = None) -> dict:
    """The race we're currently training for: the next race whose date is today
    or later. Falls back to the last race once everything is in the past, and to
    the legacy single-race config if no `races` array is present."""
    today = today or dt.date.today()
    races = cfg.get("races")
    if not races:
        r = dict(cfg.get("race", {}))
        r["goals"] = cfg.get("goals", {})
        r["primary_goal"] = cfg.get("primary_goal", "A")
        return r
    upcoming = [r for r in races if dt.date.fromisoformat(r["date"]) >= today]
    return min(upcoming, key=lambda r: r["date"]) if upcoming else max(races, key=lambda r: r["date"])


def resolve_active_race(cfg: dict, today: Optional[dt.date] = None) -> dict:
    """A shallow copy of cfg with race / goals / primary_goal pointed at the
    active race, so every downstream consumer retargets automatically as the
    calendar advances (half now → marathon after Oct 11)."""
    ar = active_race(cfg, today)
    out = dict(cfg)
    out["race"] = {k: ar[k] for k in ("name", "location", "date", "distance_km", "distance_label") if k in ar}
    out["goals"] = ar.get("goals", cfg.get("goals", {}))
    out["primary_goal"] = ar.get("primary_goal", cfg.get("primary_goal", "A"))
    out["active_race"] = ar
    return out


def weeks_to_race(today: Optional[dt.date] = None, race_date: Optional[str] = None) -> dict:
    today = today or dt.date.today()
    cfg = load_config()
    ar = active_race(cfg, today)
    rd = dt.date.fromisoformat(race_date or ar.get("date") or cfg["race"]["date"])
    days = (rd - today).days
    goal = (ar.get("goals", {}) or {}).get(ar.get("primary_goal", "A"), {})
    return {
        "race_date": rd.isoformat(),
        "race_name": ar.get("name"),
        "race_location": ar.get("location"),
        "race_distance_km": ar.get("distance_km"),
        "distance_label": ar.get("distance_label"),
        "goal_time": goal.get("time"),
        "goal_pace_per_km": goal.get("pace_per_km"),
        "today": today.isoformat(),
        "days_to_race": days,
        "weeks_to_race": round(days / 7.0, 1),
        "weeks_to_race_ceil": max(0, (days + 6) // 7),
        "is_race_week": 0 <= days <= 7,
        "race_passed": days < 0,
    }


def fmt_secs(s: Optional[float]) -> Optional[str]:
    if s is None:
        return None
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def pace_str(s_per_km: Optional[float]) -> Optional[str]:
    if not s_per_km:
        return None
    m, sec = divmod(int(round(s_per_km)), 60)
    return f"{m}:{sec:02d}"


def resolve_day(day: dict, plan: dict, cfg: dict) -> dict:
    """Attach resolved bail-outs and human pace strings to a plan day."""
    out = dict(day)
    templates = plan.get("bailout_templates", {})
    out["bailouts"] = templates.get(day.get("type"), {})
    pk = day.get("pace_key")
    if pk and pk in cfg.get("paces", {}):
        out["pace"] = cfg["paces"][pk]
    # strength day gets its own bail-out set too — a day may carry its own
    # (the core-led sessions do), which wins over the generic template.
    if day.get("strength"):
        out["strength_bailouts"] = day.get("strength_bailouts") or templates.get("strength", {})
    # Easy runs are now HR-anchored (avoid Z3 drift), not pace-anchored. Compute
    # the target range from the athlete's Z2 (config.hr_zones), with pace kept
    # as a reference in `pace`. Quality/long/race stay pace-based.
    if day.get("type") == "easy" and day.get("pace_key") == "easy":
        zones = ((cfg.get("hr_zones") or {}).get("zones") or [])
        z1 = next((z for z in zones if z.get("zone") == 1), {})
        z2 = next((z for z in zones if z.get("zone") == 2), {})
        if z1 and z2:
            lo = max(0, (z1.get("high") or 134) - 8)   # a touch into Z1 top
            hi = z2.get("high") or 153                  # HARD ceiling — no Z3
            out["easy_hr"] = {"low": lo, "high": hi, "zone": "Z2",
                               "note": f"Ceiling {hi} bpm — cross above and you're in Z3."}
    out["phases"] = build_phases(day, cfg)
    return out


def find_week_for_date(plan: dict, date_iso: str) -> Optional[dict]:
    for wk in plan["weeks"]:
        for d in wk["days"]:
            if d["date"] == date_iso:
                return wk
    return None


def get_day(plan: dict, date_iso: str) -> Optional[dict]:
    for wk in plan["weeks"]:
        for d in wk["days"]:
            if d["date"] == date_iso:
                return d
    return None


def enrich_plan(plan: dict, cfg: dict) -> dict:
    """Return the plan with each day resolved (bailouts + pace objects) and
    per-week planned-km / hmp-km totals computed."""
    out = {"meta": plan.get("meta", {}),
           "core_stability": plan.get("core_stability"),
           "weeks": []}
    for wk in plan["weeks"]:
        days = [resolve_day(d, plan, cfg) for d in wk["days"]]
        planned_km = round(sum(d.get("distance_km", 0) or 0 for d in wk["days"]), 1)
        hmp_km = sum(d.get("hmp_km", 0) or 0 for d in wk["days"])
        quality = sum(1 for d in wk["days"] if d.get("type") == "quality")
        w = dict(wk)
        w["days"] = days
        w["planned_km"] = planned_km
        w["hmp_km"] = hmp_km
        w["quality_sessions"] = quality
        out["weeks"].append(w)
    return out


# Garmin race-predictor key + display noun for each supported race distance.
PRED_KEYS = {"5k": "sec_5k", "10k": "sec_10k", "half": "sec_half", "marathon": "sec_marathon"}
RACE_NOUNS = {"5k": "the 5K", "10k": "the 10K", "half": "the half", "marathon": "the marathon"}


def distance_label_for(km: float) -> str:
    """Nearest supported race distance for a distance in km."""
    km = float(km or 0)
    if km >= 32:
        return "marathon"
    if km >= 15:
        return "half"
    if km >= 7.5:
        return "10k"
    return "5k"


def race_label(cfg: dict) -> str:
    race = (cfg or {}).get("race") or {}
    return race.get("distance_label") or distance_label_for(race.get("distance_km") or 21.1)


def derived_paces(cfg: dict, snapshot: Optional[dict]) -> dict:
    """Compare the locked plan paces against Garmin's current fitness (race
    prediction). Returns the plan paces plus a Garmin-equivalent HMP and a
    flag on whether the goal looks realistic yet."""
    paces = cfg["paces"]
    result = {"plan": paces, "garmin": None, "assessment": None}
    if not snapshot:
        return result
    rp = snapshot.get("race_predictions")
    # Use the prediction that matches the active race distance.
    pred_key = PRED_KEYS[race_label(cfg)]
    if rp and rp.get(pred_key):
        race_km = cfg["race"]["distance_km"]
        garmin_pace_s = rp[pred_key] / race_km
        goal = cfg["goals"][cfg.get("primary_goal", "A")]
        goal_pace_s = goal["seconds"] / race_km
        gap_s = garmin_pace_s - goal_pace_s  # positive => currently slower than goal
        result["garmin"] = {
            "predicted_half": fmt_secs(rp[pred_key]),
            "predicted_half_pace": pace_str(garmin_pace_s),
            "goal_half": goal["time"],
            "goal_half_pace": goal["pace_per_km"],
            "gap_per_km_s": round(gap_s, 1),
            "gap_per_km": ("+" if gap_s >= 0 else "-") + pace_str(abs(gap_s)),
        }
        if gap_s <= 3:
            verdict = "on_track"
        elif gap_s <= 15:
            verdict = "slightly_ambitious"
        else:
            verdict = "ambitious"
        result["assessment"] = verdict
    return result
