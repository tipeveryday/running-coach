# -*- coding: utf-8 -*-
"""Sync the app's plan.json to Garmin Connect as scheduled workouts.

For each plan day in a rolling window (default: next 4 weeks):
  - RUN days (quality / long / easy with a special block) upload as a
    RunningWorkout with warm-up, interval reps and cool-down, pace-zoned.
  - MONDAY strength uploads as a StrengthWorkout (core block + lifts).
  - Every day gets a short evening mobility+core workout as a Strength workout.

Two modes:
  python -m garmin_sync preview            # print what would happen
  python -m garmin_sync push [weeks]       # actually push (default 4 weeks)

Idempotent: before pushing, it lists existing scheduled workouts on the target
dates and unschedules + deletes ones this script previously created (tagged in
the workoutName), so re-running produces a clean state.
"""
from __future__ import annotations
import datetime as dt
import os
import re
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import planlib
from garmin_exercise_map import garmin_exercise

TAG = "[coach]"                 # marks workouts THIS script uploaded


# ---------------- pace helpers ----------------
def _mps_from_sec_per_km(sec_per_km: float) -> float:
    return 1000.0 / max(1.0, sec_per_km)


def _hr_target(cfg: dict, purpose: str = "easy"):
    """Return (targetType dict, zone_num, None) for an HR-ZONE target (targetType 4).

    HR-ZONE lives at the STEP level, not inside targetType - Garmin's server
    silently drops any `zoneNumber` embedded in targetType and expects both
    `targetValueOne` and `zoneNumber` on the step itself. Missing them leaves
    the watch showing a broken "1-1 bpm" fallback. `_mk` writes both fields.

    We use HEART_RATE_ZONE + a zone number (not HEART_RATE_LAP with min/max BPM)
    because Fenix devices have a rendering bug where the workout screen shows
    HR=0 for lap-range targets even when the sensor is reading. Zone-number
    targets are the standard workout format and the watch reads them correctly.

    Since the watch's Running Z2 has been aligned to the app's Z2 (135-153),
    a zoneNumber=2 target maps to the exact same bpm band we want anyway.
    Recovery uses Z1."""
    zone_num = 1 if purpose == "recovery" else 2
    tt = {"workoutTargetTypeId": 4, "workoutTargetTypeKey": "heart.rate.zone",
          "displayOrder": 4}
    return tt, zone_num, None


def _pace_target(pace_key: str, paces: dict, tol: int = 5):
    """Return (targetType dict, valueOne mps low, valueTwo mps high) for a
    Garmin pace-zone target from one of the plan's pace keys. Garmin stores the
    range values on the STEP as targetValueOne/Two, not inside targetType."""
    p = paces.get(pace_key)
    if not p:
        return None, None, None
    if "sec" in p:
        mid = p["sec"]
        lo_s, hi_s = mid - tol, mid + tol
    elif "sec_low" in p and "sec_high" in p:
        lo_s, hi_s = p["sec_low"], p["sec_high"]
    else:
        return None, None, None
    v_slow = round(_mps_from_sec_per_km(hi_s), 3)   # lower m/s
    v_fast = round(_mps_from_sec_per_km(lo_s), 3)   # higher m/s
    tt = {"workoutTargetTypeId": 6, "workoutTargetTypeKey": "pace.zone", "displayOrder": 6}
    return tt, v_slow, v_fast


# ---------------- reps parser ----------------
def _sec(val: str, unit: str) -> float:
    """Parse '90', '1:30', '2 min' -> seconds."""
    unit = (unit or "s").lower()
    if ":" in val:
        m, s = val.split(":", 1)
        return int(m) * 60 + int(s)
    n = float(val)
    if unit.startswith("min"):
        return n * 60
    return n


def _pace_key_for_target_pace(pace_str: str, paces: dict) -> str:
    """Match a target pace like '4:03/km' to the plan's nearest pace key."""
    if not pace_str:
        return "threshold"
    mm = re.match(r"(\d+):(\d+)", pace_str)
    if not mm:
        return "threshold"
    target = int(mm.group(1)) * 60 + int(mm.group(2))
    best_k, best_d = "threshold", 9999
    for k, p in paces.items():
        candidates = []
        if "sec" in p:
            candidates.append(p["sec"])
        if "sec_low" in p and "sec_high" in p:
            candidates.append((p["sec_low"] + p["sec_high"]) / 2)
        for c in candidates:
            d = abs(c - target)
            if d < best_d:
                best_d, best_k = d, k
    return best_k


