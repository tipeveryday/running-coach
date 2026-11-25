"""Activity analysis: a coach's look-back over recent running that surfaces the
patterns worth acting on (volume trend, easy/hard balance, easy-day discipline,
cadence, long-run progression, load ramp), plus per-run enrichment so the UI can
drill into any activity.

Classification leans on PACE (reliable) rather than the athlete's HR zones,
which are known to be mis-set on this account.
"""
from __future__ import annotations
import datetime as dt
import statistics
from typing import Optional

from planlib import load_config, pace_str, fmt_secs


def _week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _clean_cad(c):
    """Garmin cadence is sometimes per-leg or garbage — keep only plausible
    running values (and double obvious per-leg readings)."""
    if not c:
        return None
    if 60 <= c <= 110:      # reported per-leg → convert to full steps/min
        return round(c * 2)
    if 140 <= c <= 220:
        return round(c)
    return None             # implausible → treat as missing


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def _pctl(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


GAP_K = 2.0   # ~2 s/km faster per metre-climbed-per-km (flat-equivalent effort)


def _effort(run: dict, cfg: dict, rhr: float, pwr_lo, pwr_hi):
    """Blend grade-adjusted pace, raw pace, heart rate (%HRR) and power into one
    0–1 effort, weighted by which signals are present. Raw pace alone under-rates
    hilly runs, interval sessions (recovery jogs drag the average down) and heat
    days — HR and power catch that; grade-adjusted pace catches terrain."""
    paces = cfg["paces"]
    easy_hi = paces.get("easy", {}).get("sec_high") or 325
    rep = paces.get("rep", {}).get("sec") or 210
    span = max(1, easy_hi - rep)
    paceI = lambda p: _clamp((easy_hi - p) / span) if p else None

    pace = run.get("avg_pace_s_per_km")
    pace_i = paceI(pace)

    dist = run.get("distance_km") or 0
    elev = run.get("elev_gain_m") or 0
    gap_pace = gap_i = None
    if pace and dist:
        vgain = elev / dist                             # metres climbed per km
        gap_pace = int(max(rep, round(pace - vgain * GAP_K)))
        gap_i = paceI(gap_pace)

    hr = run.get("avg_hr")
    hr_i = None
    mx = ((cfg.get("hr_zones") or {}).get("max_hr")) or 190
    if hr and mx > rhr:
        hrr = (hr - rhr) / (mx - rhr)                   # % heart-rate reserve
        hr_i = _clamp((hrr - 0.60) / (0.95 - 0.60))

    pwr = run.get("avg_power")
    pwr_i = None
    if pwr and pwr >= 150 and pwr_hi and pwr_hi > pwr_lo:
        pwr_i = _clamp((pwr - pwr_lo) / (pwr_hi - pwr_lo))

    # HR is the truest read of aerobic intensity, so it leads; pace + grade-adjusted
    # pace describe the mechanical effort; power only corroborates.
    wts = {"pace": 0.25, "gap": 0.20, "hr": 0.45, "power": 0.10}
    sig = {"pace": pace_i, "gap": gap_i, "hr": hr_i, "power": pwr_i}
    num = den = 0.0
    for k, v in sig.items():
        if v is not None:
            num += wts[k] * v
            den += wts[k]
    effort = round(num / den, 2) if den else (round(pace_i, 2) if pace_i is not None else 0.0)
    comps = {k: (round(v, 2) if v is not None else None) for k, v in sig.items()}
    comps["gap_pace"] = gap_pace
    return effort, comps


def _is_workout(run: dict, cfg: dict) -> bool:
    """Did a real quality stimulus happen, even if warm-up/recovery/cool-down
    diluted the whole-run average into the grey zone? Uses Garmin's own workout
    metrics (aerobic/anaerobic training effect) plus max HR + time in the harder
    zones — all robust to a session that's only partly at effort."""
    # Clear workout signals only — a borderline aerobic-TE (~3.5) or a few strides'
    # worth of anaerobic-TE shouldn't promote an easy run.
    if (run.get("aerobic_te") or 0) >= 3.8 or (run.get("anaerobic_te") or 0) >= 1.5:
        return True
    lthr = (cfg.get("hr_zones") or {}).get("lthr")
    z = run.get("hr_zones_s") or {}
    ztot = sum(z.values()) or 1
    hard_pct = 100 * (z.get("z4", 0) + z.get("z5", 0)) / ztot   # sustained threshold+ time
    return bool(run.get("max_hr") and lthr and run["max_hr"] >= lthr and hard_pct >= 20)


def _classify(run: dict, effort: float, cfg: dict) -> str:
    """easy | moderate | quality | long, from blended effort + distance, with a
    workout-evidence override so an interval/tempo session buried inside warm-up
    and cool-down isn't mislabelled 'moderate'."""
    if (run.get("distance_km") or 0) >= 15:
        return "long"
    if effort >= 0.50 or _is_workout(run, cfg):
        return "quality"
    if effort < 0.30:
        return "easy"
    return "moderate"                                   # the grey zone


def _trend_word(delta, up_good=True):
    if abs(delta) < 1e-9:
        return "steady", "→"
    rising = delta > 0
    good = rising == up_good
    return ("climbing" if rising else "easing"), ("↗" if rising else "↘")


def analyze_activity(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    cfg = load_config()
    easy = cfg["paces"].get("easy", {})
    easy_sec = easy.get("sec_high") or 325           # slow end of easy (~5:25)
    easy_fast = easy.get("sec_low") or 295           # fast end of easy (~4:55)

    runs = [a for a in (snapshot or {}).get("activities", []) if a.get("date")]
    runs.sort(key=lambda r: r.get("start") or r.get("date"))
    if not runs:
        return {"has_data": False, "message": "No running activity synced yet."}

    # blended-effort anchors: resting HR (2-yr baseline) + personal power range
    try:
        import cache
        bl = cache.load_baselines() or {}
    except Exception:
        bl = {}
    rhr = (bl.get("rhr") or {}).get("recent_rested") or 50
    pwr_vals = [r["avg_power"] for r in runs if r.get("avg_power") and r["avg_power"] >= 150]
    pwr_lo = _pctl(pwr_vals, 0.20)
    pwr_hi = _pctl(pwr_vals, 0.85)
    # power only helps if it actually varies with effort; on many watches it barely
    # moves for runs, so a tight spread means it's not discriminating — drop it.
    if not (pwr_vals and pwr_hi and pwr_lo and (pwr_hi - pwr_lo) >= 0.08 * statistics.median(pwr_vals)):
        pwr_lo = pwr_hi = None
    for r in runs:
        eff, comps = _effort(r, cfg, rhr, pwr_lo, pwr_hi)
        r["_effort"] = eff
        r["_comps"] = comps
        r["_type"] = _classify(r, eff, cfg)

    weekly = (snapshot or {}).get("weekly_volume", [])
    wk_km = {w["week_start"]: w.get("km", 0) for w in weekly}

    def _rundate(r):
        return dt.date.fromisoformat(r["date"])

    last28 = [r for r in runs if (today - _rundate(r)).days < 28]
    prev28 = [r for r in runs if 28 <= (today - _rundate(r)).days < 56]

    def _km(rs):
        return round(sum(r.get("distance_km") or 0 for r in rs), 1)

    # ---- trend tiles ----
    vol_4wk = round(_km(last28) / 4, 1)
    vol_prev = round(_km(prev28) / 4, 1) if prev28 else None
    runs_per_wk = round(len(last28) / 4, 1)
    cad_vals = [c for c in (_clean_cad(r.get("avg_cadence")) for r in last28) if c]
    avg_cadence = round(statistics.mean(cad_vals)) if len(cad_vals) >= 3 else None
    longest = max((r.get("distance_km") or 0 for r in last28), default=0)
    # easy / hard split over the last 6 weeks — computed from TIME IN HR ZONE,
    # not whole-workout type. A quality session's warm-up + recovery jogs sit in
    # Z1-Z2 and are easy running; only the reps themselves are hard.
    #   easy   = Z1 + Z2 time
    #   grey   = Z3 (the "moderate" band that stalls both recovery and adaptation)
    #   hard   = Z4 + Z5
    win = [r for r in runs if (today - _rundate(r)).days < 42]
    tot_km = sum(r.get("distance_km") or 0 for r in win) or 1
    zsum = {"z1": 0.0, "z2": 0.0, "z3": 0.0, "z4": 0.0, "z5": 0.0}
    for r in win:
        z = r.get("hr_zones_s") or {}
        for k in zsum:
            zsum[k] += z.get(k) or 0.0
    ztot = sum(zsum.values()) or 1
    easy_pct = round(100 * (zsum["z1"] + zsum["z2"]) / ztot)
    grey_pct = round(100 * zsum["z3"] / ztot)
    hard_pct = round(100 * (zsum["z4"] + zsum["z5"]) / ztot)

    tiles = {
        "vol_4wk": vol_4wk, "vol_delta": (round(vol_4wk - vol_prev, 1) if vol_prev is not None else None),
        "runs_per_wk": runs_per_wk,
        "easy_pct": easy_pct, "hard_pct": hard_pct, "grey_pct": grey_pct,
        "avg_cadence": avg_cadence,
        "longest_km": round(longest, 1),
    }

    summary = _insights(runs, last28, prev28, win, tiles, easy_fast, easy_sec, snapshot)

    # ---- per-run enrichment (most recent 25) for the drill-down log ----
    log = []
    for r in list(reversed(runs))[:25]:
        z = r.get("hr_zones_s", {})
        zt = sum(z.values()) or 1
        log.append({
            "id": r.get("id"), "date": r.get("date"), "name": r.get("name"),
            "type": r["_type"],
            "distance_km": r.get("distance_km"),
            "duration": fmt_secs(r.get("moving_s")),
            "pace": pace_str(r.get("avg_pace_s_per_km")),
            "pace_s": r.get("avg_pace_s_per_km"),
            "avg_hr": r.get("avg_hr"), "max_hr": r.get("max_hr"),
            "cadence": _clean_cad(r.get("avg_cadence")),
            "vo": round(r["avg_vo"], 1) if r.get("avg_vo") else None,
            "gct": round(r["avg_gct"]) if r.get("avg_gct") else None,
            "vratio": round(r["avg_vratio"], 1) if r.get("avg_vratio") else None,
            "gct_balance": round(r["avg_gct_balance"], 1) if r.get("avg_gct_balance") else None,
            "stride": round(r["avg_stride"]) if r.get("avg_stride") else None,
            "load": round(r["training_load"]) if r.get("training_load") else None,
            "aerobic_te": r.get("aerobic_te"), "anaerobic_te": r.get("anaerobic_te"),
            "elev": round(r["elev_gain_m"]) if r.get("elev_gain_m") else None,
            "power": round(r["avg_power"]) if r.get("avg_power") and r["avg_power"] >= 150 else None,
            "zone_pct": {k: round(100 * (z.get(k, 0)) / zt) for k in ["z1", "z2", "z3", "z4", "z5"]},
            "effort": r.get("_effort"),
            "gap_pace": pace_str((r.get("_comps") or {}).get("gap_pace")) if (r.get("_comps") or {}).get("gap_pace") else None,
            "blend": r.get("_comps"),
            "note": _run_note(r, easy_fast, easy_sec),
        })

    # ---- chart series ----
    pace_series = [{"date": r["date"], "pace_s": r.get("avg_pace_s_per_km"),
                    "type": r["_type"], "km": r.get("distance_km")}
                   for r in runs if r.get("avg_pace_s_per_km")]
    cad_series = [{"date": r["date"], "cadence": _clean_cad(r.get("avg_cadence"))}
                  for r in runs if _clean_cad(r.get("avg_cadence"))]

    return {
        "has_data": True,
        "summary": summary,
        "tiles": tiles,
        "runs": log,
        "pace_series": pace_series,
        "cadence_series": cad_series,
        "dynamics": running_dynamics(runs, cfg),
        "target_km": cfg.get("weekly_target_km", 50),
    }


# ---------------- running dynamics (form) ----------------
# Garmin's Running Dynamics colour zones, from their published user percentiles.
_DYN = {
    "cadence": {"unit": "spm", "better": "high", "name": "Cadence",
        "bands": [[183, 999, "purple"], [174, 183, "blue"], [164, 174, "green"], [153, 164, "amber"], [0, 153, "red"]]},
    "vo": {"unit": "cm", "better": "low", "name": "Vertical oscillation",
        "bands": [[0, 6.4, "purple"], [6.4, 8.1, "blue"], [8.1, 10.1, "green"], [10.1, 11.8, "amber"], [11.8, 99, "red"]]},
    "vratio": {"unit": "%", "better": "low", "name": "Vertical ratio",
        "bands": [[0, 6.1, "purple"], [6.1, 7.4, "blue"], [7.4, 8.6, "green"], [8.6, 10.1, "amber"], [10.1, 99, "red"]]},
    "gct": {"unit": "ms", "better": "low", "name": "Ground contact time",
        "bands": [[0, 208, "purple"], [208, 240, "blue"], [240, 270, "green"], [270, 305, "amber"], [305, 999, "red"]]},
}
_DYN_LABEL = {"purple": "elite", "blue": "great", "green": "good", "amber": "high", "red": "very high"}
_DYN_TIP = {
    "cadence": "160-180 spm is a healthy range for most trained runners; individual optimum varies with leg length, height and pace. Anything above ~165 is fine — only worth nudging if you're hearing a heavy footstrike or the GCT / vertical-oscillation reads flag overstriding.",
    "vo": "Less vertical bounce sends more energy forward. Cue 'run tall, quiet feet'; strides and plyometrics bring it down.",
    "vratio": "Bounce relative to stride — the most economy-relevant of the four. Higher cadence and tall posture improve it.",
    "gct": "Shorter ground contact means a springier, quicker push-off. Build it with strides, hill sprints, and plyometrics.",
}
_DYN_SRC = {"cadence": "avg_cadence", "vo": "avg_vo", "vratio": "avg_vratio", "gct": "avg_gct"}


def _dyn_band(metric, val):
    if val is None:
        return None
    for lo, hi, color in _DYN[metric]["bands"]:
        if lo <= val < hi:
            lbl = _DYN_LABEL[color]
            if _DYN[metric]["better"] == "high" and color == "amber":
                lbl = "low"
            elif _DYN[metric]["better"] == "high" and color == "red":
                lbl = "very low"
            return {"color": color, "label": lbl}
    return None


def running_dynamics(runs, cfg):
    have = [r for r in runs if r.get("avg_vratio") or r.get("avg_vo") or r.get("avg_gct")]
    if not have:
        return None
    latest = have[-1]
    metrics = {}
    for m in ("cadence", "vo", "vratio", "gct"):
        val = latest.get(_DYN_SRC[m])
        if m == "cadence":
            val = _clean_cad(val)
        if val is None:
            continue
        val = round(val, 1) if m in ("vo", "vratio") else round(val)
        metrics[m] = {"val": val, "unit": _DYN[m]["unit"], "name": _DYN[m]["name"], "band": _dyn_band(m, val)}
    trend = [{"date": r["date"],
              "cadence": (lambda c: round(c) if c else None)(_clean_cad(r.get("avg_cadence"))),
              "vo": round(r["avg_vo"], 1) if r.get("avg_vo") else None,
              "vratio": round(r["avg_vratio"], 1) if r.get("avg_vratio") else None,
              "gct": round(r["avg_gct"]) if r.get("avg_gct") else None} for r in have]
    reads = [{"metric": _DYN[m]["name"], "band": metrics[m]["band"]["label"], "color": metrics[m]["band"]["color"], "tip": _DYN_TIP[m]}
             for m in metrics if metrics[m]["band"] and metrics[m]["band"]["color"] in ("amber", "red")]
    goal = (cfg.get("goals") or {}).get(cfg.get("primary_goal", "A"), {})
    gp, gt = goal.get("pace_per_km", "race pace"), goal.get("time", "your goal")
    if not reads:
        summary = (f"Efficient form across the board — Garmin green or better on every metric. That economy is exactly what lets "
                   f"you hold {gp}/km at a lower heart-rate cost; nudging vertical ratio and cadence toward the blue band is the last few percent.")
    else:
        weak = ", ".join(r["metric"].lower() for r in reads)
        summary = (f"Solid, average form (Garmin green) with the most room in {weak}. Better economy means holding {gp}/km for "
                   f"{gt} at a lower cost — the form drills are the lever, and they compound over a block.")
    balance = _gct_balance(have)
    return {"latest_date": latest["date"], "metrics": metrics, "trend": trend, "reads": reads,
            "summary": summary, "bands": _DYN, "balance": balance}


def _gct_balance(runs):
    """Ground-contact-time L/R balance — only recorded with a chest strap (HRM-Tri/Pro/Run)."""
    bal = [r for r in runs if r.get("avg_gct_balance")]
    if not bal:
        return None
    br = bal[-1]
    left = round(br["avg_gct_balance"], 1)          # Garmin reports the left-foot share
    right = round(100 - left, 1)
    dev = round(abs(left - 50), 1)
    if dev <= 0.7:
        color, quality = "green", "balanced"
    elif dev <= 1.5:
        color, quality = "amber", "slight lean"
    else:
        color, quality = "red", "notable imbalance"
    heavier = "left" if left > 50 else "right"       # foot with the longer ground contact
    if dev <= 0.7:
        tip = ("Within Garmin's balanced band (48.7-51.3%). Symmetry this good means no single-side overload - "
               "keep strength work bilateral and you're set.")
    else:
        tip = (f"Ground contact leans {heavier} by {dev}%. A persistent lean can hint at a mobility or strength asymmetry - "
               f"single-leg strength (split squats, single-leg RDLs) on the {heavier} side and core anti-rotation work help even it out.")
    return {"date": br["date"], "left": left, "right": right, "dev": dev, "color": color,
            "quality": quality, "heavier": heavier, "tip": tip,
            "trend": [{"date": r["date"], "left": round(r["avg_gct_balance"], 1)} for r in bal]}


def _run_note(r, easy_fast, easy_sec):
    t = r["_type"]
    pace = r.get("avg_pace_s_per_km")
    c = r.get("_comps") or {}
    gp = c.get("gap_pace")
    hilly = (r.get("elev_gain_m") or 0) >= 200 or (gp and pace and pace - gp >= 25)
    if t == "long":
        return "Long run — aerobic durability." + (" Hilly, so the true effort ran higher than the pace." if hilly else "")
    if t == "quality":
        drivers = []
        if c.get("hr") is not None and c["hr"] >= 0.5:
            drivers.append("heart rate")
        if c.get("power") is not None and c["power"] >= 0.6:
            drivers.append("power")
        if c.get("gap") is not None and c.get("pace") is not None and c["gap"] - c["pace"] >= 0.12:
            drivers.append("the climbing")
        if drivers:
            return f"Quality effort — a real stimulus, flagged by {', '.join(drivers)}, not just pace."
        # promoted on workout evidence: the hard block is diluted by warm-up/cool-down
        te = r.get("aerobic_te")
        ev = f"training-effect {te}" if te else "max HR + hard-zone time"
        return (f"Quality effort — the whole-run average looks moderate because warm-up, recovery and "
                f"cool-down dilute it, but {ev} confirm a genuine workout inside it.")
    if t == "moderate":
        return "Moderate 'grey-zone' effort — a bit hard for easy, not hard enough to be a workout."
    if hilly:
        return "Easy by feel, but hilly — grade-adjusted, the true effort was a notch higher than the pace."
    if pace and pace < easy_fast - 5:
        return "Logged easy but ran quicker than your easy range — keep easy days honest."
    return "Easy aerobic — right where base is built."


def _insights(runs, last28, prev28, win, tiles, easy_fast, easy_sec, snapshot) -> list[dict]:
    out = []

    # 1) Volume trend
    if tiles["vol_delta"] is not None:
        d = tiles["vol_delta"]
        word, arrow = _trend_word(d, up_good=True)
        if abs(d) < 2:
            out.append({"tone": "good", "title": f"Volume is holding ~{tiles['vol_4wk']} km/wk {arrow}",
                        "detail": "Consistent week-to-week mileage is exactly what builds the aerobic base. Hold this, then let the plan's step-ups nudge it."})
        elif d > 0:
            out.append({"tone": "good", "title": f"Volume is {word} (+{d} km/wk) {arrow}",
                        "detail": f"You've built from ~{tiles['vol_4wk']-d} to ~{tiles['vol_4wk']} km/wk over the last month — a healthy progressive load. Keep the jumps under ~10%/week."})
        else:
            out.append({"tone": "watch", "title": f"Volume has eased ({d} km/wk) {arrow}",
                        "detail": f"Down to ~{tiles['vol_4wk']} km/wk. If that wasn't a planned down week, protect your run days — consistency drives the projection more than any single session."})

    # 2) Easy / hard balance (polarization)
    if tiles["grey_pct"] >= 20:
        out.append({"tone": "improve", "title": f"{tiles['grey_pct']}% of your TIME sits in the Z3 'grey zone'",
                    "detail": ("That's HR too high to recover well but too low to drive real adaptation. "
                               "Push the easy days genuinely easy (Z1-Z2) and make the hard days hard (Z4-Z5); "
                               "polarizing frees up quality for the sessions that count. Time-in-zone, not km "
                               "of whole workouts — a quality session's warm-up and jogs count as easy time.")})
    elif tiles["easy_pct"] >= 75:
        out.append({"tone": "good", "title": f"Easy/hard split looks polarized ({tiles['easy_pct']}% easy time)",
                    "detail": (f"{tiles['hard_pct']}% of your running TIME sits at Z4-Z5 (threshold+) - close to the 80/20 "
                               f"polarized sweet spot. Measured from time in HR zone, not workout labels, so a "
                               f"quality session's warm-up and recovery jogs correctly count as easy.")})
    else:
        out.append({"tone": "watch", "title": f"Not polarized enough ({tiles['easy_pct']}% easy time, {tiles['hard_pct']}% hard)",
                    "detail": (f"The 80/20 model wants ~80% Z1-Z2 and ~20% Z4-Z5, with very little Z3 in between. "
                               f"You're at {tiles['easy_pct']}/{tiles['grey_pct']}/{tiles['hard_pct']} (easy/grey/hard by TIME). "
                               "Slow the easy days a touch and concentrate the intensity into fewer, sharper sessions.")})

    # 3) Easy-day discipline
    easy_runs = [r for r in win if r["_type"] == "easy" and r.get("avg_pace_s_per_km")]
    if easy_runs:
        avg_easy = statistics.mean(r["avg_pace_s_per_km"] for r in easy_runs)
        if avg_easy < easy_fast - 5:
            out.append({"tone": "watch", "title": f"Easy runs are creeping fast (~{pace_str(round(avg_easy))}/km)",
                        "detail": f"Your easy range is {pace_str(easy_fast)}–{pace_str(easy_sec)}/km. Faster 'easy' days quietly add fatigue and blunt the real workouts. Slow down until you could chat in full sentences."})
        else:
            out.append({"tone": "good", "title": f"Easy days are honest (~{pace_str(round(avg_easy))}/km)",
                        "detail": "You're keeping recovery runs truly easy — the discipline that lets the quality days land."})

    # 4) Cadence
    if tiles["avg_cadence"]:
        c = tiles["avg_cadence"]
        if c < 170:
            out.append({"tone": "improve", "title": f"Cadence is a little low (~{c} spm)",
                        "detail": "Lower cadence usually means overstriding and more braking/impact. Nudging toward 172–180 (quicker, lighter steps) improves efficiency and tends to reduce injury risk — try a few strides and a metronome on easy runs."})
        else:
            out.append({"tone": "good", "title": f"Cadence is in a good range (~{c} spm)",
                        "detail": "Turnover is where you want it — efficient and low-impact. Keep the strides going to hold it."})

    # 5) Long-run progression
    longs = [r for r in runs if r["_type"] == "long"]
    if len(longs) >= 2:
        recent_long = longs[-3:]
        seq = " → ".join(f"{round(r['distance_km'])}" for r in recent_long)
        out.append({"tone": "good", "title": f"Long runs are progressing ({seq} km)",
                    "detail": "Steady growth in the long run is the backbone of half-marathon endurance. The plan layers race-pace blocks onto these — that's where 4:10 becomes sustainable."})

    # 6) Consistency / gaps
    gaps = []
    for a, b in zip(runs, runs[1:]):
        g = (dt.date.fromisoformat(b["date"]) - dt.date.fromisoformat(a["date"])).days
        if g >= 4:
            gaps.append((a["date"], b["date"], g))
    if tiles["runs_per_wk"] >= 4 and not any(g[2] >= 5 for g in gaps[-4:]):
        out.append({"tone": "good", "title": f"Consistent rhythm (~{tiles['runs_per_wk']} runs/wk)",
                    "detail": "Frequency this steady is a bigger driver of fitness than any single hard session. Protect it."})
    elif gaps and gaps[-1][2] >= 5:
        out.append({"tone": "watch", "title": f"A {gaps[-1][2]}-day gap recently broke the rhythm",
                    "detail": "The odd break is fine, but repeated gaps flatten the aerobic build. Even a short 20-30 min jog keeps the chain going."})

    return out[:6]
