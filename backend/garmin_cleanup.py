# -*- coding: utf-8 -*-
"""Clean up Garmin Connect workouts to stay under the account limit.

Removes:
  1. Old coach-plan workouts (names matching "W## Dow — ..." from before the
     [coach] tag existed) - these are stale, replaced by the tagged sync
  2. [coach] workouts scheduled in the PAST (already done; freeing space)

Keeps everything else - user-created workouts, current [coach] workouts scheduled
for today/future, anything that doesn't match the patterns.

Modes:
  python backend/garmin_cleanup.py preview
  python backend/garmin_cleanup.py delete
"""
from __future__ import annotations
import datetime as dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import garmin_client

TAG = "[coach]"
# old-plan format: "W3 Wed — 5×1 km @ threshold" or "W9 Wed — Strength: light"
OLD_PLAN_RE = re.compile(r"^W\d+\s+(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+[—-]")


def _client():
    return garmin_client.GarminData().client()


def _all_workouts(g):
    out = []
    for start in (0, 100, 200, 300, 400):
        p = g.get_workouts(start=start, limit=100)
        out.extend(p or [])
        if not p or len(p) < 100:
            break
    return out


def _all_scheduled(g, months_back=6, months_forward=24):
    """Fetch every scheduled workout the account has across a wide date window.
    Returns list of scheduled-workout dicts with scheduledDate normalized."""
    today = dt.date.today()
    seen = {}
    for delta in range(-months_back, months_forward + 1):
        d = today.replace(day=1) + dt.timedelta(days=32 * delta)
        try:
            data = g.get_scheduled_workouts(d.year, d.month)
        except Exception:
            continue
        items = data if isinstance(data, list) else (data.get("calendarItems") or data.get("items") or [])
        for it in items or []:
            sid = it.get("scheduledWorkoutId") or it.get("id")
            wid = it.get("workoutId") or (it.get("workout") or {}).get("workoutId")
            dt_iso = (it.get("date") or it.get("calendarDate") or it.get("scheduledDate") or "")[:10]
            name = it.get("workoutName") or (it.get("workout") or {}).get("workoutName") or it.get("title") or ""
            if sid and wid:
                seen[sid] = {"scheduledWorkoutId": sid, "workoutId": wid,
                             "date": dt_iso, "workoutName": name}
    return list(seen.values())


def categorize(all_workouts, all_scheduled):
    today = dt.date.today().isoformat()
    sched_by_wid = {}
    for s in all_scheduled:
        sched_by_wid.setdefault(s["workoutId"], []).append(s)

    old_plan = []          # untagged W## Dow — workouts (stale coach plan)
    past_coach = []        # [coach]-tagged, all scheduled dates < today
    keep = []              # everything else — keep untouched

    for w in all_workouts:
        wid = w.get("workoutId")
        name = w.get("workoutName") or ""
        sched_dates = [s["date"] for s in sched_by_wid.get(wid, [])]

        if OLD_PLAN_RE.match(name):
            old_plan.append({"wid": wid, "name": name, "sched": sched_by_wid.get(wid, [])})
        elif name.startswith(TAG) and sched_dates and all(d < today for d in sched_dates if d):
            past_coach.append({"wid": wid, "name": name, "sched": sched_by_wid.get(wid, [])})
        else:
            keep.append({"wid": wid, "name": name, "sched": sched_by_wid.get(wid, [])})
    return old_plan, past_coach, keep


def _delete(g, targets, label):
    ok = fail = 0
    for t in targets:
        for s in t["sched"]:
            try:
                g.unschedule_workout(s["scheduledWorkoutId"])
            except Exception as e:
                print(f"   [!] unschedule sid={s['scheduledWorkoutId']}: {e}")
        try:
            g.delete_workout(t["wid"])
            ok += 1
        except Exception as e:
            fail += 1
            print(f"   [!] delete wid={t['wid']}: {e}")
    print(f"  {label}: deleted {ok}, failed {fail}")


def main(mode):
    g = _client()
    print("Fetching account state...")
    all_ws = _all_workouts(g)
    all_s = _all_scheduled(g)
    old_plan, past_coach, keep = categorize(all_ws, all_s)
    print(f"Account has {len(all_ws)} workouts total.")
    print(f"  old-plan (untagged, stale)   : {len(old_plan)}")
    print(f"  past [coach] (already done)  : {len(past_coach)}")
    print(f"  keep (current + user-created): {len(keep)}")
    remaining = len(keep)
    print(f"  after cleanup: {remaining} workouts (target < 100)")

    if mode == "preview":
        print("\n=== OLD-PLAN WORKOUTS (would delete) ===")
        for t in old_plan[:12]:
            ds = ",".join(s["date"] for s in t["sched"])
            print(f"  wid={t['wid']}  {ds}  {t['name'][:60]}")
        if len(old_plan) > 12:
            print(f"  ... and {len(old_plan)-12} more")
        print("\n=== PAST [coach] WORKOUTS (would delete) ===")
        for t in past_coach[:12]:
            ds = ",".join(s["date"] for s in t["sched"])
            print(f"  wid={t['wid']}  {ds}  {t['name'][:60]}")
        if len(past_coach) > 12:
            print(f"  ... and {len(past_coach)-12} more")
        return

    if mode == "delete":
        print("\n=== DELETING ===")
        _delete(g, old_plan, "old-plan")
        _delete(g, past_coach, "past [coach]")
        # re-check
        all_ws2 = _all_workouts(g)
        print(f"\nDone. Account now has {len(all_ws2)} workouts.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "preview"
    main(mode)
