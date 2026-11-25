# -*- coding: utf-8 -*-
"""Generate config.json + plan.json from an onboarding profile.

The profile is what the setup screen collects: the goal race (5K, 10K, half or
marathon), a goal time, current and peak weekly volume, how many days a week
you run, and optional heart-rate / weight details. From that we derive:

  * training paces from the goal time's VDOT (Daniels' running formula)
  * heart-rate zones anchored to lactate-threshold HR
  * a periodised plan: base -> build -> race-specific -> taper, scaled to the
    weeks available, with a down week every fourth week

Workout text uses the same token grammar planlib.build_phases parses
("2 km warm-up · 5×1000m @3:45/km w/ 2:30 jog · 2 km cool-down"), so the
workout blueprint charts and Garmin workout sync work on generated plans.
"""
from __future__ import annotations
import datetime as dt
import json
import math
import os
from typing import Optional

import paths
from planlib import fmt_secs, pace_str

DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DOW_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

DISTANCES = {
    "5k": {"km": 5.0, "m": 5000.0, "name": "5K", "taper": 1, "long_cap": 18, "typical_weeks": 10},
    "10k": {"km": 10.0, "m": 10000.0, "name": "10K", "taper": 1, "long_cap": 21, "typical_weeks": 12},
    "half": {"km": 21.1, "m": 21097.5, "name": "half marathon", "taper": 2, "long_cap": 24, "typical_weeks": 14},
    "marathon": {"km": 42.195, "m": 42195.0, "name": "marathon", "taper": 3, "long_cap": 34, "typical_weeks": 18},
}


# ---------------- VDOT ----------------
def vdot_from(distance_m: float, seconds: float) -> float:
    t = seconds / 60.0
    v = distance_m / t
    vo2 = -4.60 + 0.182258 * v + 0.000104 * v * v
    pct = 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)
    return vo2 / pct


def race_time(distance_m: float, vdot: float) -> float:
    lo, hi = 120.0, 8 * 3600.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if vdot_from(distance_m, mid) > vdot:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _pace_at_fraction(vdot: float, frac: float) -> float:
    """Seconds/km at a fraction of VDOT (inverting the VO2-velocity curve)."""
    target = frac * vdot
    a, b, c = 0.000104, 0.182258, -4.60 - target
    v = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)     # m/min
    return 60000.0 / v


def derive_paces(vdot: float) -> dict:
    """Training paces in config.json's shape (strings + seconds)."""
    def race_pace(m):
        return race_time(m, vdot) / (m / 1000.0)

    easy_fast = _pace_at_fraction(vdot, 0.70)
    easy_slow = _pace_at_fraction(vdot, 0.62)
    mp = race_pace(42195)
    hmp = race_pace(21097.5)
    thr = _pace_at_fraction(vdot, 0.88)
    tenk = race_pace(10000)
    fivek = race_pace(5000)
    rep = race_pace(1500)
    steady = (mp + easy_fast) / 2

    def one(key, label, sec, zone):
        sec = round(sec)
        return {"key": key, "label": label, "target": pace_str(sec), "sec": sec, "zone": zone}

    return {
        "easy": {"key": "easy", "label": "Easy / Zone 2 recovery",
                 "low": pace_str(round(easy_fast)), "high": pace_str(round(easy_slow)),
                 "sec_low": round(easy_fast), "sec_high": round(easy_slow), "zone": "Z2"},
        "steady": one("steady", "Steady / aerobic-threshold effort", steady, "Z3"),
        "mp": one("mp", "Marathon race pace (MP)", mp, "Z3-4"),
        "hmp": one("hmp", "Half-marathon race pace (HMP)", hmp, "Z4"),
        "threshold": one("threshold", "Threshold / tempo", thr, "Z4"),
        "tenk": one("tenk", "10K pace", tenk, "Z4"),
        "fivek": one("fivek", "5K / interval (VO2) pace", fivek, "Z5"),
        "rep": one("rep", "Rep / strides", rep, "Z5"),
    }


