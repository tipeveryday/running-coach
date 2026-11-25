"""Sleep coach: turns Garmin's nightly sleep data into an expert-level read.

Beyond duration + score, it evaluates the things a world-class sleep coach cares
about for an endurance athlete:
  - duration vs the athlete's *personalized* sleep need (not a generic 8 h)
  - accumulated sleep debt over the recent window
  - sleep efficiency and fragmentation (awakenings, restlessness)
  - sleep architecture: deep / REM / light % vs healthy target ranges
  - regularity — consistency of bed/wake timing, the strongest predictor of
    recovery and health (Sleep Regularity)
  - overnight physiological recovery: HRV, resting HR, respiration, SpO2,
    stress, Body Battery recharge
  - training linkage: sleep ahead of hard sessions

Everything degrades gracefully when only a few nights have synced.
"""
from __future__ import annotations
import datetime as dt
import statistics
from typing import Any, Optional

from planlib import load_config, load_plan, get_day

# Healthy adult sleep-architecture targets (% of asleep time). Deep drives
# physical repair / growth-hormone release; REM drives cognitive + motor-memory
# consolidation — both matter to a runner absorbing training.
DEEP_TARGET = (13, 23)
REM_TARGET = (20, 28)
EFF_GOOD = 85.0        # sleep efficiency (%): >=85 healthy, >=90 excellent
BEDTIME_STABLE = 45    # bedtime stdev (min): <=45 tight, <=75 ok, else erratic


def _local(ms) -> Optional[dt.datetime]:
    """Local-offset-baked epoch-ms → wall-clock datetime (read via UTC)."""
    if not ms:
        return None
    return dt.datetime.utcfromtimestamp(ms / 1000.0)


def _mins_after_midnight(d: Optional[dt.datetime]) -> Optional[int]:
    return None if d is None else d.hour * 60 + d.minute


def _clock(minute: Optional[int]) -> str:
    if minute is None:
        return "–"
    minute %= 1440
    h, m = divmod(minute, 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}"


def _pct(part, whole):
    return round(100 * part / whole, 1) if part is not None and whole else None


def _norm_night(n: dict) -> dict:
    total = n.get("total_s") or 0
    awake = n.get("awake_s") or 0
    in_bed = total + awake
    need = n.get("need_min")
    start = _local(n.get("start_local_ms"))
    end = _local(n.get("end_local_ms"))
    bed_min = _mins_after_midnight(start)
    wake_min = _mins_after_midnight(end)
    # bedtime plotted on a "night" axis: after-midnight times wrap to >24 so an
    # 00:30 bedtime sits just after a 23:00 one instead of a full day away.
    bed_plot = None if bed_min is None else (bed_min / 60.0 + (24 if bed_min < 720 else 0))
    wake_plot = None if wake_min is None else wake_min / 60.0
    mid = None
    if bed_plot is not None and wake_plot is not None:
        mid = ((bed_plot + (wake_plot + 24)) / 2.0) % 24
    return {
        "date": n.get("date"),
        "duration_h": round(total / 3600.0, 2),
        "deep_h": round((n.get("deep_s") or 0) / 3600.0, 2),
        "light_h": round((n.get("light_s") or 0) / 3600.0, 2),
        "rem_h": round((n.get("rem_s") or 0) / 3600.0, 2),
        "awake_h": round(awake / 3600.0, 2),
        "need_h": round(need / 60.0, 2) if need else None,
        "debt_min": round(need - total / 60.0) if need else None,
        "efficiency": _pct(total, in_bed),
        "deep_pct": _pct(n.get("deep_s"), total),
        "rem_pct": _pct(n.get("rem_s"), total),
        "light_pct": _pct(n.get("light_s"), total),
        "awake_count": n.get("awake_count"),
        "restless_moments": n.get("restless_moments"),
        "score": n.get("score"),
        "score_q": n.get("score_q"),
        "bedtime_min": bed_min,
        "waketime_min": wake_min,
        "bedtime_str": _clock(bed_min),
        "waketime_str": _clock(wake_min),
        "bed_plot": round(bed_plot, 2) if bed_plot is not None else None,
        "wake_plot": round(wake_plot, 2) if wake_plot is not None else None,
        "midsleep": round(mid, 2) if mid is not None else None,
        "hrv": n.get("avg_overnight_hrv"),
        "rhr": n.get("resting_hr") or n.get("avg_hr"),
        "respiration": n.get("avg_respiration"),
        "stress": n.get("avg_stress"),
        "spo2_low": n.get("lowest_spo2"),
        "spo2_avg": n.get("avg_spo2"),
        "bb_change": n.get("bb_change"),
        "feedback": n.get("feedback"),
        "insight": n.get("insight"),
    }