def _parse_reps(reps: str, cfg: dict):
    if not reps:
        return []
    paces = cfg.get("paces", {})
    tokens = [t.strip() for t in re.split(r"[·]", reps)]
    steps: list[dict] = []
    for tok in tokens:
        tl = tok.lower()
        m = re.search(r"([\d.]+)\s*km\s*warm[- ]?up", tl)
        if m:
            steps.append({"kind": "warmup", "distance_m": float(m.group(1)) * 1000, "pace_key": "easy"})
            continue
        m = re.search(r"([\d.]+)\s*km\s*cool[- ]?down", tl)
        if m:
            steps.append({"kind": "cooldown", "distance_m": float(m.group(1)) * 1000, "pace_key": "easy"})
            continue
        # alternations: N x (Am @A / Bm @B) continuous
        m = re.search(r"(\d+)\s*[×x]\s*\(\s*(\d+)\s*m\s*@\s*[0-9:.]+.*?/\s*(\d+)\s*m\s*@\s*[0-9:.]+", tl)
        if m:
            n, a, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
            for _ in range(n):
                steps.append({"kind": "interval", "distance_m": float(a), "pace_key": "threshold"})
                steps.append({"kind": "interval", "distance_m": float(b), "pace_key": "mp"})
            continue
        # sets: N x (A km @P + B km @Q) w/ Rs jog
        m = re.search(r"(\d+)\s*[×x]\s*\(\s*([\d.]+)\s*km\s*@\s*[0-9:.]+[^+]*?\+\s*([\d.]+)\s*km\s*@\s*[0-9:.]+.*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n = int(m.group(1))
            a_km, b_km = float(m.group(2)), float(m.group(3))
            r_s = _sec(m.group(4), (m.group(5) or "min"))
            for i in range(n):
                steps.append({"kind": "interval", "distance_m": a_km * 1000, "pace_key": "threshold"})
                steps.append({"kind": "interval", "distance_m": b_km * 1000, "pace_key": "mp"})
                if i < n - 1:
                    steps.append({"kind": "recovery", "duration_s": r_s, "pace_key": "easy"})
            continue
        # N x M m @pace w/ Rs jog
        m = re.search(r"(\d+)\s*[×x]\s*(\d+)\s*m\s*@\s*([0-9:.]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n, dm = int(m.group(1)), int(m.group(2))
            pk = _pace_key_for_target_pace(m.group(3), paces)
            r_s = _sec(m.group(4), (m.group(5) or "s"))
            for i in range(n):
                steps.append({"kind": "interval", "distance_m": float(dm), "pace_key": pk})
                if i < n - 1:
                    steps.append({"kind": "recovery", "duration_s": r_s, "pace_key": "easy"})
            continue
        # N x K km @pace w/ Rs jog
        m = re.search(r"(\d+)\s*[×x]\s*([\d.]+)\s*km\s*@\s*([0-9:.]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n, dk = int(m.group(1)), float(m.group(2))
            pk = _pace_key_for_target_pace(m.group(3), paces)
            r_s = _sec(m.group(4), (m.group(5) or "s"))
            for i in range(n):
                steps.append({"kind": "interval", "distance_m": dk * 1000, "pace_key": pk})
                if i < n - 1:
                    steps.append({"kind": "recovery", "duration_s": r_s, "pace_key": "easy"})
            continue
        # N x M min @pace w/ Rs jog
        m = re.search(r"(\d+)\s*[×x]\s*(\d+)\s*min\s*@\s*([0-9:.]+).*?w/\s*([\d:.]+)\s*(mins?|secs?|s)?", tl)
        if m:
            n, mins = int(m.group(1)), int(m.group(2))
            pk = _pace_key_for_target_pace(m.group(3), paces)
            r_s = _sec(m.group(4), (m.group(5) or "min"))
            for i in range(n):
                steps.append({"kind": "interval", "duration_s": mins * 60, "pace_key": pk})
                if i < n - 1:
                    steps.append({"kind": "recovery", "duration_s": r_s, "pace_key": "easy"})
            continue
        # N min fartlek
        m = re.search(r"(\d+)\s*min\s*fartlek", tl)
        if m:
            steps.append({"kind": "interval", "duration_s": int(m.group(1)) * 60, "pace_key": "tenk"})
            continue
        # N min @pace / N min tempo (avoid matching 'min jog' recovery bits)
        m = re.search(r"(\d+)\s*min\s*(?:continuous\s*)?@?\s*([0-9:.]+)?", tl)
        if m and "min jog" not in tl and "min recovery" not in tl:
            mins = int(m.group(1))
            pk = _pace_key_for_target_pace(m.group(2), paces) if m.group(2) else "threshold"
            steps.append({"kind": "interval", "duration_s": mins * 60, "pace_key": pk})
            continue
    return steps


# ---------------- workout builders ----------------
_WK_MAP: dict = {}


def _wk_of(day: dict) -> int:
    return _WK_MAP.get(day.get("date"), 0)


def _build_wk_map(plan):
    for wk in plan["weeks"]:
        for d in wk["days"]:
            _WK_MAP[d["date"]] = wk["week"]


def build_running_workout(day: dict, cfg: dict):
    from garminconnect import workout as w
    paces = cfg.get("paces", {})

    steps_spec: list[dict] = []
    if day.get("reps"):
        steps_spec = _parse_reps(day["reps"], cfg)
    if not steps_spec and day.get("segments"):
        for seg in day["segments"]:
            eff = seg.get("effort", "easy")
            pk = {"hmp": "hmp", "mp": "mp", "steady": "steady",
                  "threshold": "threshold", "fivek": "fivek", "tenk": "tenk",
                  "rep": "rep", "easy": "easy"}.get(eff, "easy")
            steps_spec.append({"kind": "interval",
                               "distance_m": (seg.get("km") or 0) * 1000, "pace_key": pk})
    # Strides-only easy day: e.g. "4-6x20s strides" -> easy body then N reps of
    # (20s stride @ rep pace + 45s jog @ easy pace). Take the upper rep count.
    if not steps_spec and day.get("strides"):
        m = re.search(r"(\d+)(?:\s*[–—-]\s*(\d+))?\s*[×x]\s*(\d+)\s*s", day["strides"])
        if m:
            n_reps = int(m.group(2) or m.group(1))
            dur = int(m.group(3))
            body_km = day.get("distance_km") or 0
            # Plan distance IS the easy body — strides go ON TOP.
            if body_km > 0:
                steps_spec.append({"kind": "interval", "distance_m": body_km * 1000,
                                   "pace_key": "easy"})
            for i in range(n_reps):
                steps_spec.append({"kind": "interval", "duration_s": dur, "pace_key": "rep"})
                if i < n_reps - 1:
                    steps_spec.append({"kind": "recovery", "duration_s": 45, "pace_key": "easy"})
    if not steps_spec:
        km = day.get("distance_km") or 0
        steps_spec.append({"kind": "interval", "distance_m": km * 1000,
                           "pace_key": day.get("pace_key") or "easy"})

    def _mk(order, s):
        pk = s.get("pace_key") or "easy"
        if pk == "easy" and s.get("kind") != "recovery":
            tt, v1, v2 = _hr_target(cfg, "easy")
        elif s.get("kind") == "recovery":
            tt, v1, v2 = None, None, None
        else:
            tt, v1, v2 = _pace_target(pk, paces)
        sec_per_km = paces.get(pk, {}).get("sec") or paces.get("easy", {}).get("sec_high", 320)
        kind = s["kind"]
        if kind == "warmup":
            secs = s.get("duration_s") or (s["distance_m"] / _mps_from_sec_per_km(paces["easy"]["sec_high"]))
            step = w.create_warmup_step(float(secs), step_order=order, target_type=tt)
        elif kind == "cooldown":
            secs = s.get("duration_s") or (s["distance_m"] / _mps_from_sec_per_km(paces["easy"]["sec_high"]))
            step = w.create_cooldown_step(float(secs), step_order=order, target_type=tt)
        elif kind == "recovery":
            return w.create_recovery_step(float(s["duration_s"]), step_order=order, target_type=None)
        elif s.get("distance_m", 0) > 0:
            step = w.create_distance_interval_step(float(s["distance_m"]),
                                                    step_order=order, target_type=tt)
        else:
            step = w.create_interval_step(float(s["duration_s"]),
                                           step_order=order, target_type=tt)
        # Garmin stores range values on the STEP itself, not inside targetType.
        if v1 is not None:
            step.targetValueOne = v1
            step.targetValueTwo = v2
        # HR-ZONE targets also need the zone number on the STEP as `zoneNumber`
        # for the watch to render the correct bpm range. Setting only
        # targetValueOne leaves the field null server-side (Garmin reads them
        # separately) and the watch falls back to a broken 1-1 bpm display.
        if tt and tt.get("workoutTargetTypeId") == 4:
            step.zoneNumber = int(v1) if v1 is not None else 2
        return step

    workout_steps = [_mk(i + 1, s) for i, s in enumerate(steps_spec)]

    est = 0.0
    for s in steps_spec:
        pk = s.get("pace_key") or "easy"
        sec_per_km = paces.get(pk, {}).get("sec") or paces.get("easy", {}).get("sec_high", 320)
        if s.get("duration_s"):
            est += s["duration_s"]
        elif s.get("distance_m"):
            est += (s["distance_m"] / 1000.0) * sec_per_km

    name = f"{TAG} W{_wk_of(day)}{day['dow'][0]} {day['title']}"[:100]
    return w.RunningWorkout(
        workoutName=name,
        description=(day.get("why") or "")[:250],
        estimatedDurationInSecs=int(round(est)),
        workoutSegments=[w.WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 1, "sportTypeKey": "running"},
            workoutSteps=workout_steps,
        )],
    )


def _rep_step(rep_desc: str):
    """Parse the leading count out of a strength/mobility line. Returns
    (sets, reps, time_based, seconds_if_time)."""
    # sets x seconds  (e.g. "3x20s")
    m = re.search(r"(\d+)\s*[xX×]\s*(\d+)\s*s\b", rep_desc)
    if m:
        return int(m.group(1)), 1, True, int(m.group(2))
    # sets x mins
    m = re.search(r"(\d+)\s*[xX×]\s*(\d+)\s*min", rep_desc)
    if m:
        return int(m.group(1)), 1, True, int(m.group(2)) * 60
    # sets x reps
    m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", rep_desc)
    if m:
        return int(m.group(1)), int(m.group(2)), False, 0
    # bare seconds hold
    m = re.search(r"(\d+)\s*(?:s|sec)\b", rep_desc)
    if m:
        return 1, 1, True, int(m.group(1))
    m = re.search(r"(\d+)\s*min", rep_desc)
    if m:
        return 1, 1, True, int(m.group(1)) * 60
    return 1, 10, False, 0


def build_strength_workout(day: dict):
    from garminconnect import workout as w
    st = day.get("strength") or {}
    exs = st.get("exercises") or []
    steps = []
    order = 1
    for e in exs:
        if not e or e.strip().startswith(("—", "-")):
            continue
        sets, reps, time_based, secs = _rep_step(e)
        cat, name = garmin_exercise(e)
        # multiple sets => single RepeatGroup (cleaner in the watch UI)
        if sets > 1:
            steps.append(w.create_strength_set(
                cat, step_order=order, sets=sets,
                reps=(secs if time_based else max(1, reps)),
                rest_seconds=60.0, exercise_name=name))
        else:
            steps.append(w.create_strength_exercise_step(
                cat, step_order=order,
                reps=(secs if time_based else max(1, reps)),
                exercise_name=name))
            steps.append(w.create_strength_rest_step(45.0, step_order=order + 1))
            order += 1
        order += 1
    wo_name = f"{TAG} W{_wk_of(day)}{day['dow'][0]} Strength - {st.get('focus') or 'core-led'}"[:100]
    return w.StrengthWorkout(
        workoutName=wo_name,
        description=(st.get("note") or "")[:250],
        estimatedDurationInSecs=int(len(exs) * 90),
        workoutSegments=[w.WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 13, "sportTypeKey": "strength_training"},
            workoutSteps=steps,
        )],
    )