def derive_hr_zones(max_hr: int, lthr: Optional[int]) -> dict:
    lthr = int(lthr or round(max_hr * 0.90))
    b = [round(lthr * f) for f in (0.79, 0.90, 0.96, 1.015)]
    zones = [
        {"zone": 1, "name": "Recovery", "low": 0, "high": b[0] - 1},
        {"zone": 2, "name": "Easy", "low": b[0], "high": b[1]},
        {"zone": 3, "name": "Tempo", "low": b[1] + 1, "high": b[2]},
        {"zone": 4, "name": "Threshold", "low": b[2] + 1, "high": b[3]},
        {"zone": 5, "name": "VO2", "low": b[3] + 1, "high": 999},
    ]
    return {"method": "lthr_anchored", "lthr": lthr, "max_hr": int(max_hr),
            "note": "Zones anchored to lactate-threshold HR (Z4 brackets LTHR). Edit them in data/config.json "
                    "if your watch's zones fit you better.",
            "zones": zones}


# ---------------- config ----------------
def _goal(label, secs, km, note):
    return {"label": label, "time": fmt_secs(secs), "seconds": int(secs),
            "pace_per_km": pace_str(secs / km), "note": note}


def build_config(profile: dict) -> dict:
    p = profile
    dist_key = p["distance"]
    d = DISTANCES[dist_key]
    km = float(p.get("distance_km") or d["km"])
    a = int(p["goal_seconds"])
    vdot = vdot_from(km * 1000.0, a)
    goals = {
        "A": _goal("A — the goal", a, km, "Your target. Everything in the plan is paced off this."),
        "B": _goal("B — realistic", round(a * 1.025), km, "A strong day if training mostly goes to plan."),
        "C": _goal("C — rough day", round(a * 1.05), km, "Heat, wind or heavy legs — still a solid result."),
    }
    race = {"name": p.get("race_name") or f"Goal {d['name']}", "location": p.get("race_location") or "",
            "date": p["race_date"], "distance_km": km, "distance_label": dist_key}
    birth = p.get("birth_date")
    age = None
    if birth:
        try:
            bd = dt.date.fromisoformat(birth)
            age = (dt.date.today() - bd).days // 365
        except ValueError:
            birth = None
    max_hr = int(p.get("max_hr") or (round(208 - 0.7 * age) if age else 185))
    cfg = {
        "athlete": {k: v for k, v in {
            "name": p.get("name") or "Runner",
            "level": p.get("level") or "intermediate",
            "sex": p.get("sex") or None,
            "birth_date": birth,
            "goal_weight_lb": p.get("goal_weight_lb"),
            "current_weight_lb_fallback": p.get("weight_lb"),
        }.items() if v not in (None, "")},
        "race": race,
        "goals": goals,
        "primary_goal": "A",
        "races": [dict(race, primary_goal="A", goals=goals)],
        "weekly_target_km": int(p.get("peak_km") or 0),
        "strength_day": p.get("strength_day") or None,
        "strength_day_fallback": None,
        "paces": derive_paces(vdot),
        "pace_map": {"@HMP": "hmp", "@MP": "mp", "@marathon": "mp", "@10K": "tenk", "@5K": "fivek",
                     "@goal": {"5k": "fivek", "10k": "tenk", "half": "hmp", "marathon": "mp"}[dist_key],
                     "@goal pace": {"5k": "fivek", "10k": "tenk", "half": "hmp", "marathon": "mp"}[dist_key]},
        "hr_zones": derive_hr_zones(max_hr, p.get("lthr")),
        "profile": p,
    }
    if p.get("home_lat") not in (None, "") and p.get("home_lon") not in (None, ""):
        cfg["home"] = {"lat": float(p["home_lat"]), "lon": float(p["home_lon"])}
    return cfg


# ---------------- plan ----------------
def _next_monday(d: dt.date) -> dt.date:
    return d + dt.timedelta(days=(7 - d.weekday()) % 7)


def _phase_plan(n_weeks: int, taper: int) -> list:
    """Phase name per week index: Base / Build / Race-specific / Taper / Race week."""
    pre = max(1, n_weeks - taper)
    base = max(1, round(pre * 0.40))
    build = max(1, round(pre * 0.33)) if pre >= 3 else 0
    spec = max(0, pre - base - build)
    out = ["Base"] * base + ["Build"] * build + ["Race-specific"] * spec
    out += ["Taper"] * (taper - 1) + ["Race week"]
    return out[-n_weeks:] if len(out) > n_weeks else out


