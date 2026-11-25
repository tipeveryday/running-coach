"""Coach logic: morning readiness verdict, post-run analysis, progress vs plan,
and a rule-based chat that answers like a running coach who knows the athlete.

All logic degrades gracefully when Garmin fields are missing (device not synced),
returning a "no data yet" style read instead of crashing.
"""
from __future__ import annotations
import datetime as dt
from typing import Any, Optional

import planlib
from planlib import (load_config, load_plan, weeks_to_race, get_day, resolve_day,
                     find_week_for_date, fmt_secs, pace_str, derived_paces, enrich_plan,
                     resolve_active_race)
import insights as insightslib


def _run_insights(run, day, cfg, snapshot, stream_provider):
    """Rich, type-specific coaching insights for a run (uses the stream when a
    provider is available)."""
    stream = None
    if stream_provider and run.get("id"):
        try:
            s = stream_provider(run["id"])
            if s and s.get("has_data"):
                stream = s
        except Exception:
            stream = None
    try:
        return insightslib.build_insights(run, day, cfg, snapshot, stream=stream)
    except Exception:
        return []


# ---------------- helpers ----------------
def _latest(seq, key="date"):
    return seq[-1] if seq else None


def _get_by_date(seq, date_iso, key="date"):
    for x in seq:
        if x.get(key) == date_iso:
            return x
    return None


def _week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def _run_on_date(snapshot: dict, date_iso: str) -> Optional[dict]:
    """The run logged on a given date (longest, if more than one)."""
    best = None
    for r in (snapshot or {}).get("activities", []):
        if r.get("date") == date_iso:
            if best is None or (r.get("distance_km") or 0) > (best.get("distance_km") or 0):
                best = r
    return best


def _most_recent_run(snapshot: dict) -> Optional[dict]:
    runs = [r for r in (snapshot or {}).get("activities", []) if r.get("date")]
    if not runs:
        return None
    return max(runs, key=lambda r: r.get("start") or r.get("date"))


_RUN_TYPES = ("easy", "quality", "long", "race")


def hr_zone_for(hr, cfg: dict) -> Optional[dict]:
    """Classify an average HR into the athlete's configured LThr-based zones
    (config.json → hr_zones). Used instead of Garmin's per-activity zone bins,
    which are mis-set on this account."""
    if not hr:
        return None
    zones = (cfg.get("hr_zones") or {}).get("zones") or []
    for z in zones:
        lo = z.get("low", 0)
        hi = z.get("high")
        if hr >= lo and (hi is None or hr <= hi):
            return {
                "zone": z["zone"],
                "name": z.get("name"),
                "label": f"Z{z['zone']} · {z.get('name')}",
                "bpm": f"{lo}–{hi}" if hi and hi < 999 else f"{lo}+",
            }
    return None


# ---------------- morning verdict ----------------
def _verdict_core(readiness, hrv, bb, rhr, sleep, baseline, day, yesterday=False) -> dict:
    """Green/amber/red read from a set of recovery signals + the day's session.
    Shared by the live morning verdict and the historical day-context lookup."""
    signals = []
    score_points = []
    bb_word = "the day before" if yesterday else "yesterday"

    r_score = readiness.get("score") if readiness else None
    if r_score is not None:
        signals.append(f"Training readiness {r_score}/100 ({(readiness.get('level') or '').title()}).")
        score_points.append(("readiness", r_score))
    if hrv and hrv.get("last_night_avg") is not None:
        v = hrv["last_night_avg"]
        if baseline and baseline.get("low") and baseline.get("upper"):
            lo, hi = baseline["low"], baseline["upper"]
            if v < lo:
                signals.append(f"HRV {v} ms is below your balanced range ({lo}–{hi}) — a fatigue flag.")
                score_points.append(("hrv", 35))
            elif v > hi:
                signals.append(f"HRV {v} ms is above baseline ({lo}–{hi}) — well recovered.")
                score_points.append(("hrv", 80))
            else:
                signals.append(f"HRV {v} ms sits inside your balanced range ({lo}–{hi}).")
                score_points.append(("hrv", 65))
        else:
            signals.append(f"Last-night HRV {v} ms.")
    if sleep and sleep.get("total_s"):
        hrs = sleep["total_s"] / 3600.0
        sc = sleep.get("score")
        signals.append(f"Slept {hrs:.1f} h" + (f" (score {sc})" if sc else "") + ".")
        score_points.append(("sleep", 40 if hrs < 6 else 60 if hrs < 7 else 80))
    if rhr and rhr.get("rhr"):
        signals.append(f"Resting HR {rhr['rhr']} bpm.")
    if bb and bb.get("high") is not None:
        signals.append(f"Body Battery peaked at {bb['high']} {bb_word}.")

    hard_day = bool(day and day.get("type") in ("quality", "long", "race"))

    # ---- Recovery Time: the key "am I ready for HARD work" gauge ----
    # Garmin counts the hours until you're recovered enough to BENEFIT from a hard
    # effort — it captures accumulated load that a single night's HRV/sleep misses.
    # Easy running is fine (even helpful) during this window; it only gates hard days.
    rec_min = readiness.get("recovery_time_min") if readiness else None
    rec_h = round(rec_min / 60) if rec_min else (0 if rec_min == 0 else None)
    rec_gate = None  # "cap_amber" | "force_red" — how recovery time limits hard work
    if rec_h is not None:
        if rec_h <= 1:
            signals.append("Recovery time is clear — Garmin rates you primed for a hard effort.")
        elif hard_day:
            signals.append(f"Recovery time: ~{rec_h} h until you're recovered enough to BENEFIT from a hard effort.")
        else:
            signals.append(f"Recovery time ~{rec_h} h — fine for easy running, which actually speeds recovery.")
        if hard_day:
            if rec_h <= 3:
                score_points.append(("recovery_time", 82))
            elif rec_h <= 8:
                score_points.append(("recovery_time", 62))
            elif rec_h <= 16:
                score_points.append(("recovery_time", 44)); rec_gate = "cap_amber"
            else:
                score_points.append(("recovery_time", 30)); rec_gate = "force_red"

    data_present = bool(score_points)
    if not data_present:
        return {"verdict": "grey", "headline": "No recovery data",
                "recommendation": ("No recovery metrics synced for this day. Judge by feel: if you slept "
                                   "well and the legs felt good, run as written; if not, treat it as easy."),
                "signals": signals, "has_data": False, "recovery_hours": rec_h}
    avg = sum(p[1] for p in score_points) / len(score_points)
    verdict = "green" if avg >= 66 else "amber" if avg >= 48 else "red"
    # apply the hard-effort recovery gate (recovery time can veto a hard session)
    _rank = {"green": 0, "amber": 1, "red": 2}
    if rec_gate == "force_red":
        verdict = "red"
    elif rec_gate == "cap_amber" and _rank[verdict] < 1:
        verdict = "amber"
    headline = {"green": "Good to go", "amber": "Proceed with caution", "red": "Back off"}[verdict]
    return {"verdict": verdict, "headline": headline,
            "recommendation": _verdict_recommendation(verdict, day, hard_day, rec_h, rec_gate),
            "signals": signals, "has_data": True,
            "recovery_hours": rec_h, "hard_gated": bool(rec_gate)}