def build_mobility_workout(day: dict):
    from garminconnect import workout as w
    n = day.get("nightly") or {}
    items = list(n.get("core") or []) + list(n.get("mobility") or [])
    if not items:
        return None
    steps = []
    order = 1
    for e in items:
        sets, reps, time_based, secs = _rep_step(e)
        cat, name = garmin_exercise(e)
        if sets > 1:
            steps.append(w.create_strength_set(
                cat, step_order=order, sets=sets,
                reps=(secs if time_based else max(1, reps)),
                rest_seconds=15.0, exercise_name=name))
        else:
            steps.append(w.create_strength_exercise_step(
                cat, step_order=order,
                reps=(secs if time_based else max(1, reps)),
                exercise_name=name))
        order += 1
    title = n.get("title") or "Nightly mobility"
    wo_name = f"{TAG} W{_wk_of(day)}{day['dow'][0]} PM {title}"[:100]
    return w.StrengthWorkout(
        workoutName=wo_name,
        description=(n.get("focus") or "")[:250],
        estimatedDurationInSecs=(n.get("duration_min") or 10) * 60,
        workoutSegments=[w.WorkoutSegment(
            segmentOrder=1,
            sportType={"sportTypeId": 13, "sportTypeKey": "strength_training"},
            workoutSteps=steps,
        )],
    )