def _volumes(phases: list, start_km: float, peak_km: float, dist_key: str) -> list:
    """Weekly km: ramp ~8%/wk to peak by the race-specific block, a down week
    every 4th week, then taper."""
    taper_curve = {"marathon": [0.75, 0.55], "half": [0.70], "10k": [], "5k": []}[dist_key]
    race_week_frac = {"marathon": 0.40, "half": 0.50, "10k": 0.60, "5k": 0.65}[dist_key]
    n_pre = sum(1 for p in phases if p in ("Base", "Build", "Race-specific"))
    vols, cur = [], start_km
    ramp_weeks = max(1, n_pre - 2)
    step = (peak_km / start_km) ** (1 / ramp_weeks) if start_km > 0 and peak_km > start_km else 1.0
    step = min(step, 1.10)
    ti = 0
    for i, ph in enumerate(phases):
        if ph in ("Base", "Build", "Race-specific"):
            if i > 0:
                cur = min(peak_km, cur * step)
            v = cur
            if (i + 1) % 4 == 0 and i < n_pre - 1:      # down week (not the last pre-taper week)
                v = cur * 0.80
            vols.append(round(v))
        elif ph == "Taper":
            f = taper_curve[ti] if ti < len(taper_curve) else 0.7
            ti += 1
            vols.append(round(peak_km * f))
        else:
            vols.append(round(peak_km * race_week_frac))
    return vols


def _p(cfg, key):
    pc = cfg["paces"][key]
    return pc.get("target") or pc.get("low")