def morning_verdict(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    """Green / amber / red read tied to today's prescribed workout."""
    today = today or dt.date.today()
    today_iso = today.isoformat()
    cfg = resolve_active_race(load_config(), today)
    plan = load_plan()
    day = get_day(plan, today_iso)
    day = resolve_day(day, plan, cfg) if day else None

    core = _verdict_core(
        snapshot.get("latest_readiness") if snapshot else None,
        _latest(snapshot.get("hrv", [])) if snapshot else None,
        _latest(snapshot.get("body_battery", [])) if snapshot else None,
        _latest(snapshot.get("rhr_trend", [])) if snapshot else None,
        _latest(snapshot.get("sleep", [])) if snapshot else None,
        snapshot.get("hrv_baseline") if snapshot else None,
        day,
    )
    if not core["has_data"]:
        core["headline"] = "No fresh recovery data synced"
    return {
        "date": today_iso,
        "session": _session_brief(day) if day else None,
        **core,
    }


def _session_brief(day: dict) -> dict:
    pace = day.get("pace") or {}
    pace_txt = pace.get("target") or (f"{pace.get('low')}–{pace.get('high')}" if pace.get("low") else None)
    return {
        "type": day.get("type"),
        "title": day.get("title"),
        "distance_km": day.get("distance_km"),
        "pace": pace_txt,
        "zone": pace.get("zone"),
        "reps": day.get("reps"),
        "why": day.get("why"),
        "bailouts": day.get("bailouts"),
        "strength": day.get("strength"),
        "strength_bailouts": day.get("strength_bailouts"),
    }


def _verdict_recommendation(verdict: str, day: Optional[dict], hard_day: bool,
                            rec_h: Optional[int] = None, rec_gate: Optional[str] = None) -> str:
    title = (day or {}).get("title", "today's session")
    dtype = (day or {}).get("type")
    # Recovery-time takes priority on hard days: hard work should wait until you're
    # recovered enough to benefit from it; easy running in the meantime is fine.
    if hard_day and rec_gate == "force_red":
        return (f"You've still got ~{rec_h} h of recovery time before Garmin rates you ready to *benefit* from hard "
                f"work — that's the accumulated load from your last big session, not just last night. A {dtype} effort "
                f"now would land compromised and mostly bank fatigue, not fitness. Do an easy Z2 run today (easy running "
                f"actually speeds recovery), and move {title} to when recovery time is near zero — likely in a day.")
    if hard_day and rec_gate == "cap_amber":
        return (f"~{rec_h} h of recovery time left, so you're not fully primed for {title}. Two good options: run it at "
                f"~70% (cut the reps, hold the easy end of the pace band, bail if pace drifts >5s/km), or swap easy today "
                f"and hit it fresh once recovery time clears. Easy running today won't set you back.")
    if verdict == "green":
        if dtype == "rest":
            return f"Everything looks recovered. Today is scheduled rest — take it; the freshness will pay off in your next quality day."
        if dtype == "easy":
            return ("Recovery markers are solid — which is exactly why today should still be easy. "
                    "Hold the top of Z2 (HR ≤ 153) and start the first 2 km slow; feeling good is the "
                    "most common reason an easy day quietly turns into a workout.")
        return f"Recovery markers are solid. Run {title} as prescribed and hit your targets with confidence."
    if verdict == "amber":
        if dtype == "quality":
            return (f"Signals are a touch soft. Do {title} but trim to ~70% of the reps and stop the set "
                    f"if pace drifts more than 5s/km off target. Better a clean partial session than a "
                    f"sloppy full one.")
        if dtype == "long":
            return (f"Keep the distance on {title} but downgrade any race-pace block to steady effort. "
                    f"Fuel and hydrate well; let it be a controlled aerobic day.")
        if dtype == "race":
            return "Signals are soft on race day — start at your B/C goal pace and reassess at 10 km."
        return f"Slightly under-recovered. Keep {title} easy and short; don't turn a recovery day into work."
    # red
    if dtype in ("quality", "long"):
        return (f"You're under-recovered for {title}. Swap it for easy Z2 or full rest today and move the "
                f"quality work to tomorrow or the next good day. One shifted session won't hurt the plan; "
                f"forcing it when red will.")
    if dtype == "race":
        return "Red on race morning — respect it: run by feel, start conservative (C goal), and treat finishing strong as the win."
    return f"Take today fully easy or rest. Prioritize sleep and food; let the body catch up."


# ---------------- post-run analysis ----------------
def _effort_blend(run: dict, snapshot: dict, cfg: dict):
    """Blended effort (grade-adjusted pace + pace + HR + power) for one run,
    reusing the Activity-tab engine so the coach reads effort the same way."""
    try:
        import activity as act
        import cache as _cache
        import statistics as _st
    except Exception:
        return None, None
    pw = [r["avg_power"] for r in (snapshot or {}).get("activities", [])
          if r.get("avg_power") and r["avg_power"] >= 150]
    pwr_lo = act._pctl(pw, 0.20)
    pwr_hi = act._pctl(pw, 0.85)
    if not (pw and pwr_hi and pwr_lo and (pwr_hi - pwr_lo) >= 0.08 * _st.median(pw)):
        pwr_lo = pwr_hi = None
    try:
        bl = _cache.load_baselines() or {}
    except Exception:
        bl = {}
    rhr = (bl.get("rhr") or {}).get("recent_rested") or 50
    return act._effort(run, cfg, rhr, pwr_lo, pwr_hi)


def _work_hold(work: list) -> Optional[dict]:
    """Within the work portion, can the athlete HOLD the effort at a stable heart
    rate? Compares the first vs second half: if HR climbs while pace holds (or
    slips), that's cardiac drift — the aerobic system giving way under the load."""
    w = [p for p in work if p.get("hr") and p.get("pace_s")]
    if len(w) < 12:
        return None
    w.sort(key=lambda p: p.get("t") or 0)
    h = len(w) // 2
    a, b = w[:h], w[h:]

    def avg(chunk, key):
        vals = [c[key] for c in chunk if c.get(key)]
        return sum(vals) / len(vals) if vals else None

    hr1, hr2 = avg(a, "hr"), avg(b, "hr")
    p1, p2 = avg(a, "pace_s"), avg(b, "pace_s")
    if None in (hr1, hr2, p1, p2):
        return None
    ef1, ef2 = (1000.0 / p1) / hr1, (1000.0 / p2) / hr2   # speed per beat
    drift = round(100 * (ef1 - ef2) / ef1, 1)
    hr_rise = round(hr2 - hr1)
    pace_slow = round(p2 - p1)                            # +ve = slowed down
    if drift < 3:
        tone, verdict = "green", "held the effort strong to the end"
    elif drift < 6:
        tone, verdict = "amber", "started to drift late — HR creeping up as pace held"
    else:
        tone, verdict = "red", "faded at the end — HR spiking while pace slipped on the same terrain"
    return {"hr_rise": hr_rise, "pace_slow": pace_slow, "drift": drift, "tone": tone,
            "verdict": verdict, "hr1": round(hr1), "hr2": round(hr2),
            "pace1": round(p1), "pace2": round(p2)}


def _quality_work(series: list, target_sec: float) -> Optional[dict]:
    """Isolate the hard 'work' portion of an interval/tempo session from its
    stream, so we judge the reps — not the whole-run average that also contains
    warm-up, recovery jogs and cool-down. Selection is grade-adjusted so hilly
    terrain never hides a genuine effort."""
    pts = [p for p in series if p.get("gap_s") and 120 < p["gap_s"] < 650]
    if len(pts) < 12:
        return None
    thr = target_sec * 1.18                              # within ~18% of target GAP = "running the rep"
    work = [p for p in pts if p["gap_s"] <= thr]
    if len(work) < max(8, len(pts) * 0.12):              # target may have been ambitious
        work = sorted(pts, key=lambda p: p["gap_s"])[:max(8, len(pts) // 3)]
    if len(work) < 6:
        return None
    gap = sum(p["gap_s"] for p in work) / len(work)
    paces = [p["pace_s"] for p in work if p.get("pace_s")]
    hrs = [p["hr"] for p in work if p.get("hr")]
    out = {"gap": round(gap),
           "pace": round(sum(paces) / len(paces)) if paces else None,
           "hr": round(sum(hrs) / len(hrs)) if hrs else None,
           "frac": len(work) / len(pts)}
    hold = _work_hold(work)
    if hold:
        out["hold"] = hold
    return out


def _classify_effort(run: dict, day: Optional[dict], cfg: dict, snapshot: Optional[dict] = None,
                     stream_provider=None) -> dict:
    """Compare a completed run to its prescribed day. Shared by the last-run
    panel and today's post-run verdict. Returns verdict/headline/hit/notes/tips,
    the HR-zone split, and a blended multi-factor effort read."""
    actual_pace = run.get("avg_pace_s_per_km")
    zones = run.get("hr_zones_s", {})
    ztotal = sum(zones.values()) or 1
    z_pct = {z: round(100 * zones.get(z, 0) / ztotal) for z in ["z1", "z2", "z3", "z4", "z5"]}

    notes: list[str] = []
    tips: list[str] = []
    hit: Optional[bool] = None
    verdict = "green"
    headline = "Session complete"
    kind = "aerobic"  # "aerobic" | "hit" | "miss" — drives the race-pace impact line
    dtype = (day or {}).get("type")

    def aerobic_control(label: str):
        """Judge an easy/all-easy run. Pace is the reliable signal (the plan is
        pace-based); HR zones are only a secondary confirmation because a device's
        zone boundaries are often mis-set. So we lead with pace and never fail an
        easy run on zones alone."""
        nonlocal hit, verdict, headline, kind
        kind = "aerobic"
        easy = cfg["paces"].get("easy", {})
        lo = easy.get("sec_low")   # fast end of easy range, e.g. 4:55 = 295 s/km
        rng = f"{easy.get('low')}–{easy.get('high')}/km" if easy.get("low") else "your easy range"
        if actual_pace and lo:
            if actual_pace < lo - 8:  # meaningfully faster than the easy range
                hit, verdict, headline = False, "amber", f"{label} — a bit quick for easy"
                notes.append(f"Averaged {pace_str(actual_pace)}/km — quicker than the easy range ({rng}). "
                             f"Easy means easy; keeping these controlled is what lets the quality days land.")
                tips.append("Next easy run: hold it so you could talk in full sentences the whole way.")
            else:
                hit, verdict, headline = True, "green", f"{label}, banked clean"
                notes.append(f"Averaged {pace_str(actual_pace)}/km — right in the easy range ({rng}). "
                             f"Textbook aerobic effort.")
        else:
            # no pace available — fall back to HR zones, but only as a soft read
            hard_pct = z_pct["z3"] + z_pct["z4"] + z_pct["z5"]
            if hard_pct > 40:
                hit, verdict, headline = False, "amber", f"{label} — check the effort"
                notes.append(f"No pace recorded; Garmin logged {hard_pct}% above Z2, but that depends on your "
                             f"HR-zone settings. If it felt easy, it was easy.")
            else:
                hit, verdict, headline = True, "green", f"{label}, banked clean"

    if not day:
        notes.append("This run isn't on the plan calendar (bonus or cross-training) — logged, "
                     "but no target to compare against.")
        headline = "Bonus run logged"
    elif dtype == "easy":
        aerobic_control("Easy run")
    elif dtype in ("quality", "long", "race"):
        pk = day.get("pace_key")
        target = cfg["paces"].get(pk, {}) if pk else {}
        is_hmp_block = bool(day.get("hmp_km"))
        tgt_sec = cfg["paces"]["hmp"]["sec"] if is_hmp_block else target.get("sec")
        tgt_str = cfg["paces"]["hmp"]["target"] if is_hmp_block else target.get("target")

        # Judge the WORK portion from the stream (reps / race-pace block), not the
        # whole-run average — that average also contains warm-up, recovery jogs and
        # cool-down, so it always reads slow for an interval or tempo session.
        work = None
        if stream_provider and run.get("id") and tgt_sec:
            try:
                s = stream_provider(run["id"])
                if s and s.get("has_data"):
                    work = _quality_work(s.get("series", []), tgt_sec)
            except Exception:
                work = None

        if work and tgt_sec:
            wgap = work["gap"]
            diff = wgap - tgt_sec
            wsecs = round(work["frac"] * (run.get("moving_s") or 0))
            raw = work.get("pace")
            raw_txt = (f" (~{pace_str(raw)}/km raw on the terrain)"
                       if raw and (raw - wgap) >= 12 else "")
            hr_txt = f" at HR {work['hr']}" if work.get("hr") else ""
            block = "race-pace block" if is_hmp_block else "the work"
            if diff <= 8:
                hit, verdict, headline, kind = True, "green", "Quality session — nailed the work", "hit"
                read = "bang on target"
            elif diff <= 22:
                hit, verdict, headline, kind = True, "green", "Quality session — solid work", "hit"
                read = "right there once the terrain is accounted for"
            else:
                hit, verdict, headline, kind = False, "amber", "Quality session — work ran short", "miss"
                read = f"about {pace_str(diff)}/km off the {tgt_str}/km target"
            notes.append(
                f"Judged on {block}, not the whole-run average: your work ran ~{pace_str(wgap)}/km "
                f"grade-adjusted{raw_txt}{hr_txt} over ~{fmt_secs(wsecs)} — {read}. The "
                f"{pace_str(actual_pace)}/km overall average includes warm-up, recovery jogs and cool-down.")
            hd = work.get("hold")
            if hd:
                notes.append(
                    f"Holding the effort: across the {'block' if is_hmp_block else 'reps'} your HR went "
                    f"{hd['hr1']}→{hd['hr2']} bpm while pace held {pace_str(hd['pace1'])}→{pace_str(hd['pace2'])}/km "
                    f"— you {hd['verdict']} ({hd['drift']}% drift). " +
                    ("Strong aerobic durability at this intensity." if hd["tone"] == "green"
                     else "The late HR climb at the same pace is the aerobic ceiling showing — more threshold "
                          "volume and long-run strength push it out." if hd["tone"] == "red"
                     else "A little late drift is normal at threshold; it shrinks as the engine strengthens."))
        elif is_hmp_block and actual_pace:
            hmp_s = cfg["paces"]["hmp"]["sec"]
            if actual_pace <= hmp_s + 8:
                hit, verdict, headline, kind = True, "green", "Race-pace long run — nailed it", "hit"
                notes.append(f"You averaged {pace_str(actual_pace)}/km — right around race pace. "
                             f"Strong execution of the race-pace work.")
            else:
                hit, verdict, headline, kind = False, "amber", "Race-pace work — check the block", "miss"
                notes.append(f"Average pace was {pace_str(actual_pace)}/km; the race-pace block target is "
                             f"{cfg['paces']['hmp']['target']}/km. On a long run the average includes easy "
                             f"portions, so check the block itself — if it felt hard, that's useful feedback "
                             f"about current fitness.")
        elif dtype == "quality" and actual_pace and target.get("sec"):
            # no stream available — never fail a quality day on the whole-run average
            # (it always includes warm-up/recovery). Judge on the workout stimulus.
            hard_pct = z_pct["z3"] + z_pct["z4"] + z_pct["z5"]
            lthr = (cfg.get("hr_zones") or {}).get("lthr")
            ate = run.get("aerobic_te") or 0
            strong = ate >= 3.5 or hard_pct >= 20 or (run.get("max_hr") and lthr and run["max_hr"] >= lthr)
            if strong:
                hit, verdict, headline, kind = True, "green", "Quality session — stimulus banked", "hit"
                notes.append(f"A quality session's whole-run average ({pace_str(actual_pace)}/km) includes "
                             f"warm-up, recovery jogs and cool-down, so it reads slower than the "
                             f"{target.get('target')}/km rep target — that's expected. Max HR {run.get('max_hr')}, "
                             f"aerobic training-effect {ate}, and time in the harder zones confirm a real stimulus. "
                             f"Open the run to see the reps in the trace.")
            else:
                hit, verdict, headline, kind = None, "amber", "Quality session — effort looks light", "miss"
                notes.append(f"Averaged {pace_str(actual_pace)}/km with limited time in the harder HR zones and a "
                             f"modest training-effect. If the reps hit target it's fine — but the effort read is "
                             f"light for a quality day.")
        else:
            # all-easy long run (no race-pace block) — judge like an aerobic day
            aerobic_control("Long run" if dtype == "long" else "Session")
    else:
        # a run logged on a rest/strength day
        headline = "Bonus run logged"

    hr_zone = hr_zone_for(run.get("avg_hr"), cfg)
    if run.get("avg_hr"):
        zsuffix = f" — {hr_zone['label']}" if hr_zone else ""
        notes.append(f"Average HR {run['avg_hr']} bpm, max {run.get('max_hr')}{zsuffix}.")

    # ---- blended multi-factor effort (grade-adjusted pace + pace + HR + power) ----
    effort, comps = _effort_blend(run, snapshot, cfg) if snapshot is not None else (None, None)
    gap_pace = None
    if effort is not None and comps:
        labels = []
        if comps.get("hr") is not None:
            labels.append("heart rate")
        labels.append("pace")
        if comps.get("gap") is not None:
            labels.append("grade-adjusted pace")
        if comps.get("power") is not None:
            labels.append("power")
        gp = comps.get("gap_pace")
        gap_pace = pace_str(gp) if gp else None
        hilly = gp and actual_pace and (actual_pace - gp) >= 25
        extra = (f" Hills lifted the real effort — grade-adjusted ~{pace_str(gp)}/km vs {pace_str(actual_pace)}/km."
                 if hilly else "")
        notes.append(f"Blended effort {round(effort * 100)}/100, weighing {', '.join(labels)} — "
                     f"not raw pace alone.{extra}")

    return {"verdict": verdict, "headline": headline, "hit": hit, "kind": kind,
            "zone_pct": z_pct, "hr_zone": hr_zone, "notes": notes, "tips": tips,
            "effort": effort, "blend": comps, "gap_pace": gap_pace}


def _race_impact(day: Optional[dict], kind: str, cfg: dict, snapshot: dict) -> str:
    """One-line forward read: how today's effort bears on the goal-race target.
    `kind` is 'aerobic' (easy/base), 'hit' (race-pace nailed) or 'miss'."""
    dp = derived_paces(cfg, snapshot)
    rp = (snapshot or {}).get("race_predictions")
    label = planlib.race_label(cfg)
    noun = planlib.RACE_NOUNS[label]
    pred_key = planlib.PRED_KEYS[label]
    proj = fmt_secs(rp[pred_key]) if rp and rp.get(pred_key) else None
    assess = dp.get("assessment")
    goal = cfg["goals"][cfg.get("primary_goal", "A")]
    gt, gp = goal["time"], goal["pace_per_km"]

    proj_line = ""
    if proj:
        if assess == "on_track":
            proj_line = f" Garmin projects {proj} for {noun} — at or ahead of your {gt} goal."
        elif assess == "slightly_ambitious":
            proj_line = (f" Garmin projects {proj}, a touch off {gp}/km goal pace — "
                         f"sessions like this close the gap.")
        elif assess:
            proj_line = (f" Garmin still projects {proj}; {gt} is a stretch from today's fitness, "
                         f"but consistent race-pace work is what moves it.")
        else:
            proj_line = f" Garmin projects {proj} for {noun}."

    if not day or (day.get("type") not in _RUN_TYPES):
        return ("Bonus aerobic work — every easy km supports the engine." + proj_line).strip()
    if kind == "hit":
        return (f"Exactly the stimulus {gt} asks for — holding goal pace when it's prescribed is how "
                f"{gp}/km becomes automatic." + proj_line).strip()
    if kind == "miss":
        return ("Today landed above target. One session won't move the projection, but the race-pace "
                f"blocks are where {gt} is won — bank a clean one next time." + proj_line).strip()
    # aerobic (easy day or all-easy long run)
    return (f"Aerobic base work — it builds the engine race pace rides on; the {gt} call is made on the "
            "quality and race-pace long-run days." + proj_line).strip()


def _recent_runs(snapshot: dict) -> list:
    """All logged runs, most-recent first."""
    runs = [r for r in (snapshot or {}).get("activities", []) if r.get("date")]
    runs.sort(key=lambda r: r.get("start") or r.get("date"), reverse=True)
    return runs


def _analyze_run(run: dict, snapshot: dict, stream_provider=None) -> dict:
    """Full coach analysis for a single run (shared by the last-run card and its
    history pager)."""
    plan = load_plan()
    _rd = run.get("date")
    cfg = resolve_active_race(load_config(), dt.date.fromisoformat(_rd) if _rd else None)
    day = get_day(plan, run.get("date"))
    day = resolve_day(day, plan, cfg) if day else None

    cls = _classify_effort(run, day, cfg, snapshot, stream_provider)
    tips = cls["tips"]
    if not tips:
        tips = ["Refuel with carbs + protein in the next 30–60 min and get a solid night's sleep "
                "to lock in the adaptation."]

    return {
        "run": {
            "name": run.get("name"),
            "date": run.get("date"),
            "distance_km": run.get("distance_km"),
            "duration": fmt_secs(run.get("moving_s")),
            "avg_pace": pace_str(run.get("avg_pace_s_per_km")),
            "gap_pace": cls.get("gap_pace"),
            "avg_hr": run.get("avg_hr"),
            "power": round(run["avg_power"]) if run.get("avg_power") and run["avg_power"] >= 150 else None,
            "training_load": run.get("training_load"),
            "aerobic_te": run.get("aerobic_te"),
        },
        "headline": cls.get("headline"),
        "verdict": cls.get("verdict"),
        "zone_pct": cls["zone_pct"],
        "hr_zone": cls["hr_zone"],
        "effort": cls.get("effort"),
        "blend": cls.get("blend"),
        "prescribed": _session_brief(day) if day else None,
        "hit_hmp": cls["hit"],
        "notes": cls["notes"],
        "insights": _run_insights(run, day, cfg, snapshot, stream_provider),
        "tip": tips[0],
    }


def analyze_last_run(snapshot: dict, stream_provider=None, offset: int = 0) -> Optional[dict]:
    """Coach analysis for a past run. offset 0 = most recent (the spotlight);
    higher offsets step back through history. Includes pager metadata so the card
    can scroll while keeping the latest as the default view."""
    runs = _recent_runs(snapshot)
    if not runs:
        return None
    offset = max(0, min(offset, len(runs) - 1))
    out = _analyze_run(runs[offset], snapshot, stream_provider)
    out["index"] = offset
    out["count"] = len(runs)
    out["has_older"] = offset < len(runs) - 1
    out["has_newer"] = offset > 0
    return out


# ---------------- today's read (pre-run verdict OR post-run analysis) ----------------
_FEEL_LABELS = [(100, "Very strong", "green"), (75, "Strong", "green"), (50, "Normal", "grey"),
                (25, "Weak", "amber"), (0, "Very weak", "red")]
_RPE_LABELS = [(90, "Maximal"), (80, "Very hard"), (70, "Hard"), (55, "Somewhat hard"),
               (40, "Moderate"), (25, "Easy"), (0, "Very easy")]


def _feel_label(v):
    if v is None:
        return None
    for thr, lbl, tone in _FEEL_LABELS:
        if v >= thr:
            return {"value": v, "label": lbl, "tone": tone}
    return {"value": v, "label": "Very weak", "tone": "red"}


def _rpe_label(v):
    if v is None:
        return None
    for thr, lbl in _RPE_LABELS:
        if v >= thr:
            tone = "red" if v >= 70 else "amber" if v >= 55 else "green"
            return {"value": v, "score10": round(v / 10, 1), "label": lbl, "tone": tone}
    return {"value": v, "score10": round(v / 10, 1), "label": "Very easy", "tone": "green"}


def _humanize_sleep_feedback(code):
    """Garmin sleep feedback comes as an enum like NEGATIVE_LONG_BUT_DISCONTINUOUS."""
    if not code or not isinstance(code, str):
        return None
    for pre in ("POSITIVE_", "NEGATIVE_", "NEUTRAL_"):
        if code.startswith(pre):
            code = code[len(pre):]
            break
    txt = code.replace("_", " ").strip().lower()
    return txt[:1].upper() + txt[1:] if txt else None


def day_context(snapshot: dict, date_iso: str, feel_provider=None) -> dict:
    """Everything that happened around one calendar day, for the calendar drill-in:
    that morning's sleep + recovery (readiness / HRV), what the coach said the plan
    was, and the athlete's own post-run Feel / Perceived-Effort tags."""
    snapshot = snapshot or {}
    date = dt.date.fromisoformat(date_iso)
    cfg = resolve_active_race(load_config(), date)
    plan = load_plan()
    day = get_day(plan, date_iso)
    day = resolve_day(day, plan, cfg) if day else None

    by_date = lambda seq: next((x for x in (snapshot.get(seq) or []) if x.get("date") == date_iso), None)
    sleep = by_date("sleep")
    rd = by_date("readiness_trend")
    hrv = by_date("hrv")
    rhr = by_date("rhr_trend")
    bb = by_date("body_battery")
    baseline = snapshot.get("hrv_baseline")

    out = {"date": date_iso, "has_data": False}

    # ---- that morning's sleep ----
    if sleep and sleep.get("total_s"):
        out["sleep"] = {
            "hours": round(sleep["total_s"] / 3600.0, 1),
            "score": sleep.get("score"),
            "deep_min": round((sleep.get("deep_s") or 0) / 60),
            "rem_min": round((sleep.get("rem_s") or 0) / 60),
            "light_min": round((sleep.get("light_s") or 0) / 60),
            "awake_min": round((sleep.get("awake_s") or 0) / 60),
            "resting_hr": sleep.get("resting_hr"),
            "avg_hrv": sleep.get("avg_overnight_hrv"),
            "feedback": _humanize_sleep_feedback(sleep.get("feedback")),
        }
        out["has_data"] = True

    # ---- that morning's recovery ----
    rec = {}
    if rd and rd.get("score") is not None:
        rec["readiness"] = {"score": rd.get("score"), "level": rd.get("level"),
                            "recovery_pct": rd.get("recovery_pct"), "sleep_score": rd.get("sleep_score")}
    if hrv and hrv.get("last_night_avg") is not None:
        lo = hrv.get("balanced_low") or (baseline or {}).get("low")
        hi = hrv.get("balanced_upper") or (baseline or {}).get("upper")
        st = "in"
        if lo and hi:
            st = "below" if hrv["last_night_avg"] < lo else "above" if hrv["last_night_avg"] > hi else "in"
        rec["hrv"] = {"value": hrv["last_night_avg"], "status": hrv.get("status"),
                      "low": lo, "high": hi, "state": st}
    if rhr and rhr.get("rhr"):
        rec["rhr"] = rhr["rhr"]
    if rec:
        out["recovery"] = rec
        out["has_data"] = True

    # ---- what the coach said for that day ----
    if day is not None or rd or hrv or sleep:
        core = _verdict_core(rd, hrv, bb, rhr, sleep, baseline, day, yesterday=True)
        out["coach_said"] = {
            "verdict": core["verdict"], "headline": core["headline"],
            "recommendation": core["recommendation"],
            "session": _session_brief(day) if day else None,
        }
        out["has_data"] = True

    # ---- the athlete's own post-run tags (Feel + Perceived Effort) ----
    runs = [a for a in (snapshot.get("activities") or []) if a.get("date") == date_iso and a.get("id")]
    tags = []
    for r in runs:
        fr = feel_provider(r["id"]) if feel_provider else {}
        feel, rpe = _feel_label(fr.get("feel")), _rpe_label(fr.get("rpe"))
        if feel or rpe:
            tags.append({"name": r.get("name"), "feel": feel, "rpe": rpe})
    if tags:
        out["run_tags"] = tags
        out["has_data"] = True

    return out


def _next_day_outlook(snapshot: dict, today: dt.date, cfg: dict, plan: dict,
                      run_today: Optional[dict], day_today: Optional[dict]) -> Optional[dict]:
    """Forward look at tomorrow's session with tonight's recovery advice and
    conditional scenarios. Deliberately PROVISIONAL: tomorrow's readiness can't be
    scored until sleep is banked, so it never declares a definitive 'good to go'."""
    tmr = today + dt.timedelta(days=1)
    tmr_iso = tmr.isoformat()
    tday = get_day(plan, tmr_iso)
    tday = resolve_day(tday, plan, cfg) if tday else None
    if not tday:
        return None
    ttype = tday.get("type")
    ttitle = tday.get("title") or "tomorrow's session"

    # was today a real stimulus? (drives tonight's recovery emphasis)
    load = run_today.get("training_load") if run_today else None
    te = run_today.get("aerobic_te") if run_today else None
    hard_today = bool((day_today and day_today.get("type") in ("quality", "long", "race"))
                      or (te and te >= 3.5) or (load and load >= 130))
    if run_today:
        if hard_today:
            advice = (f"Today was a genuine stimulus{f' (load {round(load)})' if load else ''} — the adaptation "
                      f"banks tonight. Get a carb + protein refuel in soon, hydrate well, and protect 8 h+ of sleep. "
                      f"That sleep is exactly what sets tomorrow's readiness.")
        else:
            advice = ("Easy day in the bank — nothing special needed tonight beyond a normal meal and a good "
                      "night's sleep. Keep the easy discipline so tomorrow's session lands fresh.")
    else:
        advice = ("Rest day logged. The highest-value thing you can do for tomorrow is sleep — it's what lets the "
                  "next quality day be genuinely quality.")

    # conditional scenarios for tomorrow, keyed to the session type
    if ttype in ("quality", "long", "race"):
        scenarios = [
            {"tone": "green", "cond": "Readiness ≥ 75 and sleep ≥ 7.5 h",
             "action": f"Run {ttitle} as prescribed — commit to the full targets."},
            {"tone": "amber", "cond": "Readiness 50–74, or sleep 6–7.5 h",
             "action": "Keep the session but run the easy end of the pace band and drop a rep if HR drifts — quality over quota."},
            {"tone": "red", "cond": "Readiness < 50, or sleep < 6 h",
             "action": f"Swap to easy Z2 and slide {ttitle} to the next good day. A flat quality session costs more than it gives."},
        ]
        headline = f"Tomorrow is a {ttype} day — {ttitle}."
    elif ttype == "easy":
        scenarios = [
            {"tone": "green", "cond": "Feeling fresh / readiness green",
             "action": "Run the full easy distance, strictly Z2 — this is base-building, not a test."},
            {"tone": "amber", "cond": "Sleep 6–7.5 h or legs flat",
             "action": "Still run, but keep it short and conversational; walk breaks are fine."},
            {"tone": "red", "cond": "Readiness red or sleep < 6 h",
             "action": "Cut it right down or take it as an extra rest day — an easy run this tired adds fatigue without fitness."},
        ]
        headline = f"Tomorrow is an easy day — {ttitle}."
    else:  # rest / strength
        scenarios = [
            {"tone": "green", "cond": "However you sleep",
             "action": "Tomorrow is scheduled rest — take it. Sleep and mobility are the session."},
            {"tone": "amber", "cond": "If you're itching to move and wake up green",
             "action": "An optional easy shakeout or the strength work is fine — but it's a bonus, not required."},
        ]
        headline = f"Tomorrow is {('strength' if ttype == 'strength' else 'rest')} — {ttitle}."

    return {
        "date": tmr_iso,
        "provisional": True,
        "headline": headline,
        "session": _session_brief(tday),
        "recovery_advice": advice,
        "scenarios": scenarios,
        "note": ("This is provisional. Tomorrow's readiness can't be scored until you've slept, so the call is "
                 "confirmed in the morning once your sleep and HRV are in — not tonight."),
    }


def todays_verdict(snapshot: dict, today: Optional[dt.date] = None, stream_provider=None) -> dict:
    """If today's run is already logged, return a post-run analysis (mode='done');
    otherwise the morning readiness verdict (mode='pending')."""
    today = today or dt.date.today()
    today_iso = today.isoformat()
    cfg = resolve_active_race(load_config(), today)
    plan = load_plan()
    day = get_day(plan, today_iso)
    day = resolve_day(day, plan, cfg) if day else None

    run = _run_on_date(snapshot, today_iso)
    if run:
        cls = _classify_effort(run, day, cfg, snapshot, stream_provider)
        tips = cls["tips"] or [
            "Refuel with carbs + protein in the next 30–60 min and get a solid night's sleep "
            "to lock in the adaptation."]
        return {
            "date": today_iso,
            "mode": "done",
            "verdict": cls["verdict"],
            "headline": cls["headline"],
            "has_data": True,
            "session": _session_brief(day) if day else None,
            "next_day": _next_day_outlook(snapshot, today, cfg, plan, run, day),
            "completed": {
                "name": run.get("name"),
                "distance_km": run.get("distance_km"),
                "duration": fmt_secs(run.get("moving_s")),
                "avg_pace": pace_str(run.get("avg_pace_s_per_km")),
                "gap_pace": cls.get("gap_pace"),
                "avg_hr": run.get("avg_hr"),
                "max_hr": run.get("max_hr"),
                "power": round(run["avg_power"]) if run.get("avg_power") and run["avg_power"] >= 150 else None,
                "training_load": run.get("training_load"),
                "aerobic_te": run.get("aerobic_te"),
                "zone_pct": cls["zone_pct"],
                "hr_zone": cls["hr_zone"],
                "effort": cls.get("effort"),
                "blend": cls.get("blend"),
                "hit": cls["hit"],
                "notes": cls["notes"],
                "insights": _run_insights(run, day, cfg, snapshot, stream_provider),
                "race_impact": _race_impact(day, cls["kind"], cfg, snapshot),
                "tip": tips[0],
            },
        }

    v = morning_verdict(snapshot, today)
    v["mode"] = "pending"
    return v


# ---------------- aerobic endurance assessment ----------------
def aerobic_endurance(snapshot: dict, cfg: dict, today: Optional[dt.date] = None) -> Optional[dict]:
    """Most-recent read of aerobic durability from training: the longest steady
    aerobic effort held (time + pace + HR), plus an efficiency trend (is easy pace
    costing less HR over time?)."""
    today = today or dt.date.today()
    runs = [r for r in (snapshot or {}).get("activities", [])
            if r.get("avg_pace_s_per_km") and r.get("moving_s")]
    if not runs:
        return None

    def rd(r):
        return dt.date.fromisoformat(r["date"])

    recent = [r for r in runs if (today - rd(r)).days < 42] or runs
    longest = max(recent, key=lambda r: r.get("moving_s") or 0)

    # aerobic efficiency (speed ÷ HR) on easy runs: recent vs a month back
    easy_lo = cfg["paces"].get("easy", {}).get("sec_low", 295)
    ez = [r for r in runs if r.get("avg_hr") and (r.get("avg_pace_s_per_km") or 0) >= easy_lo - 12]

    def ef(r):
        return (1000.0 / r["avg_pace_s_per_km"]) / r["avg_hr"]

    rec = [r for r in ez if (today - rd(r)).days < 30]
    old = [r for r in ez if 30 <= (today - rd(r)).days < 90]
    trend = trend_txt = None
    if len(rec) >= 2 and len(old) >= 2:
        rr = sum(ef(r) for r in rec) / len(rec)
        oo = sum(ef(r) for r in old) / len(old)
        d = round(100 * (rr - oo) / oo, 1)
        if d >= 2:
            trend, trend_txt = "up", f"Aerobic efficiency is up ~{d}% vs a month ago — you're holding easy pace at a lower heart rate. The base is building."
        elif d <= -2:
            trend, trend_txt = "down", f"Aerobic efficiency is down ~{abs(d)}% — easy pace is costing more HR lately (fatigue, heat, or a dip in base). Bank easy volume."
        else:
            trend, trend_txt = "flat", "Aerobic efficiency is steady — holding easy pace at the same heart rate."

    hz = hr_zone_for(longest.get("avg_hr"), cfg)
    hr_r = round(longest["avg_hr"]) if longest.get("avg_hr") else None
    return {
        "longest": {
            "duration": fmt_secs(longest.get("moving_s")),
            "km": longest.get("distance_km"),
            "pace": pace_str(longest.get("avg_pace_s_per_km")),
            "avg_hr": hr_r,
            "hr_zone": hz, "date": longest.get("date"),
        },
        "trend": trend, "trend_text": trend_txt,
        "read": (f"Your longest recent aerobic effort held {pace_str(longest.get('avg_pace_s_per_km'))}/km "
                 f"for {fmt_secs(longest.get('moving_s'))} ({longest.get('distance_km')} km) at HR "
                 f"{hr_r}. That's your demonstrated durability — push the ceiling with the "
                 f"progressive long runs, where the Wk6 22 km is the key test. Open any run for its in-workout "
                 f"decoupling read."),
    }


# ---------------- progress vs plan ----------------
def progress(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    cfg = resolve_active_race(load_config(), today)
    plan = load_plan()
    enriched = enrich_plan(plan, cfg)
    wtr = weeks_to_race(today)

    weekly_actual = {w["week_start"]: w for w in (snapshot or {}).get("weekly_volume", [])}

    weeks = []
    quality_done = 0
    quality_planned = 0
    for wk in enriched["weeks"]:
        ws = wk["start_date"]
        actual = weekly_actual.get(ws)
        actual_km = actual["km"] if actual else 0.0
        wk_start = dt.date.fromisoformat(ws)
        wk_end = wk_start + dt.timedelta(days=6)
        is_past = wk_end < today
        is_current = wk_start <= today <= wk_end
        # A quality session counts as done when a run is actually logged on the
        # prescribed quality day's date — no reliance on (mis-set) HR zones.
        done_q = sum(1 for d in wk["days"]
                     if d.get("type") == "quality" and _run_on_date(snapshot, d.get("date")))
        if is_past or is_current:
            quality_planned += wk["quality_sessions"]
            quality_done += min(done_q, wk["quality_sessions"])
        # prescribed workouts (run days) vs how many days that week actually had a
        # run — counted by distinct run-day, not exact prescribed date, so shifting
        # a session to another day still counts. Feeds the lenient "good week" rule.
        workouts_planned = sum(1 for d in wk["days"] if d.get("type") in _RUN_TYPES)
        run_days = {a.get("date") for a in (snapshot or {}).get("activities", [])
                    if a.get("date") and ws <= a["date"] <= wk_end.isoformat()}
        workouts_done = min(len(run_days), workouts_planned)
        # ---- anchor-based good-week signals -----------------------------------
        # A "good week" is one where the WORK that matters landed, even if a mid-
        # week easy day slipped: the prescribed quality session got run, the long
        # run was substantially completed, and enough aerobic base sat behind it.
        # No quality prescribed (recovery / break weeks) => auto-hit on that axis.
        qual_days = [d for d in wk["days"] if d.get("type") == "quality"]
        quality_hit = (not qual_days) or (done_q >= min(len(qual_days), 1))
        long_days = [d for d in wk["days"] if d.get("type") in ("long", "race")]
        long_hit = True
        long_ratio = None
        if long_days:
            longest_planned = max((d.get("distance_km") or 0) for d in long_days)
            week_acts = [a for a in (snapshot or {}).get("activities", [])
                         if a.get("date") and ws <= a["date"] <= wk_end.isoformat()
                         and (a.get("distance_km") or 0) >= 8]
            longest_done = max((a.get("distance_km") or 0) for a in week_acts) if week_acts else 0
            long_ratio = round(longest_done / max(1, longest_planned), 2)
            # 55% floor -- a genuinely long run (even if shortened) still counts.
            long_hit = long_ratio >= 0.55
        vol_ratio = round((actual_km / max(1, wk["target_km"])), 2)
        vol_hit = vol_ratio >= 0.70
        good_week = bool(quality_hit and long_hit and vol_hit) if (is_past or is_current) else False
        weeks.append({
            "week": wk["week"],
            "start_date": ws,
            "phase": wk["phase"],
            "target_km": wk["target_km"],
            "planned_km": wk["planned_km"],
            "actual_km": round(actual_km, 1),
            "hmp_km": wk["hmp_km"],
            "quality_planned": wk["quality_sessions"],
            "quality_done_est": done_q,
            "workouts_planned": workouts_planned,
            "workouts_done": workouts_done,
            "quality_hit": bool(quality_hit),
            "long_hit": bool(long_hit),
            "long_ratio": long_ratio,
            "vol_hit": bool(vol_hit),
            "vol_ratio": vol_ratio,
            "good_week": good_week,
            "state": "past" if is_past else ("current" if is_current else "future"),
        })

    # fitness trajectory + projected finish — use the recalibrated race_targets
    # projection so the Coach read matches the Fitness VDOT projection (both anchor
    # on VO2max + demonstrated racing, not Garmin's suppressed predictor).
    import fitness as _fit
    training = (snapshot or {}).get("training") or {}
    dp = derived_paces(cfg, snapshot)
    projected = None
    on_track = "grey"
    projection_note = ""
    try:
        _rt = _fit.race_targets(snapshot, cfg, today)
    except Exception:
        _rt = None
    _pj = (_rt or {}).get("projection")
    if _pj:
        projected = _pj.get("race_time")
        hit = _pj.get("hits_goal")
        on_track = "green" if hit == "A" else "amber" if hit in ("B", "C") else "red"
        projection_note = _pj.get("note") or ""

    total_planned = sum(w["planned_km"] for w in weeks)
    total_actual = round(sum(w["actual_km"] for w in weeks if w["state"] in ("past", "current")), 1)

    return {
        "weeks_to_race": wtr,
        "weeks": weeks,
        "quality_completion": {
            "done": quality_done, "planned": quality_planned,
            "pct": round(100 * quality_done / quality_planned) if quality_planned else None,
        },
        "volume": {"planned_to_date": round(sum(
            w["planned_km"] for w in weeks if w["state"] in ("past", "current")), 1),
            "actual_to_date": total_actual, "season_planned": round(total_planned, 1)},
        "fitness": {
            "vo2max": training.get("vo2max"),
            "vo2max_trend": (snapshot or {}).get("vo2max_trend", []),
            "training_status": training.get("status_feedback"),
            "acwr": training.get("acwr"),
            "acwr_status": training.get("acwr_status"),
        },
        "projected_finish": projected,
        "on_track": on_track,
        "projection_note": projection_note,
        "paces": dp,
        "endurance": aerobic_endurance(snapshot, cfg, today),
    }


# ---------------- chat ----------------
def chat(message: str, snapshot: dict, today: Optional[dt.date] = None) -> dict:
    """Rule-based coach chat. Routes the question to the relevant computed read
    so answers are grounded in the athlete's real data and plan."""
    today = today or dt.date.today()
    m = (message or "").lower()
    cfg = resolve_active_race(load_config(), today)

    def reply(text):
        return {"reply": text}

    # today's plan / what should I do
    if any(k in m for k in ["today", "right now", "this morning", "should i run", "what's on", "whats on"]):
        v = todays_verdict(snapshot, today)
        if v.get("mode") == "done":
            c = v["completed"]
            parts = [f"**{v['headline']}.** You've already done today's session."]
            line = f"{c['distance_km']} km @ {c['avg_pace']}/km"
            if c.get("avg_hr"):
                line += f", avg HR {c['avg_hr']}"
            parts.append(line + ".")
            if c.get("notes"):
                parts.append(c["notes"][0])
            if c.get("race_impact"):
                parts.append(c["race_impact"])
            return reply("\n\n".join(parts))
        s = v.get("session")
        parts = [f"**{v['headline']}** ({v['verdict'].upper()}). {v['recommendation']}"]
        if s:
            line = f"Prescribed: {s['title']}"
            if s.get("distance_km"):
                line += f" — {s['distance_km']} km"
            if s.get("pace"):
                line += f" @ {s['pace']}/km"
            parts.append(line)
        return reply("\n\n".join(parts))

    # sleep-specific questions → dedicated sleep coach
    if any(k in m for k in ["sleep", "slept", "bedtime", "rem", "deep sleep", "insomnia", "nap"]):
        import sleep as _sleeplib
        sl = _sleeplib.analyze_sleep(snapshot, today)
        if not sl.get("has_data"):
            return reply(sl.get("message", "No sleep data has synced yet."))
        r = sl["rolling"]
        parts = [f"**{sl['headline']}.**"]
        if r.get("avg_duration_h") and r.get("avg_need_h"):
            line = f"Last {r['nights']} nights: {r['avg_duration_h']} h/night vs your ~{r['avg_need_h']} h need"
            if r.get("regularity") and r["regularity"] != "—":
                line += f", timing {r['regularity'].lower()}"
            parts.append(line + ".")
        top = sl.get("recommendations") or []
        if top:
            parts.append(f"Top focus — **{top[0]['title']}**: {top[0]['detail']}")
        if sl.get("training_note"):
            parts.append(sl["training_note"])
        return reply("\n\n".join(parts))

    # readiness / recovery / hrv
    if any(k in m for k in ["readiness", "recover", "hrv", "tired", "rested", "fatigue"]):
        v = morning_verdict(snapshot, today)
        body = " ".join(v["signals"]) if v["signals"] else "No recovery data has synced recently."
        return reply(f"{v['headline']} ({v['verdict'].upper()}). {body}\n\n{v['recommendation']}")

    # on track / goal / prediction
    if any(k in m for k in ["on track", "goal", "predict", "finish", "realistic", "sub"]):
        p = progress(snapshot, today)
        note = p.get("projection_note") or "Not enough data to project yet — sync a recent run."
        return reply(note)

    # pace questions
    if "pace" in m or "how fast" in m:
        paces = cfg["paces"]
        lines = [f"- {v['label']}: {v.get('target') or (v.get('low') + '–' + v.get('high'))}/km"
                 for v in paces.values()]
        return reply("Your target paces:\n" + "\n".join(lines))

    # last run / how did I do
    if any(k in m for k in ["last run", "how did i", "my run", "analyze", "workout go"]):
        a = analyze_last_run(snapshot)
        if not a:
            return reply("No recent run found to analyze. Sync your watch and try again.")
        return reply(" ".join(a["notes"]) + "\n\nTip: " + a["tip"])

    # volume / mileage
    if any(k in m for k in ["volume", "mileage", "how many km", "km this week", "distance"]):
        p = progress(snapshot, today)
        cur = next((w for w in p["weeks"] if w["state"] == "current"), None)
        if cur:
            return reply(f"Week {cur['week']}: {cur['actual_km']} km logged vs a {cur['target_km']} km target "
                         f"({cur['planned_km']} km planned across the week). Season to date: "
                         f"{p['volume']['actual_to_date']} km actual vs {p['volume']['planned_to_date']} km planned.")
        return reply(f"Season to date: {p['volume']['actual_to_date']} km actual vs "
                     f"{p['volume']['planned_to_date']} km planned.")

    # strength
    if "strength" in m or "lift" in m or "gym" in m:
        plan = load_plan()
        wk = find_week_for_date(plan, today.isoformat())
        if wk:
            for d in wk["days"]:
                if d.get("strength"):
                    st = d["strength"]
                    return reply(f"This week's strength ({st['phase']}): {st['focus']}.\n" +
                                 "\n".join(f"- {e}" for e in st["exercises"]) +
                                 f"\n\n{st['note']} {plan.get('core_stability','')}")
        return reply("Strength is one running-specific session per week, default Wednesday after intervals. "
                     "Check the calendar for this week's exact prescription.")

    # fallback
    _gt = (cfg.get("goals", {}) or {}).get(cfg.get("primary_goal", "A"), {}).get("time", "your goal")
    return reply(f"I'm your coach — ask me about today's session, your recovery/readiness, whether {_gt} is on "
                 "track, your paces, your last run, weekly volume, or strength training.")