# ---- Garmin coded feedback → plain coaching language ----
def _humanize(code: Optional[str]) -> Optional[str]:
    if not code or code in ("NONE", "UNKNOWN"):
        return None
    known = {
        "NEGATIVE_LONG_BUT_RESTLESS": "Good duration, but the night was restless — the sleep was there, the quality wasn't.",
        "NEGATIVE_NOT_ENOUGH_REM": "Light on REM — the stage that consolidates skills and manages stress.",
        "NEGATIVE_NOT_ENOUGH_DEEP": "Light on deep sleep — the stage that does the physical repair.",
        "POSITIVE_LONG_AND_RESTFUL": "Long and restful — an ideal recovery night.",
        "MOD_EXERCISE_NEG_FAIR_OR_POOR_SLEEP_INACTIVITY":
            "Moderate training on fair/poor sleep — recovery will lag if this repeats; bank an earlier night.",
    }
    if code in known:
        return known[code]
    # fallback: turn CODE_LIKE_THIS into a readable phrase
    return code.replace("_", " ").capitalize() + "."


def _qual(q: Optional[str]) -> str:
    return {"EXCELLENT": "excellent", "GOOD": "good", "FAIR": "fair",
            "POOR": "poor"}.get(q, (q or "").lower())


def _percentile(vals: list, p: float):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _recovery_vitals(snapshot: dict, nights: list, bl: Optional[dict] = None) -> dict:
    """Read overnight HRV and resting HR against the athlete's *own* history.
    Prefers the long-history (2-year) baselines when available, falling back to
    the ~6-week snapshot window."""
    out: dict = {}
    bl = bl or {}

    # ---- HRV: Garmin's personalized balanced band is the ideal ----
    hrv_bl = bl.get("hrv") or {}
    hrv_hist = [h for h in (snapshot or {}).get("hrv", []) if h.get("last_night_avg") is not None]
    sleep_hrv = [n["hrv"] for n in nights if n.get("hrv") is not None]
    baseline = (snapshot or {}).get("hrv_baseline") or {}
    if hrv_hist or sleep_hrv or hrv_bl:
        latest_h = hrv_hist[-1] if hrv_hist else {}
        lo = hrv_bl.get("balanced_low") or latest_h.get("balanced_low") or baseline.get("low")
        hi = hrv_bl.get("balanced_high") or latest_h.get("balanced_upper") or baseline.get("upper")
        last = (sleep_hrv[-1] if sleep_hrv else None) or latest_h.get("last_night_avg")
        recent = sleep_hrv[-7:] or [h["last_night_avg"] for h in hrv_hist[-7:]]
        avg = hrv_bl.get("recent_avg") or (round(sum(recent) / len(recent), 1) if recent else None)
        best = hrv_bl.get("all_time_best") or (max(sleep_hrv or [h["last_night_avg"] for h in hrv_hist]) if (sleep_hrv or hrv_hist) else None)
        best_txt = f" Your 2-year best is ~{best} ms." if (bl.get("hrv") and best) else ""
        state, read = "in", ""
        if last is not None and lo and hi:
            if last < lo:
                state = "below"
                read = (f"{last} ms is below your balanced range ({lo}–{hi}) — your clearest early sign of "
                        f"accumulated fatigue or an oncoming stressor. If it stays low, ease off.{best_txt}")
            elif last > hi:
                state = "above"
                read = (f"{last} ms is above your usual band ({lo}–{hi}) — deeply parasympathetic, a well-recovered "
                        f"night. A good day to absorb a hard session.{best_txt}")
            else:
                pos = "upper" if (last >= (lo + hi) / 2) else "lower"
                read = (f"{last} ms sits in the {pos} of your balanced range ({lo}–{hi}) — normal and recovered. "
                        f"Higher within the band is better; a drop below {lo} is your fatigue flag.{best_txt}")
        out["hrv"] = {"last": last, "unit": "ms", "ideal_low": lo, "ideal_high": hi,
                      "recent_avg": avg, "best": best, "state": state, "read": read,
                      "source": "2-year" if bl.get("hrv") else "recent"}

    # ---- Resting HR: personal rested floor + baseline ----
    rhr_bl = bl.get("rhr") or {}
    rhr_hist = [r["rhr"] for r in (snapshot or {}).get("rhr_trend", []) if r.get("rhr")]
    sleep_rhr = [n["rhr"] for n in nights if n.get("rhr") is not None]
    if rhr_bl or rhr_hist or sleep_rhr:
        if rhr_bl:
            rested = rhr_bl.get("recent_rested")
            median = rhr_bl.get("recent_baseline")
            elevated = rhr_bl.get("recent_elevated")
            best = rhr_bl.get("all_time_low")
            span = (f" Your 2-year range is {best}–{rhr_bl.get('all_time_high')} bpm."
                    if best and rhr_bl.get("all_time_high") else "")
        else:
            recent = rhr_hist[-30:] if rhr_hist else sleep_rhr
            rested = round(_percentile(recent, 0.2))
            median = round(_percentile(recent, 0.5))
            elevated = round(_percentile(recent, 0.8))
            best = round(min(recent)) if recent else None
            span = ""
        last = (sleep_rhr[-1] if sleep_rhr else None) or (rhr_hist[-1] if rhr_hist else None)
        recent7 = (sleep_rhr[-7:] or rhr_hist[-7:])
        avg = round(sum(recent7) / len(recent7), 1) if recent7 else None
        state, read = "normal", ""
        if last is not None and rested is not None:
            if last <= rested + 2:
                state = "excellent"
                read = (f"{last} bpm is right at your rested floor (~{rested}) — a strong recovery signal; the body "
                        f"has fully stood down.{span}")
            elif last <= (median or rested) + 3:
                state = "normal"
                read = f"{last} bpm is around your normal baseline (~{median}). Nothing to flag.{span}"
            else:
                state = "elevated"
                read = (f"{last} bpm is elevated for you (baseline ~{median}, rested ~{rested}). An overnight resting "
                        f"HR this high usually means incomplete recovery — hard training, short sleep, alcohol, a late "
                        f"meal, heat, or an oncoming illness.{span}")
        out["rhr"] = {"last": last, "unit": "bpm", "rested": rested, "baseline": median,
                      "elevated": elevated, "best": best, "recent_avg": avg, "state": state, "read": read,
                      "source": "2-year" if rhr_bl else "recent"}

    # ---- combined overreaching read ----
    h, r = out.get("hrv"), out.get("rhr")
    if h and r and h.get("state") == "below" and r.get("state") == "elevated":
        out["combined"] = ("HRV down *and* resting HR up together is the classic overreaching signature — take "
                           "today easy and prioritize sleep; re-check tomorrow before any hard work.")
    elif h and r and h.get("state") in ("in", "above") and r.get("state") in ("excellent", "normal"):
        out["combined"] = ("HRV and resting HR agree — your autonomic recovery is in a good place, so your body "
                           "can take on the training you've got planned.")
    return out