def _quality(cfg, dist_key, phase, wk_in_phase, second=False) -> dict:
    """One quality session: title, reps string, pace_key, work km (approx)."""
    T, I, R = _p(cfg, "threshold"), _p(cfg, "fivek"), _p(cfg, "rep")
    MP, HMP, TENK = _p(cfg, "mp"), _p(cfg, "hmp"), _p(cfg, "tenk")
    w = wk_in_phase
    if phase == "Base":
        if second:
            return {"title": "Hill strides", "reps": f"2 km warm-up · 8×60s @{R}/km w/ 90s jog · 2 km cool-down",
                    "pace_key": "rep", "work_km": 2.0,
                    "why": "Short hill or flat strides for economy and leg speed without fatigue."}
        mins = 20 + 5 * min(w, 3)
        return {"title": f"Fartlek {mins} min", "reps": f"2 km warm-up · {mins} min fartlek · 2 km cool-down",
                "pace_key": "tenk", "work_km": mins / 4.5,
                "why": "Unstructured surges (1–3 min hard, equal easy) — reintroduces speed while the base builds."}
    if phase == "Build":
        if second:
            n = 4 + min(w, 2)
            return {"title": f"VO2: {n}×1000m", "reps": f"2 km warm-up · {n}×1000m @{I}/km w/ 2:30 jog · 2 km cool-down",
                    "pace_key": "fivek", "work_km": n,
                    "why": "VO2max intervals raise the ceiling the threshold work then pushes toward."}
        sets = [(3, 8), (4, 8), (3, 10), (4, 10), (2, 15), (3, 12)][min(w, 5)]
        return {"title": f"Threshold {sets[0]}×{sets[1]} min",
                "reps": f"2 km warm-up · {sets[0]}×{sets[1]} min @{T}/km w/ 2 min jog · 2 km cool-down",
                "pace_key": "threshold", "work_km": sets[0] * sets[1] / 3.8,
                "why": "Cruise intervals at threshold — the single best session for raising the pace you can hold."}
    if phase in ("Race-specific", "Taper"):
        taper = phase == "Taper"
        if dist_key == "marathon":
            if second:
                k = 2 if taper else 3
                return {"title": f"Threshold {k}×2 km", "reps": f"2 km warm-up · {k}×2 km @{T}/km w/ 2 min jog · 2 km cool-down",
                        "pace_key": "threshold", "work_km": 2 * k,
                        "why": "Threshold keeps the engine sharp while the long runs carry the marathon work."}
            k = 2 if taper else min(4, 2 + w // 2)
            return {"title": f"MP {k}×4 km", "reps": f"2 km warm-up · {k}×4 km @{MP}/km w/ 3 min jog · 2 km cool-down",
                    "pace_key": "mp", "work_km": 4 * k,
                    "why": "Marathon pace in long blocks — rehearse the rhythm and fuelling of race day."}
        if dist_key == "half":
            if second:
                n = 4 if taper else 5
                return {"title": f"VO2: {n}×1000m", "reps": f"2 km warm-up · {n}×1000m @{TENK}/km w/ 2 min jog · 2 km cool-down",
                        "pace_key": "tenk", "work_km": n,
                        "why": "10K-pace reps make half pace feel controlled."}
            k = 2 if taper else min(4, 3 + w // 3)
            return {"title": f"HMP {k}×3 km", "reps": f"2 km warm-up · {k}×3 km @{HMP}/km w/ 2 min jog · 2 km cool-down",
                    "pace_key": "hmp", "work_km": 3 * k,
                    "why": "Goal-pace blocks — the most race-specific session for the half."}
        if dist_key == "10k":
            if second:
                return {"title": "Threshold 20 min", "reps": f"2 km warm-up · 20 min continuous @{T}/km · 2 km cool-down",
                        "pace_key": "threshold", "work_km": 5.3,
                        "why": "Steady threshold supports the 10K's sustained aerobic demand."}
            n = 4 if taper else min(6, 4 + w // 2)
            return {"title": f"10K pace {n}×1 km", "reps": f"2 km warm-up · {n}×1 km @{TENK}/km w/ 90s jog · 2 km cool-down",
                    "pace_key": "tenk", "work_km": n,
                    "why": "Goal-pace kilometres with short recoveries — race rhythm under fatigue."}
        # 5k
        if second:
            n = 6 if taper else 10
            return {"title": f"Reps {n}×400m", "reps": f"2 km warm-up · {n}×400m @{R}/km w/ 60s jog · 2 km cool-down",
                    "pace_key": "rep", "work_km": n * 0.4,
                    "why": "Fast 400s for speed and economy — the top-end gear a 5K needs."}
        n = 4 if taper else min(6, 4 + w // 2)
        return {"title": f"5K pace {n}×800m", "reps": f"2 km warm-up · {n}×800m @{I}/km w/ 2 min jog · 2 km cool-down",
                "pace_key": "fivek", "work_km": n * 0.8,
                "why": "Goal-pace 800s — the classic 5K sharpening session."}
    return {"title": "Easy + strides", "reps": None, "pace_key": "easy", "work_km": 0, "why": "Keep the legs ticking over."}


def _long_run(cfg, dist_key, phase, wk_in_phase, km) -> dict:
    out = {"type": "long", "title": f"Long run {km} km", "distance_km": km, "pace_key": "easy",
           "why": "Easy long run — the aerobic and durability backbone of the plan."}
    if phase == "Race-specific" and km >= 12:
        eff = {"marathon": "mp", "half": "hmp", "10k": "steady", "5k": "steady"}[dist_key]
        lab = {"mp": "@MP", "hmp": "@HMP", "steady": "steady"}[eff]
        frac = {"marathon": 0.25 + 0.05 * min(wk_in_phase, 3), "half": 0.25, "10k": 0.2, "5k": 0.2}[dist_key]
        fast = max(3, round(km * frac))
        out.update({"title": f"Long run {km} km, last {fast} km {lab}",
                    "segments": [{"effort": "easy", "km": km - fast}, {"effort": eff, "km": fast}],
                    "why": "Finish fast on tired legs — rehearses the back half of the race."})
        if eff in ("mp", "hmp"):
            out["hmp_km"] = fast
    elif phase == "Build" and km >= 14 and wk_in_phase % 2 == 1:
        fast = max(3, round(km * 0.2))
        out.update({"title": f"Long run {km} km, last {fast} km steady",
                    "segments": [{"effort": "easy", "km": km - fast}, {"effort": "steady", "km": fast}],
                    "why": "A steady finish builds durability without the cost of race pace."})
    return out


_NIGHTLY = [
    ("Hips + glutes - 10 min", ["Glute bridge 2x12", "Side plank 2x30s/side"], ["90/90 hip switches 6/side", "Couch stretch 60s/side"]),
    ("Calves + ankles - 8 min", ["Bird dog 2x8/side"], ["Calf stretch 60s/side", "Ankle circles 10/side", "Foam-roll calves 60s/side"]),
    ("Core + thoracic - 10 min", ["Dead bug 3x8/side", "Plank 2x40s"], ["Threaded needle 8/side", "Open book 8/side"]),
    ("Hamstrings + lower back - 10 min", ["Bird dog 2x8/side"], ["Hamstring floss 10/side", "Child's pose 60s", "Supine twist 60s/side"]),
    ("Quads + IT band - 8 min", ["Glute bridge 2x12"], ["Foam-roll quads 60s/side", "Foam-roll IT band 60s/side", "Couch stretch 60s/side"]),
    ("Post-long-run flush - 12 min", ["Glute bridge 2x10"], ["Legs-up-the-wall 3 min", "Figure-4 stretch 90s/side", "Foam-roll hamstrings + calves"]),
    ("Full-body reset - 12 min", ["Side plank 2x30s/side", "Dead bug 2x8/side"], ["Adductor rock-back 8/side", "Cobra 3x5 breaths", "Box breathing 4-4-4-4 x8"]),
]


def _nightly(i):
    t, core, mob = _NIGHTLY[i % 7]
    return {"title": t, "core": core, "mobility": mob, "duration_min": int(t.split("-")[-1].split()[0]),
            "note": "Optional, most nights. Ten minutes of consistent mobility beats an hour once a week."}


def _strength(phase) -> Optional[dict]:
    if phase in ("Race week",):
        return None
    if phase == "Base":
        return {"phase": "General strength", "focus": "Build tissue resilience",
                "exercises": ["Goblet squat 3×10", "Split squat 3×8/leg", "Romanian deadlift 3×8",
                              "Single-leg calf raise 3×12/leg", "Dead bug 3×8/side", "Side plank 3×30s/side"],
                "note": "Moderate loads, controlled reps. Lift after an easy run, not before a quality day."}
    if phase in ("Build", "Race-specific"):
        return {"phase": "Max strength + power", "focus": "Heavy, low-rep lower body",
                "exercises": ["Trap-bar or back squat 3×5 (heavy)", "Bulgarian split squat 3×6/leg",
                              "Single-leg calf raise 3×10/leg", "Box jumps 3×5", "Pallof press 3×10/side"],
                "note": "Heavy and short — strength without the soreness. Keep 48 h between this and the long run."}
    return {"phase": "Maintenance", "focus": "Keep strength, shed fatigue",
            "exercises": ["Goblet squat 2×6", "Single-leg calf raise 2×10/leg", "Dead bug 2×8/side"],
            "note": "Light touch during the taper — just enough to hold what you built."}


_STRENGTH_BAILOUTS = {
    "time": "Short on time → core moves only (15 min).",
    "legs": "Legs heavy → upper body + core, skip the lower-body lifts.",
    "readiness": "Red readiness → mobility only, no loaded lifts.",
}


def _easy_cap(cfg, recovery=False):
    z = cfg["hr_zones"]["zones"]
    z2_hi = z[1]["high"]
    e = cfg["paces"]["easy"]
    if recovery:
        return (f"Recovery day — deliberately short and slow. Keep it slower than {e['high']}/km and HR under "
                f"{z2_hi - 8}. If it feels like training, you're going too hard.")
    return (f"Ceiling: HR ≤ {z2_hi} (top of Z2) and conversational effort — roughly {e['low']}–{e['high']}/km. "
            f"If HR climbs past {z2_hi}, slow down or walk the hill. Easy days run too hard blunt the quality days.")


def build_plan(profile: dict, cfg: dict, today: Optional[dt.date] = None) -> dict:
    p = profile
    today = today or dt.date.today()
    dist_key = p["distance"]
    D = DISTANCES[dist_key]
    race_date = dt.date.fromisoformat(p["race_date"])
    start = dt.date.fromisoformat(p["start_date"]) if p.get("start_date") else _next_monday(today)
    start = start - dt.timedelta(days=start.weekday())                 # Monday-anchored
    race_monday = race_date - dt.timedelta(days=race_date.weekday())
    n_weeks = (race_monday - start).days // 7 + 1
    if n_weeks < 2:
        raise ValueError("The race is too soon — pick a race at least two weeks out.")
    n_weeks = min(n_weeks, 40)
    start = race_monday - dt.timedelta(weeks=n_weeks - 1)

    runs = max(3, min(7, int(p.get("runs_per_week") or 5)))
    cur_km = float(p.get("current_km") or 30)
    peak_km = max(cur_km, float(p.get("peak_km") or cur_km * 1.3))
    long_dow = DOW.index(p.get("long_run_day") or "Sun")
    q1_dow = DOW.index(p.get("quality_day") or "Tue")
    two_q = bool(p.get("second_quality", runs >= 5))
    strength_dow = DOW_FULL.index(p["strength_day"]) if p.get("strength_day") in DOW_FULL else None

    phases = _phase_plan(n_weeks, D["taper"])
    vols = _volumes(phases, cur_km, peak_km, dist_key)

    # running days: long + quality fixed, then spread easy days away from them
    q2_dow = (q1_dow + 2) % 7
    if q2_dow == long_dow or (q2_dow + 1) % 7 == long_dow:
        q2_dow = (q1_dow + 3) % 7 if (q1_dow + 3) % 7 not in (long_dow, (long_dow - 1) % 7) else (q1_dow - 2) % 7
    order = [long_dow, q1_dow, q2_dow, (long_dow - 1) % 7, (q1_dow - 1) % 7, (q1_dow + 1) % 7, (long_dow + 1) % 7]
    run_days = []
    for d in order + list(range(7)):
        if d not in run_days:
            run_days.append(d)
    run_days = set(run_days[:runs])

    weeks, phase_count = [], {}
    for wi in range(n_weeks):
        ph = phases[wi]
        wk_in_phase = phase_count.get(ph, 0)
        phase_count[ph] = wk_in_phase + 1
        wstart = start + dt.timedelta(weeks=wi)
        target = vols[wi]
        is_down = ph in ("Base", "Build", "Race-specific") and wi > 0 and target < vols[wi - 1] * 0.9
        days = [None] * 7
        remaining = float(target)

        # race week
        race_dow = race_date.weekday() if ph == "Race week" else None
        if race_dow is not None:
            days[race_dow] = {"type": "race", "title": f"RACE — {cfg['race']['name']}",
                              "distance_km": round(D["km"] + 3, 1), "pace_key": cfg["pace_map"]["@goal"],
                              "why": f"Race day. Warm up 10–15 min easy + 3–4 strides, then run {cfg['goals']['A']['time']} "
                                     f"({cfg['goals']['A']['pace_per_km']}/km). Start controlled — the first km should feel easy."}
            remaining -= D["km"] + 3
            for off in (-1,):
                d = (race_dow + off) % 7
                if d != race_dow and wstart + dt.timedelta(days=d) < race_date:
                    days[d] = {"type": "easy", "title": "Shake-out + 4×20s strides", "distance_km": 4, "pace_key": "easy",
                               "strides": "4×20s strides", "why": "Loosen up, nothing more."}
                    remaining -= 4

        # long run
        if days[long_dow] is None and long_dow in run_days and ph != "Race week":
            frac = {"marathon": 0.34, "half": 0.31, "10k": 0.28, "5k": 0.26}[dist_key] + (0.03 if runs <= 4 else 0)
            cap = D["long_cap"]
            if ph == "Taper":
                cap = round(cap * 0.7)
            lk = int(max(8, min(cap, round(target * frac))))
            if is_down:
                lk = int(round(lk * 0.85))
            days[long_dow] = _long_run(cfg, dist_key, ph, wk_in_phase, lk)
            remaining -= lk

        # quality
        q_slots = [q1_dow] + ([q2_dow] if two_q and ph in ("Build", "Race-specific") and not is_down else [])
        if ph == "Race week":
            q_slots = [q1_dow] if (race_dow is None or q1_dow < race_dow - 2) else []
        for qi, qd in enumerate(q_slots):
            if days[qd] is not None or qd not in run_days:
                continue
            if ph == "Race week":
                g = cfg["paces"][cfg["pace_map"]["@goal"]]["target"]
                q = {"title": "Race-week sharpener 3×1 km", "pace_key": cfg["pace_map"]["@goal"], "work_km": 3,
                     "reps": f"2 km warm-up · 3×1 km @{g}/km w/ 2 min jog · 2 km cool-down",
                     "why": "A short touch of goal pace to keep the legs sharp — not a workout to prove anything."}
            else:
                q = _quality(cfg, dist_key, ph, wk_in_phase, second=qi == 1)
            qkm = round(4 + q["work_km"] * 1.1)
            days[qd] = {"type": "quality", "title": q["title"], "distance_km": qkm, "pace_key": q["pace_key"],
                        "reps": q["reps"], "why": q["why"]}
            remaining -= qkm

        # easy days share what's left
        easy_slots = [d for d in sorted(run_days) if days[d] is None
                      and (race_dow is None or wstart + dt.timedelta(days=d) < race_date)]
        if easy_slots:
            per = max(4.0, remaining / len(easy_slots))
            for d in easy_slots:
                after_q = days[(d - 1) % 7] and days[(d - 1) % 7]["type"] in ("quality", "long")
                km = int(round(per * (0.8 if after_q else 1.05)))
                km = max(4, min(km, int(D["long_cap"] * 0.6) + 4))
                day = {"type": "easy", "title": "Recovery jog" if after_q else "Easy run", "distance_km": km,
                       "pace_key": "easy", "easy_cap": _easy_cap(cfg, recovery=bool(after_q)),
                       "why": ("Short and slow the day after a hard session, so it gets absorbed."
                               if after_q else "Easy aerobic volume — conversational effort. The base every hard day is built on.")}
                if days[(d + 1) % 7] and days[(d + 1) % 7]["type"] in ("long", "race") and not after_q:
                    day["title"] = "Easy run + 4–6 strides"
                    day["strides"] = "4–6×20s strides"
                days[d] = day

        for d in range(7):
            if days[d] is None:
                days[d] = {"type": "rest", "title": "Rest — full day off", "distance_km": 0,
                           "why": "Recovery is where the adaptation happens. Walk, stretch, sleep."}
            day = days[d]
            day["date"] = (wstart + dt.timedelta(days=d)).isoformat()
            day["dow"] = DOW[d]
            day["nightly"] = _nightly(d)
            if strength_dow is not None and d == strength_dow and day["type"] in ("easy", "rest"):
                s = _strength(ph)
                if s:
                    day["strength"] = s
                    day["strength_bailouts"] = _STRENGTH_BAILOUTS
                    if day["type"] == "easy":
                        day["title"] += " + strength"
            if day["date"] > race_date.isoformat():
                days[d] = {"date": day["date"], "dow": DOW[d], "type": "rest", "title": "Recovery — post-race",
                           "distance_km": 0, "why": "Rest. You earned it.", "nightly": _nightly(d)}

        focus = {
            "Base": "Build aerobic volume; keep the hard days light and the easy days easy.",
            "Build": "Threshold and VO2 work raise the engine; long runs grow.",
            "Race-specific": "Goal-pace work and race-specific long runs.",
            "Taper": "Volume drops, intensity stays — arrive fresh.",
            "Race week": "Short, sharp and rested. Trust the work.",
        }[ph]
        if is_down:
            focus = "Down week — absorb the last block before the next push."
        weeks.append({"week": wi + 1, "start_date": wstart.isoformat(),
                      "target_km": int(round(sum(x.get("distance_km") or 0 for x in days))),
                      "phase": ph, "focus": focus, "days": days})

    goal = cfg["goals"]["A"]
    return {
        "meta": {
            "name": f"{cfg['race']['name']} — {goal['time']} plan",
            "goal_time": f"{D['name'].title() if dist_key in ('half', 'marathon') else D['name']} {goal['time']}",
            "avg_weekly_km": round(sum(w["target_km"] for w in weeks) / len(weeks)),
            "quality_days": [f"{DOW_FULL[q1_dow]} quality"] + ([f"{DOW_FULL[q2_dow]} second quality (build + race-specific)"] if two_q else [])
                            + [f"{DOW_FULL[long_dow]} long run"],
            "notes": (f"{n_weeks}-week plan generated from your setup: base → build → race-specific → taper, "
                      f"ramping from ~{round(cur_km)} to ~{round(peak_km)} km/week with a down week every fourth week."),
            "editable": "Regenerate from Settings → Edit setup, or hand-edit data/plan.json (distances, reps, segments).",
            "strength_day": p.get("strength_day") or "none",
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        },
        "bailout_templates": BAILOUTS,
        "core_stability": "Nightly mobility is optional but cheap insurance — ten minutes of core and hips most nights "
                          "keeps the volume sustainable.",
        "weeks": weeks,
    }


BAILOUTS = {
    "easy": {"time": "Short on time → cut to 20–30 min, keep it Zone 2.",
             "readiness": "Amber/red or poor sleep → shorten or swap for a brisk walk. Easy days must stay truly easy.",
             "legs": "Tired legs → slow the pace and drop to ~20 min; don't force it.",
             "weather": "Bad weather → treadmill at 1% incline, same easy effort."},
    "quality": {"time": "Short on time → halve the reps, keep target pace.",
                "readiness": "Amber → run 60–70% of the reps; Red → swap for easy Z2 and push quality a day or two.",
                "legs": "Tired legs → drop one rep, lengthen the jogs; stop the set if pace fades >5 s/km off target.",
                "weather": "Bad weather → treadmill intervals or effort-based fartlek indoors."},
    "long": {"time": "Short on time → trim 20–30%, but protect any goal-pace block if you feel good.",
             "readiness": "Amber → keep distance, downgrade the fast finish to steady; Red → easy-only and 20–30% shorter.",
             "legs": "Tired legs → convert the fast finish to steady; walk-run the last km if needed.",
             "weather": "Bad weather → short loops near home; hydrate extra in heat."},
    "strength": {"time": "Short on time → 20-min core + single-leg circuit.",
                 "readiness": "Red readiness → mobility and foam-roll only.",
                 "legs": "Legs trashed → upper body + core, no lower-body loading.",
                 "weather": "No gym → bodyweight + band version."},
    "rest": {"note": "Full rest or gentle mobility/walk. Adaptation happens when you recover."},
    "race": {"note": "Trust the taper. Warm up 10–15 min easy + 3–4 strides. Start conservative."},
}


# ---------------- validate + save ----------------
def validate(profile: dict) -> dict:
    """Normalise and sanity-check the setup form. Raises ValueError with a
    message the UI can show next to the form."""
    p = dict(profile or {})
    if p.get("distance") not in DISTANCES:
        raise ValueError("Pick a race distance: 5k, 10k, half or marathon.")
    try:
        rd = dt.date.fromisoformat(str(p.get("race_date")))
    except ValueError:
        raise ValueError("Race date must be YYYY-MM-DD.") from None
    if rd <= dt.date.today() + dt.timedelta(days=10):
        raise ValueError("Pick a race at least two weeks away.")
    gs = p.get("goal_seconds")
    if not gs and p.get("goal_time"):
        parts = [int(x) for x in str(p["goal_time"]).strip().split(":")]
        gs = parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]
    if not gs:
        raise ValueError("Enter a goal time (h:mm:ss or mm:ss).")
    p["goal_seconds"] = int(gs)
    km = DISTANCES[p["distance"]]["km"]
    pace = p["goal_seconds"] / km
    if not 150 <= pace <= 600:
        raise ValueError(f"That goal works out to {pace_str(pace)}/km — check the time and distance.")
    for k in ("current_km", "peak_km", "runs_per_week", "max_hr", "lthr", "weight_lb", "goal_weight_lb"):
        if p.get(k) in ("", None):
            p.pop(k, None)
        elif k in p:
            p[k] = float(p[k]) if k in ("current_km", "peak_km", "weight_lb", "goal_weight_lb") else int(float(p[k]))
    p.setdefault("current_km", 30.0)
    p.setdefault("peak_km", round(p["current_km"] * 1.3))
    if p["peak_km"] < p["current_km"]:
        p["peak_km"] = p["current_km"]
    p.pop("goal_time", None)
    return p


def generate(profile: dict, today: Optional[dt.date] = None) -> tuple[dict, dict]:
    p = validate(profile)
    cfg = build_config(p)
    plan = build_plan(p, cfg, today)
    return cfg, plan


def save(cfg: dict, plan: dict) -> None:
    os.makedirs(paths.DATA_DIR, exist_ok=True)
    for path, data in ((paths.CONFIG_PATH, cfg), (paths.PLAN_PATH, plan)):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)


if __name__ == "__main__":
    import sys
    prof = json.load(open(sys.argv[1], encoding="utf-8")) if len(sys.argv) > 1 else {
        "distance": "half", "race_date": (dt.date.today() + dt.timedelta(weeks=16)).isoformat(),
        "goal_time": "1:45:00", "current_km": 35, "peak_km": 55, "runs_per_week": 5}
    c, pl = generate(prof)
    for w in pl["weeks"]:
        print(f"Wk{w['week']:>2} {w['start_date']} {w['phase']:<14} {w['target_km']:>4} km  "
              + " | ".join(f"{d['dow']} {d['type'][:4]} {d['distance_km']}" for d in w["days"]))