# ---------------- iteration over the plan ----------------
def plan_days_in_window(plan, cfg, start: dt.date, end: dt.date):
    for wk in plan["weeks"]:
        for d in wk["days"]:
            dd = dt.date.fromisoformat(d["date"])
            if start <= dd <= end:
                yield planlib.resolve_day(d, plan, cfg)


def workouts_for_window(weeks_ahead: int = 2, start: Optional[dt.date] = None, kinds=("run", "strength", "mobility")):
    cfg = planlib.resolve_active_race(planlib.load_config(), dt.date.today())
    plan = planlib.load_plan()
    _build_wk_map(plan)
    if start is None:
        start = dt.date.today()
    end = start + dt.timedelta(days=weeks_ahead * 7 - 1)
    out = []
    for d in plan_days_in_window(plan, cfg, start, end):
        t = d.get("type")
        if "run" in kinds and t in ("quality", "long", "easy", "race"):
            wo = build_running_workout(d, cfg)
            if wo:
                out.append((d["date"], "run", wo))
        if "strength" in kinds and d.get("strength"):
            wo = build_strength_workout(d)
            if wo:
                out.append((d["date"], "strength", wo))
        if "mobility" in kinds and d.get("nightly"):
            wo = build_mobility_workout(d)
            if wo:
                out.append((d["date"], "mobility", wo))
    return out


