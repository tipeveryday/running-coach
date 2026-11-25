"""Run insights library.

Turns a completed run (summary metrics + optional per-point stream) plus its
prescribed plan day into a ranked list of coaching insights — each one saying
what went well or what to improve, and *how* to improve it. Rooted in the
workout types the plan actually prescribes: easy/Z2, strides, VO2/interval reps,
threshold/tempo, long runs, and long runs with a race-pace (MP/HMP) block.

Every insight is a dict:
    {"tone": "good"|"improve"|"note", "cat": <slug>, "title": str,
     "detail": str, "how": str|None}

`build_insights` degrades gracefully: with a stream it does split / rep / block
analysis; without one it still reads cadence, effort discipline and the workout
stimulus from summary fields.
"""
from __future__ import annotations
import re
from typing import Optional

from planlib import pace_str, fmt_secs


# ---------------- small helpers ----------------
def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _seg(pts, key="pace_s"):
    return _avg([p.get(key) for p in pts])


def _mk(tone, cat, title, detail, how=None):
    return {"tone": tone, "cat": cat, "title": title, "detail": detail, "how": how}


def good(cat, title, detail, how=None):
    return _mk("good", cat, title, detail, how)


def improve(cat, title, detail, how):
    return _mk("improve", cat, title, detail, how)


def note(cat, title, detail, how=None):
    return _mk("note", cat, title, detail, how)


# ---------------- stream primitives ----------------
def _clean(series):
    return [p for p in (series or []) if p.get("pace_s") and 120 < p["pace_s"] < 900]


def _splits(series):
    """Per-km splits by integrating speed over elapsed time."""
    pts = [p for p in _clean(series) if p.get("t") is not None]
    if len(pts) < 12:
        return []
    splits, dist, last_t, bucket, km = [], 0.0, pts[0]["t"], [], 1
    for p in pts:
        dtt = max(0.0, p["t"] - last_t); last_t = p["t"]
        dist += (1000.0 / p["pace_s"]) * dtt          # metres
        bucket.append(p)
        if dist >= km * 1000:
            splits.append({"km": km, "pace_s": round(_seg(bucket)),
                           "hr": (round(_seg(bucket, "hr")) if _seg(bucket, "hr") else None)})
            bucket, km = [], km + 1
    return splits


def _reps(series, target_sec):
    """Group contiguous 'work' points (grade-adjusted pace at/under target) into
    reps, so we can read hit-rate and rep-to-rep fade."""
    pts = [p for p in _clean(series) if p.get("t") is not None]
    if len(pts) < 12 or not target_sec:
        return []
    thr = target_sec * 1.15
    reps, cur = [], []
    for p in pts:
        gp = p.get("gap_s") or p["pace_s"]
        if gp <= thr:
            cur.append(p)
        else:
            if cur and (cur[-1]["t"] - cur[0]["t"]) >= 25:
                reps.append(cur)
            cur = []
    if cur and (cur[-1]["t"] - cur[0]["t"]) >= 25:
        reps.append(cur)
    out = []
    for r in reps:
        out.append({"pace_s": round(_seg(r)),
                    "gap_s": round(_seg(r, "gap_s") or _seg(r)),
                    "hr": (round(_seg(r, "hr")) if _seg(r, "hr") else None),
                    "secs": round(r[-1]["t"] - r[0]["t"]), "pts": r})
    return out