def _rolling_baseline(nights: list, idx: int, key: str, window: int = 30) -> Optional[float]:
    """Median of `key` across the up-to-`window` nights preceding index `idx`.
    Used to decide "is this night's stress genuinely higher than my normal"
    rather than comparing against a fixed threshold that ignores individual
    baseline drift."""
    prior = [n.get(key) for n in nights[max(0, idx - window): idx] if n.get(key) is not None]
    if len(prior) < 5:
        return None
    prior.sort()
    return prior[len(prior) // 2]


def alcohol_flags(nights: list) -> list[dict]:
    """Flag nights whose signature suggests evening alcohol: an elevated overnight
    stress + suppressed HRV + elevated respiration / RHR combined with a low
    body-battery restore is the classic pattern.

    Composite of 5 signals, each compared to that athlete's rolling 30-night
    median (not a fixed threshold - this respects individual baseline):
      - overnight stress ≥ baseline + 10 pts
      - HRV ≤ baseline × 0.85 (>= 15% suppressed)
      - respiration ≥ baseline + 1.5 bpm
      - RHR ≥ baseline + 4 bpm
      - Body Battery restore < 40 (regardless of baseline; absolute floor)

    Nights that clear 3+ signals are 'likely'; 2 signals is 'possible'.
    Weekend nights (Fri/Sat/Sun morning) get called out separately.

    Returns [{date, level, reasons, is_weekend, weekday}]. Only nights with a
    non-null level are included."""
    out: list[dict] = []
    if not nights:
        return out
    for i, n in enumerate(nights):
        signals = []
        # 1) stress
        s_now = n.get("stress")
        s_base = _rolling_baseline(nights, i, "stress")
        if s_now is not None and s_base is not None and s_now >= s_base + 10:
            signals.append({"kind": "stress",
                            "detail": f"overnight stress {round(s_now)} vs baseline {round(s_base)}"})
        # 2) HRV
        h_now = n.get("hrv")
        h_base = _rolling_baseline(nights, i, "hrv")
        if h_now is not None and h_base is not None and h_now <= h_base * 0.85:
            signals.append({"kind": "hrv",
                            "detail": f"HRV {round(h_now)} ms vs baseline {round(h_base)} (~{round(100*(1-h_now/h_base))}% suppressed)"})
        # 3) respiration
        r_now = n.get("respiration")
        r_base = _rolling_baseline(nights, i, "respiration")
        if r_now is not None and r_base is not None and r_now >= r_base + 1.5:
            signals.append({"kind": "respiration",
                            "detail": f"respiration {r_now:.1f} vs baseline {r_base:.1f} bpm"})
        # 4) RHR
        rhr_now = n.get("rhr")
        rhr_base = _rolling_baseline(nights, i, "rhr")
        if rhr_now is not None and rhr_base is not None and rhr_now >= rhr_base + 4:
            signals.append({"kind": "rhr",
                            "detail": f"resting HR {round(rhr_now)} vs baseline {round(rhr_base)} bpm"})
        # 5) Body Battery restore
        bb_now = n.get("bb_change")
        if bb_now is not None and bb_now < 40:
            signals.append({"kind": "bb",
                            "detail": f"body battery restored only +{round(bb_now)}"})

        if len(signals) < 2:
            continue
        level = "likely" if len(signals) >= 3 else "possible"
        # weekday of the MORNING you woke up. Alcohol usually happens the evening
        # before, so Fri/Sat/Sun mornings map to Thu/Fri/Sat evenings.
        weekday = ""
        is_weekend = False
        try:
            dwo = dt.date.fromisoformat(n["date"])
            weekday = dwo.strftime("%a")
            # Fri/Sat/Sun mornings capture Thu/Fri/Sat evenings (Thu often social)
            is_weekend = dwo.weekday() in (4, 5, 6, 0)  # Fri, Sat, Sun, Mon (covers weekend nights)
        except Exception:
            pass
        out.append({
            "date": n["date"],
            "level": level,
            "signals": signals,
            "is_weekend": is_weekend,
            "weekday": weekday,
        })
    return out


def analyze_sleep(snapshot: dict, today: Optional[dt.date] = None) -> dict:
    today = today or dt.date.today()
    cfg = load_config()
    raw = [s for s in (snapshot or {}).get("sleep", []) if s.get("total_s")]
    if not raw:
        return {"has_data": False,
                "message": "No sleep data has synced yet. Wear the watch overnight and refresh "
                           "for a full sleep-coach read."}

    nights = [_norm_night(n) for n in raw]
    nights.sort(key=lambda x: x["date"] or "")
    latest = nights[-1]
    window = nights[-7:]                       # last up-to-7 nights

    # ---- rolling metrics ----
    def _avg(key):
        vals = [x[key] for x in window if x.get(key) is not None]
        return round(sum(vals) / len(vals), 1) if vals else None

    avg_dur = _avg("duration_h")
    avg_need = _avg("need_h")
    avg_eff = _avg("efficiency")
    avg_deep = _avg("deep_pct")
    avg_rem = _avg("rem_pct")
    debt = sum(max(0, x["debt_min"]) for x in window if x.get("debt_min") is not None)

    beds = [x["bed_plot"] for x in window if x.get("bed_plot") is not None]
    bed_sd = round(statistics.pstdev(beds) * 60) if len(beds) >= 2 else None  # minutes
    if bed_sd is None:
        reg_label, reg_state = "—", "grey"
    elif bed_sd <= BEDTIME_STABLE:
        reg_label, reg_state = "Consistent", "green"
    elif bed_sd <= 75:
        reg_label, reg_state = "Somewhat variable", "amber"
    else:
        reg_label, reg_state = "Erratic", "red"

    # ---- per-dimension scoring for the overall grade ----
    pts = []
    if avg_dur and avg_need:
        deficit = avg_need - avg_dur
        pts.append(85 if deficit <= 0.25 else 62 if deficit <= 0.75 else 40)
    if avg_eff is not None:
        pts.append(85 if avg_eff >= 90 else 68 if avg_eff >= EFF_GOOD else 45)
    if avg_deep is not None:
        pts.append(80 if avg_deep >= DEEP_TARGET[0] else 50)
    if avg_rem is not None:
        pts.append(80 if avg_rem >= REM_TARGET[0] else 55)
    if bed_sd is not None:
        pts.append(85 if reg_state == "green" else 60 if reg_state == "amber" else 38)
    avg_score = sum(x["score"] for x in window if x.get("score") is not None)
    scored = [x for x in window if x.get("score") is not None]
    if scored:
        pts.append(sum(x["score"] for x in scored) / len(scored))

    grade_val = sum(pts) / len(pts) if pts else None
    if grade_val is None:
        grade, headline = "grey", "Not enough data for a full read"
    elif grade_val >= 72:
        grade, headline = "green", "Sleep is supporting your training"
    elif grade_val >= 55:
        grade, headline = "amber", "Sleep is okay, with clear room to gain"
    else:
        grade, headline = "red", "Sleep is undercutting your recovery"

    import cache
    vitals = _recovery_vitals(snapshot, nights, cache.load_baselines())
    recs = _recommendations(latest, window, avg_dur, avg_need, avg_eff, avg_deep,
                            avg_rem, bed_sd, reg_state, debt)
    # a fatigue flag from the vitals is high-priority — put it first
    hv, rv = vitals.get("hrv"), vitals.get("rhr")
    if (hv and hv.get("state") == "below") or (rv and rv.get("state") == "elevated"):
        recs.insert(0, {
            "title": "Recovery flag — back off and let it rebound",
            "detail": vitals.get("combined") or (hv or rv or {}).get("read")
                      or "Overnight HRV/resting-HR are off your baseline — favour easy training and more sleep today.",
        })
        recs = recs[:4]
    training_note = _training_linkage(snapshot, latest, today, cfg)

    return {
        "has_data": True,
        "grade": grade,
        "headline": headline,
        "insight": _humanize(latest.get("insight")) or _humanize(latest.get("feedback")),
        "recommendations": recs,
        "training_note": training_note,
        "vitals": vitals,
        "latest": latest,
        "nights": nights,
        "alcohol_flags": alcohol_flags(nights),
        "targets": {"deep": DEEP_TARGET, "rem": REM_TARGET, "efficiency": EFF_GOOD},
        "rolling": {
            "nights": len(window),
            "avg_duration_h": avg_dur,
            "avg_need_h": avg_need,
            "debt_min": round(debt),
            "avg_efficiency": avg_eff,
            "avg_deep_pct": avg_deep,
            "avg_rem_pct": avg_rem,
            "bedtime_sd_min": bed_sd,
            "regularity": reg_label,
            "regularity_state": reg_state,
        },
    }


def _recommendations(latest, window, avg_dur, avg_need, avg_eff, avg_deep,
                     avg_rem, bed_sd, reg_state, debt) -> list[dict]:
    """Prioritized, specific, science-grounded actions. Most impactful first."""
    recs: list[dict] = []

    # 1) Duration vs personalized need + debt — the biggest lever.
    if avg_dur and avg_need and (avg_need - avg_dur) > 0.4:
        gap_min = round((avg_need - avg_dur) * 60)
        extra = f" You're carrying ~{round(debt/60,1)} h of sleep debt over the last {len(window)} nights." if debt >= 60 else ""
        recs.append({
            "title": "Close the nightly gap — go to bed earlier",
            "detail": f"You're averaging {avg_dur:.1f} h against your personalized need of ~{avg_need:.1f} h "
                      f"(a {gap_min}-min shortfall).{extra} Move lights-out earlier in 15-min steps rather than "
                      f"sleeping in — a stable wake time protects your body clock. Even +30 min/night compounds "
                      f"fast into better adaptation.",
        })

    # 2) Regularity — the strongest predictor of recovery.
    if bed_sd is not None and reg_state != "green":
        recs.append({
            "title": "Anchor a consistent sleep–wake window",
            "detail": f"Your bedtime varied by about ±{bed_sd} min across the week. Timing regularity predicts "
                      f"recovery and health more reliably than any single night's duration. Pick a fixed lights-out "
                      f"and wake time (±30 min), weekends included, and let morning light lock the rhythm in.",
        })

    # 3) Deep sleep — physical repair.
    if avg_deep is not None and avg_deep < DEEP_TARGET[0]:
        recs.append({
            "title": "Protect deep sleep (physical repair)",
            "detail": f"Deep sleep averaged {avg_deep:.0f}% (healthy ≈{DEEP_TARGET[0]}–{DEEP_TARGET[1]}%). Deep sleep is "
                      f"when tissue repair and growth-hormone release peak — exactly what a training body needs. "
                      f"Biggest levers: no alcohol near bedtime, a cool (~18 °C) dark room, finish hard sessions and "
                      f"large meals ≥3 h before bed, and keep timing consistent.",
        })

    # 4) REM — cognitive + motor learning, stress regulation.
    if avg_rem is not None and avg_rem < REM_TARGET[0]:
        recs.append({
            "title": "Build back REM",
            "detail": f"REM averaged {avg_rem:.0f}% (healthy ≈{REM_TARGET[0]}–{REM_TARGET[1]}%). REM concentrates in the "
                      f"back half of the night, so short or chopped sleep cuts it first. Alcohol strongly suppresses it. "
                      f"A consistent wake time and full duration are the fix.",
        })

    # 5) Fragmentation / efficiency.
    frag = (avg_eff is not None and avg_eff < EFF_GOOD) or (latest.get("awake_count") or 0) >= 3 \
        or (latest.get("restless_moments") or 0) >= 40
    if frag:
        eff_txt = f"Efficiency was {avg_eff:.0f}% (aim ≥{int(EFF_GOOD)}%). " if avg_eff is not None else ""
        recs.append({
            "title": "Sleep more solidly (reduce awakenings)",
            "detail": f"{eff_txt}Fragmented nights blunt recovery even when total time looks fine. Cut caffeine after "
                      f"~2 pm and alcohol in the evening, stop fluids ~90 min before bed, keep the room cool and dark, "
                      f"and give yourself a 30-min screens-off wind-down.",
        })

    # 6) Overnight stress / incomplete parasympathetic recovery.
    if (latest.get("stress") is not None and latest["stress"] > 25) or \
       (latest.get("bb_change") is not None and latest["bb_change"] < 40):
        recs.append({
            "title": "Wind down for deeper recovery",
            "detail": f"Overnight stress sat high / Body Battery recharged only +{latest.get('bb_change','?')} — signs the "
                      f"nervous system stayed switched on. A 10-min pre-bed routine (slow breathing, light stretch, dim "
                      f"lights), no late meals or hard evening efforts, helps you drop into recovery faster.",
        })

    if not recs:
        recs.append({
            "title": "Keep doing what you're doing",
            "detail": "Duration, timing and architecture are all in a good place. Hold this rhythm through the training "
                      "block — consistent sleep is a genuine performance edge.",
        })
    return recs[:4]


def _training_linkage(snapshot, latest, today, cfg) -> Optional[str]:
    """Connect last night's sleep to the day's prescribed session."""
    plan = load_plan()
    day = get_day(plan, today.isoformat())
    if not day:
        return None
    dur = latest.get("duration_h")
    need = latest.get("need_h")
    hard = day.get("type") in ("quality", "long", "race")
    short = dur is not None and (need and dur < need - 0.75 or dur < 6.5)
    if hard and short:
        return (f"Heads-up: you slept {dur:.1f} h before today's {day.get('title')}. On a short night, treat the "
                f"warm-up as your gauge — if the legs and breathing don't come around, trim the reps or push the "
                f"quality to tomorrow. One well-slept hard day beats two ragged ones.")
    if hard and dur is not None:
        return (f"You slept {dur:.1f} h ahead of today's {day.get('title')} — enough to run it as prescribed. "
                f"Fuel well and go get it.")
    if day.get("type") == "rest":
        return "Rest day — the perfect chance to bank an earlier night and chip away at any sleep debt."
    return None