# ---------------- Garmin API glue ----------------
def _client():
    import garmin_client
    return garmin_client.GarminData().client()


def _months_covering(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield (y, m)
        m += 1
        if m == 13:
            m = 1
            y += 1


def existing_tagged(g, start: dt.date, end: dt.date):
    out = []
    for (y, m) in _months_covering(start, end):
        try:
            data = g.get_scheduled_workouts(y, m)
        except Exception as e:
            print(f"  [!] get_scheduled_workouts({y},{m}) failed: {e}")
            continue
        entries = data
        if isinstance(data, dict):
            entries = data.get("calendarItems") or data.get("items") or []
        for w in (entries or []):
            date_iso = (w.get("calendarDate") or w.get("date") or w.get("scheduledDate") or "")[:10]
            if not date_iso:
                continue
            try:
                dd = dt.date.fromisoformat(date_iso)
            except Exception:
                continue
            if not (start <= dd <= end):
                continue
            # Garmin's scheduled-workout DTO uses `title`, not `workoutName`.
            name = (w.get("title")
                    or w.get("workoutName")
                    or (w.get("workout") or {}).get("workoutName") or "")
            if name.startswith(TAG):
                # normalize into the shape push() expects
                out.append({
                    "scheduledWorkoutId": w.get("id") or w.get("scheduledWorkoutId"),
                    "workoutId": w.get("workoutId") or (w.get("workout") or {}).get("workoutId"),
                    "title": name,
                })
    return out


def push(weeks_ahead: int = 2, dry: bool = False, kinds=("run", "strength", "mobility")):
    start = dt.date.today()
    end = start + dt.timedelta(days=weeks_ahead * 7 - 1)
    items = workouts_for_window(weeks_ahead, start, kinds=kinds)
    print(f"Window: {start} .. {end}  ({weeks_ahead} weeks)")
    print(f"Workouts to push: {len(items)}")
    kinds = {}
    for _, k, _ in items:
        kinds[k] = kinds.get(k, 0) + 1
    print(f"  by kind: {kinds}")

    if dry:
        print("\n=== DRY RUN (no changes) ===")
        for date, kind, wo in items:
            steps = 0
            try:
                d = wo.to_dict()
                seg = (d.get("workoutSegments") or [{}])[0]
                for s in seg.get("workoutSteps") or []:
                    if s.get("type") == "RepeatGroupDTO":
                        steps += s.get("numberOfIterations", 1) * len(s.get("workoutSteps") or [])
                    else:
                        steps += 1
            except Exception:
                pass
            print(f"  {date}  {kind:8}  steps={steps:>3}  {wo.workoutName}")
        return

    g = _client()
    print("\n1) Removing ALL prior [coach] workouts (any date)...")
    all_ws = []
    for st in (0, 100, 200, 300, 400):
        pg = g.get_workouts(start=st, limit=100)
        all_ws.extend(pg or [])
        if not pg or len(pg) < 100:
            break
    prev = [w for w in all_ws if (w.get("workoutName") or "").startswith(TAG)]
    scheduled_by_wid = {}
    for (y, m2) in _months_covering(start - dt.timedelta(days=60),
                                     end + dt.timedelta(days=365)):
        try:
            data = g.get_scheduled_workouts(y, m2)
        except Exception:
            continue
        items_ = data if isinstance(data, list) else (data.get("calendarItems") or [])
        for it in items_ or []:
            if it.get("itemType") != "workout":
                continue
            wid = it.get("workoutId")
            sid = it.get("id")
            if wid and sid and (it.get("title") or "").startswith(TAG):
                scheduled_by_wid.setdefault(wid, []).append(sid)
    total_sched = sum(len(v) for v in scheduled_by_wid.values())
    print(f"   library [coach] workouts: {len(prev)}   scheduled instances: {total_sched}")
    for w in prev:
        wid = w.get("workoutId")
        for sid in scheduled_by_wid.get(wid, []):
            try:
                g.unschedule_workout(sid)
            except Exception as e:
                print(f"     [!] unschedule {sid}: {e}")
        try:
            g.delete_workout(wid)
        except Exception as e:
            print(f"     [!] delete {wid}: {e}")

    print("\n2) Uploading + scheduling new workouts...")
    ok = fail = 0
    for date, kind, wo in items:
        try:
            if kind == "run":
                resp = g.upload_running_workout(wo)
            else:
                resp = g.upload_strength_workout(wo)
            wid = None
            if isinstance(resp, dict):
                wid = resp.get("workoutId") or (resp.get("workout") or {}).get("workoutId")
            if not wid:
                print(f"   [!] {date} {kind}: upload gave no workoutId; skipping schedule")
                fail += 1
                continue
            g.schedule_workout(wid, date)
            print(f"   OK {date} {kind:8} id={wid}  {wo.workoutName[:60]}")
            ok += 1
        except Exception as e:
            print(f"   [!] {date} {kind}: {e}")
            fail += 1

    print(f"\nDone. uploaded+scheduled: {ok}   failures: {fail}")


if __name__ == "__main__":
    argv = sys.argv[1:]
    cmd = argv[0] if argv else "preview"
    weeks = int(argv[1]) if len(argv) >= 2 and argv[1].isdigit() else 2
    plus = tuple(a.lstrip("+") for a in argv[2:] if a.startswith("+"))
    kinds = plus or ("run", "strength", "mobility")
    if cmd == "preview":
        push(weeks_ahead=weeks, dry=True, kinds=kinds)
    elif cmd == "push":
        push(weeks_ahead=weeks, dry=False, kinds=kinds)
    else:
        print(__doc__)
        sys.exit(1)