def _steady_window(series):
    """Steady portion for a durability read: drop the warm-up ramp and cut off at
    the onset of late strides / finishing surges."""
    pts = [p for p in _clean(series) if p.get("hr")]
    if len(pts) < 30:
        return pts
    paces = sorted(p["pace_s"] for p in pts)
    med = paces[len(paces) // 2]
    cut = med * 0.82
    n = len(pts)
    end = n
    for i in range(int(n * 0.55), n):
        if pts[i]["pace_s"] < cut:
            end = i
            break
    win = pts[int(n * 0.12):end]
    return win if len(win) >= 24 else pts


def _sustained_work(series):
    pts = _clean(series)
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
            cur += 1; best = max(best, cur)
        else:
            cur = 0
    return best * dt_pt >= 150


def _drift(pts):
    """First-half vs second-half efficiency (speed ÷ HR) drift on a set of steady
    points. Returns pct + the HR/pace endpoints, or None."""
    w = [p for p in pts if p.get("hr") and p.get("pace_s")]
    if len(w) < 16:
        return None
    w = sorted(w, key=lambda p: p.get("t") or 0)
    h = len(w) // 2
    a, b = w[:h], w[h:]
    e1 = (1000.0 / _seg(a)) / _seg(a, "hr")
    e2 = (1000.0 / _seg(b)) / _seg(b, "hr")
    return {"pct": round(100 * (e1 - e2) / e1, 1),
            "hr1": round(_seg(a, "hr")), "hr2": round(_seg(b, "hr")),
            "p1": round(_seg(a)), "p2": round(_seg(b))}


# ---------------- durability (aerobic / holding effort) ----------------
def _durability_insight(series, kind):
    win = _steady_window(series)
    d = _drift(win)
    if not d:
        return None
    late_strides = kind == "easy" and not _sustained_work(series)
    tail = " (finishing strides excluded)" if late_strides else ""
    pct = d["pct"]
    hrtxt = f"HR {d['hr1']}→{d['hr2']} bpm at held pace"
    if pct < 4:
        return good("durability", "Held aerobic form to the end",
                    f"Efficiency stayed flat over the steady running — {hrtxt}{tail} (only {pct}% drift). Strong durability at this effort.")
    if kind == "easy":
        if pct < 8:
            return good("durability", "Steady all the way",
                        f"{hrtxt}{tail} — {pct}% drift. Normal, low HR creep for an easy run; the aerobic system was comfortable.")
        return improve("durability", "Easy run drifted upward",
                       f"{hrtxt}{tail} — {pct}% drift. The easy run cost a bit more than the pace suggests (heat, fatigue, or ran a touch quick).",
                       "Keep easy days genuinely easy and hydrate; if it recurs on true easy days, it's a fatigue flag to watch.")
    if pct < 8:
        return improve("durability", "Mild late drift",
                       f"{hrtxt}{tail} — {pct}% decoupling. Your aerobic system was being stretched a little late on.",
                       "Keep stacking easy aerobic volume and fuel on longer efforts; this tightens up as the base deepens.")
    return improve("durability", "Faded late — durability limiter",
                   f"{hrtxt}{tail} — {pct}% decoupling. HR climbed hard while pace held.",
                   "More easy volume and longer steady long runs push this ceiling out; fuel earlier (carbs every 30–40 min) on long efforts.")


def _hold_insight(pts, label):
    d = _drift(pts)
    if not d:
        return None
    pct = d["pct"]
    span = f"HR {d['hr1']}→{d['hr2']} while pace held {pace_str(d['p1'])}→{pace_str(d['p2'])}/km"
    if pct < 3:
        return good("hold", f"Held the {label} with a flat HR",
                    f"Across the {label} your {span} — {pct}% drift. Excellent lactate clearance: the engine wasn't fading.")
    if pct < 6:
        return improve("hold", f"Slight drift late in the {label}",
                       f"{span} — {pct}% drift. A little late creep is normal at this intensity.",
                       "It shrinks with threshold volume; hold the early portion a hair more conservatively so the end stays smooth.")
    return improve("hold", f"HR spiked late in the {label}",
                   f"{span} — {pct}% drift. You couldn't hold the effort at a stable HR to the end.",
                   "This is your threshold-endurance ceiling. More sustained tempo volume and long-run strength push it out.")


# ---------------- general ----------------
def _cadence(ins, run):
    cad = run.get("avg_cadence")
    if not cad or cad < 40:
        return
    cad = round(cad)
    # Healthy cadence for trained runners at aerobic pace is 160-180 spm - the
    # 180-dogma comes from elite race-pace observation and doesn't scale down.
    # Only flag when the number is genuinely outside the band, and let the
    # dedicated GCT / vertical-oscillation reads handle the actual overstriding
    # signal (so we don't double-flag on cadence alone).
    if cad >= 165:
        ins.append(good("cadence", "Efficient cadence",
                        f"Averaged {cad} spm — in the 165-185 healthy range. Quick, light turnover keeps ground contact short and impact low."))
    elif cad >= 155:
        ins.append(improve("cadence", "Cadence on the low side",
                           f"Averaged {cad} spm — fine, on the lower end of the healthy band.",
                           "If you feel like you're reaching or hearing a heavy footstrike, a 170-bpm metronome on a few easy runs nudges turnover up without effort. Otherwise this is your natural stride and doesn't need correcting."))
    else:
        ins.append(improve("cadence", "Cadence genuinely low",
                           f"Cadence {cad} spm sits below the 160-180 healthy band — likely overstriding.",
                           "Quicken to 160+ spm with short, snappy steps, landing under your hips. Check the GCT and vertical-oscillation reads too - they usually corroborate."))


def _consistency(ins, series, forgiving=False):
    """Even pacing on runs that should be steady (easy / long / tempo)."""
    sp = _splits(series)
    if len(sp) < 4:
        return
    paces = [s["pace_s"] for s in sp[1:]]                 # skip the first (warm-up) km
    if len(paces) < 3:
        return
    mean = _avg(paces)
    spread = max(paces) - min(paces)
    if spread <= 12:
        ins.append(good("pacing", "Metronomic pacing",
                        f"Your km splits held within {spread}s of each other around {pace_str(round(mean))}/km — beautifully even effort."))
    elif not forgiving and spread >= 35:
        ins.append(improve("pacing", "Uneven pacing",
                           f"Km splits swung ~{spread}s ({pace_str(min(paces))}–{pace_str(max(paces))}/km).",
                           "On steady runs, settle into one gear early — even pacing is more aerobically productive than surging and easing."))


# ---------------- per-type ----------------
def _easy_start(ins, paces, series):
    """Did the run go out too hard? A whole-run average can look fine while the
    opening kilometres were way too quick — that's what actually blunts an easy
    day. The first ~10 minutes should sit at the SLOW end of the easy band while
    the aerobic system spins up."""
    if not series:
        return
    easy = paces.get("easy", {})
    lo, hi = easy.get("sec_low", 295), easy.get("sec_high", 325)
    open_pts = [p for p in series if (p.get("t") or 0) <= 600 and p.get("pace_s")]
    rest_pts = [p for p in series if (p.get("t") or 0) > 600 and p.get("pace_s")]
    if len(open_pts) < 5 or len(rest_pts) < 5:
        return
    op, rp = _avg([p["pace_s"] for p in open_pts]), _avg([p["pace_s"] for p in rest_pts])
    if not op or not rp:
        return
    # faster than the easy band's quick end during the opening 10 min
    if op < lo - 5:
        ins.append(improve("pacing", "Went out too hard",
                           f"The first 10 minutes averaged {pace_str(op)}/km — quicker than the fast end of your "
                           f"easy band ({easy.get('low')}/km), before your aerobic system had even spun up.",
                           f"Start easy runs at {easy.get('high')}/km for the first 2 km and let the pace come to you. "
                           f"Cold muscles plus a lagging heart rate mean the opening always feels easier than it is."))
    elif op < rp - 12:
        ins.append(note("pacing", "Opened quick, settled later",
                        f"First 10 min at {pace_str(op)}/km vs {pace_str(rp)}/km after — you drifted "
                        f"{round(rp - op)}s/km slower once the run settled.",
                        "A slight positive split on an easy run is fine, but starting slower makes the whole run "
                        "cheaper aerobically."))
    elif op >= hi - 10:
        ins.append(good("pacing", "Opened patiently",
                        f"First 10 min at {pace_str(op)}/km — genuinely easy off the line. That patience is exactly "
                        f"what keeps an easy run cheap and leaves the quality days intact."))


def _easy(ins, run, day, paces, series):
    pace = run.get("avg_pace_s_per_km")
    easy = paces.get("easy", {})
    lo, band = easy.get("sec_low", 295), f"{easy.get('low')}–{easy.get('high')}/km"
    _easy_start(ins, paces, series)
    if pace:
        if pace >= lo - 8:
            ins.append(good("discipline", "Easy discipline held",
                            f"Ran {pace_str(pace)}/km, right in your easy band ({band}). This is how aerobic volume banks without a recovery cost."))
        else:
            ins.append(improve("discipline", "Easy day ran hot",
                               f"Ran {pace_str(pace)}/km — quicker than your easy band ({band}). That's the grey zone: too hard to fully recover, too easy to be a workout.",
                               "Hold easy days so you could talk in full sentences. Slower easy days are what let the quality days land."))
    _consistency(ins, series, forgiving=True)
    d = _durability_insight(series, "easy")
    if d:
        ins.append(d)


def _quality(ins, run, day, paces, series):
    pk = day.get("pace_key")
    target = paces.get(pk, {})
    tgt, tstr = target.get("sec"), target.get("target")
    reps_str = day.get("reps") or ""
    is_intervals = bool(re.search(r"\d+\s*[×x]\s*[\d.]", reps_str))
    reps = _reps(series, tgt)
    ate, anate = run.get("aerobic_te") or 0, run.get("anaerobic_te") or 0
    if not reps:
        if ate >= 3.5:
            ins.append(good("stimulus", "Real quality stimulus",
                            f"Aerobic training-effect {ate} with max HR {run.get('max_hr')} — a genuine workout. A watch that records the trace unlocks rep-level detail."))
        return
    n = len(reps)
    work_pts = [p for r in reps for p in r["pts"]]
    avg_gap = _avg([r["gap_s"] for r in reps])
    diff = round(avg_gap - tgt)
    total_work = fmt_secs(sum(r["secs"] for r in reps))
    unit = "reps" if is_intervals else "block"
    # 1) target (GAP is already terrain-adjusted, so don't credit hills again)
    if diff <= 8:
        ins.append(good("target", f"Nailed the {tstr}/km target",
                        f"The work {unit} averaged {pace_str(round(avg_gap))}/km grade-adjusted over {total_work} — bang on."))
    elif diff <= 18:
        ins.append(good("target", f"Just under the {tstr}/km target",
                        f"Work averaged {pace_str(round(avg_gap))}/km grade-adjusted — about {pace_str(diff)}/km under target. Close; fresher legs or flatter ground bring it to pace."))
    else:
        ins.append(improve("target", "Work ran under target",
                           f"Work averaged {pace_str(round(avg_gap))}/km grade-adjusted vs {tstr}/km — about {pace_str(diff)}/km off.",
                           "If it felt maxed, the target is ahead of today's fitness — bank the effort. If it felt controllable, commit to goal pace from the first rep."))
    # 2) intervals → rep-to-rep consistency; continuous tempo → hold within the block
    if is_intervals and n >= 3:
        k = max(1, n // 3)
        fade = round(_avg([r["gap_s"] for r in reps[-k:]]) - _avg([r["gap_s"] for r in reps[:k]]))
        if fade <= 4:
            ins.append(good("pacing", f"Rock-steady {n}-rep set",
                            f"Only {abs(fade)}s/km between your first and last reps — excellent control under fatigue."))
        elif fade <= 12:
            ins.append(improve("pacing", "Slight fade across the set",
                               f"Reps slipped ~{fade}s/km from first to last.",
                               "Negative-split the set: open 1–2s/km slower than target so the closing reps hold — or descend."))
        else:
            ins.append(improve("pacing", "Faded through the set",
                               f"Reps dropped ~{fade}s/km start to finish — you likely went out too hot.",
                               "Bank the opener 2–3s/km easier than target; aim for even or descending splits across the whole set."))
    else:
        h = _hold_insight(work_pts, "tempo")
        if h:
            ins.append(h)
    # 3) VO2 sharpness for short, fast reps
    if anate >= 1.0 and (tgt or 999) <= (paces.get("tenk", {}).get("sec", 237)):
        ins.append(note("stimulus", "VO2 / anaerobic touch",
                        f"Anaerobic training-effect {anate}, max HR {run.get('max_hr')} — you reached the top gears. Good for economy and top-end power; keep these sparing."))


def _long(ins, run, day, paces, series):
    dist = run.get("distance_km") or 0
    dur = fmt_secs(run.get("moving_s"))
    ins.append(good("endurance", "Time on feet banked",
                    f"{dist} km in {dur} — long runs build the fatigue resistance and slow-twitch efficiency that carry the back half of a race."))
    # race-pace block
    if day.get("hmp_km"):
        _race_pace_block(ins, run, day, paces, series)
    # durability is the headline long-run metric
    d = _durability_insight(series, "long")
    if d:
        ins.append(d)
    # positive split check
    sp = _splits(series)
    if len(sp) >= 8:
        half = len(sp) // 2
        first = _avg([s["pace_s"] for s in sp[:half]])
        second = _avg([s["pace_s"] for s in sp[half:]])
        slow = round(second - first)
        if slow >= 20 and not day.get("hmp_km"):
            ins.append(improve("pacing", "Second half slowed",
                               f"You ran the back half ~{pace_str(slow)}/km slower than the front.",
                               "Start long runs genuinely easy so you can hold — or negative-split — the finish. Fading late is usually pacing or fueling, not fitness."))


def _race_pace_block(ins, run, day, paces, series):
    mp = paces.get("mp", {}).get("sec") or paces.get("hmp", {}).get("sec")
    lbl = paces.get("mp", {}).get("target") if paces.get("mp") else paces.get("hmp", {}).get("target")
    reps = _reps(series, mp) if series else []
    block = max(reps, key=lambda r: r["secs"]) if reps else None
    km = day.get("hmp_km")
    if not block:
        ins.append(note("racepace", "Race-pace block prescribed",
                        f"{km} km at {lbl}/km was on the card. Record the trace to see how the block itself landed (the whole-run average hides it)."))
        return
    diff = round(block["gap_s"] - mp)
    dur = fmt_secs(block["secs"])
    if diff <= 8:
        ins.append(good("racepace", "Race-pace block on the money",
                        f"Held {pace_str(block['gap_s'])}/km grade-adjusted for {dur} deep in the run — right at {lbl}/km. This is the session that makes race pace automatic on tired legs."))
    elif diff <= 20:
        ins.append(good("racepace", "Race-pace block close",
                        f"Block averaged {pace_str(block['gap_s'])}/km GAP over {dur} — a few seconds off {lbl}/km once terrain is counted. Solid execution on fatigue."))
    else:
        ins.append(improve("racepace", "Race-pace block drifted off",
                           f"The block ran {pace_str(block['gap_s'])}/km vs {lbl}/km — about {pace_str(diff)}/km off.",
                           "Save more for it: run the easy portion before the block truly easy, then lock into goal pace and hold. Holding it when tired is the whole skill."))
    # HR efficiency at race pace
    if block.get("hr"):
        lthr = 170
        z = "aerobic (Z3)" if block["hr"] < lthr - 4 else ("threshold (Z4)" if block["hr"] <= lthr + 2 else "above threshold")
        if block["hr"] < lthr - 4:
            ins.append(good("efficiency", "Race pace stayed aerobic",
                            f"The block cost only HR {block['hr']} — comfortably {z}. Goal pace sitting this low is a strong marathon signal."))
        else:
            ins.append(improve("efficiency", "Race pace ran near threshold",
                               f"The block cost HR {block['hr']} — {z}. Goal pace is currently costly to hold.",
                               "As threshold and aerobic base improve, this HR drops toward Z3 and race pace feels easy. Keep the tempo + MP work coming."))
    # hold within the block
    h = _hold_insight(block["pts"], "race-pace block")
    if h and diff <= 20:                                   # only if they were actually near pace
        ins.append(h)


def _detect_strides(series, paces):
    """Two or more short, fast bursts in the back half — the stride signature."""
    pts = [p for p in _clean(series) if p.get("t") is not None]
    if len(pts) < 20:
        return False
    rep_cut = paces.get("rep", {}).get("sec", 210) * 1.12
    late = pts[int(len(pts) * 0.5):]
    bursts, cur = 0, False
    for p in late:
        fast = (p.get("gap_s") or p["pace_s"]) <= rep_cut
        if fast and not cur:
            bursts += 1
        cur = fast
    return bursts >= 2


def _strides(ins, run, series, paces):
    # Strides are ~20s — too short for the rep grouper — so read the fast bursts
    # in the closing portion of the run directly.
    pts = [p for p in _clean(series or []) if p.get("t") is not None]
    rep_sec = paces.get("rep", {}).get("sec", 210)
    rep_pace = paces.get("rep", {}).get("target", "3:30")
    late = pts[int(len(pts) * 0.45):] if pts else []
    fast = [p for p in late if (p.get("gap_s") or p["pace_s"]) <= rep_sec * 1.18]
    if fast:
        fastest = min((p.get("gap_s") or p["pace_s"]) for p in fast)
        bursts, cur = 0, False
        for p in late:
            f = (p.get("gap_s") or p["pace_s"]) <= rep_sec * 1.18
            if f and not cur:
                bursts += 1
            cur = f
        if fastest <= (paces.get("fivek", {}).get("sec", 225) + 12):
            ins.append(good("strides", f"{bursts} sharp strides to finish",
                            f"Picked up to ~{pace_str(round(fastest))}/km in the closing bursts — fast-but-relaxed. Strides bank neuromuscular sharpness and economy with zero aerobic cost. Ideal on an easy day."))
        else:
            ins.append(improve("strides", "Strides a touch soft",
                               f"Closing pickups reached ~{pace_str(round(fastest))}/km — a little conservative (target ~{rep_pace}/km, mile-to-5K feel).",
                               "Open up more for the middle 10s: drive the arms, quick light turnover, then float the finish. Fast and smooth, not strained."))
    else:
        ins.append(note("strides", "Strides were on the plan",
                        f"A few 20s strides @~{rep_pace}/km were prescribed — quick pickups that keep the legs snappy. Add them at the end of the easy run if you skipped them."))


# ---------------- public ----------------
_ORDER = {"good": 0, "improve": 0, "note": 1}
_CAT_RANK = {"target": 0, "racepace": 0, "pacing": 1, "hold": 1, "durability": 2,
             "efficiency": 2, "discipline": 2, "endurance": 3, "stimulus": 3,
             "strides": 3, "cadence": 4}


def build_insights(run: dict, day: Optional[dict], cfg: dict,
                   snapshot: Optional[dict] = None, stream: Optional[dict] = None) -> list:
    paces = cfg.get("paces", {})
    series = (stream or {}).get("series") if stream else None
    dtype = (day or {}).get("type")
    ins: list = []

    if dtype == "easy":
        _easy(ins, run, day, paces, series)
    elif dtype == "quality":
        _quality(ins, run, day, paces, series)
    elif dtype in ("long", "race"):
        _long(ins, run, day, paces, series)
    else:
        # off-plan / cross type — still give a durability + consistency read
        _consistency(ins, series, forgiving=True)
        d = _durability_insight(series, "easy")
        if d:
            ins.append(d)

    # strides: only when prescribed, or clearly present on an easy/long day
    # (never treat interval reps on a quality day as strides)
    if (day or {}).get("strides") or (dtype in ("easy", "long") and _detect_strides(series, paces)):
        _strides(ins, run, series, paces)

    _cadence(ins, run)

    # rank: actionable first, then by category importance; cap the list
    ins.sort(key=lambda x: (_ORDER.get(x["tone"], 1), _CAT_RANK.get(x["cat"], 5)))
    # ensure at least one "good" leads when present, keep it readable
    return ins[:6]
