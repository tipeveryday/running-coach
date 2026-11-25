# -*- coding: utf-8 -*-
"""Demo mode (RC_DEMO=1): a fictional runner with 90 days of synthetic Garmin
data, so anyone can try the app — and the README screenshots — without a
Garmin account.

The athlete is eight weeks into a half-marathon plan. Past plan days become
logged runs (with realistic misses, pace noise and HR), and sleep / HRV /
stress / body battery follow the training load. Seeded, so every start
produces the same data.
"""
from __future__ import annotations
import datetime as dt
import math
import random
from typing import Any, Optional

import cache
import paths
import planlib
import plangen

SEED = 42
HISTORY_DAYS = 90

DEMO_PROFILE_WEEKS_DONE = 8
DEMO_PROFILE_WEEKS_LEFT = 8


def demo_profile(today: dt.date) -> dict:
    monday = today - dt.timedelta(days=today.weekday())
    race = monday + dt.timedelta(weeks=DEMO_PROFILE_WEEKS_LEFT, days=6)
    return {
        "name": "Alex Demo", "sex": "F", "birth_date": "1991-04-12", "level": "intermediate",
        "distance": "half", "race_name": "Riverside Half Marathon", "race_location": "Springfield",
        "race_date": race.isoformat(), "goal_time": "1:39:00",
        "start_date": (monday - dt.timedelta(weeks=DEMO_PROFILE_WEEKS_DONE)).isoformat(),
        "current_km": 38, "peak_km": 58, "runs_per_week": 5,
        "long_run_day": "Sun", "quality_day": "Tue", "second_quality": True, "strength_day": "Monday",
        "max_hr": 188, "lthr": 172, "weight_lb": 142, "goal_weight_lb": 136,
        "home_lat": 40.7128, "home_lon": -74.0060,
    }


# ---------------- activities ----------------
def _zone_split(avg_hr: float, dur: float, zones: list) -> dict:
    """Spread time across zones around the average HR."""
    out = {f"z{i}": 0.0 for i in range(1, 6)}
    for z in zones:
        mid = (z["low"] + min(z["high"], 200)) / 2
        w = math.exp(-((mid - avg_hr) / 8.0) ** 2)
        out[f"z{z['zone']}"] = w
    tot = sum(out.values()) or 1
    return {k: round(v / tot * dur, 1) for k, v in out.items()}


def _activity(rng, aid, date, day, cfg, fitness_gain) -> Optional[dict]:
    paces = cfg["paces"]
    zones = cfg["hr_zones"]["zones"]
    km = float(day.get("distance_km") or 0)
    if km <= 0:
        return None
    km = round(km * rng.uniform(0.96, 1.05), 2)
    t = day["type"]
    easy = (paces["easy"]["sec_low"] + paces["easy"]["sec_high"]) / 2
    slow = 1 + 0.035 * (1 - fitness_gain)            # a touch slower earlier in the block
    if t == "quality":
        work = paces.get(day.get("pace_key") or "threshold", paces["threshold"]).get("sec", easy)
        pace = (work * 0.45 + easy * 0.55) * slow
        hr = 158 + rng.uniform(-3, 4)
        name = day["title"]
        aero, anaer = rng.uniform(3.4, 4.2), rng.uniform(1.0, 2.6)
    elif t == "long":
        pace = easy * 0.98 * slow
        hr = 148 + rng.uniform(-3, 4)
        name = day["title"]
        aero, anaer = rng.uniform(3.3, 4.0), rng.uniform(0, 0.6)
    elif t == "race":
        pace = paces["hmp"]["sec"] * 1.005
        hr = 172 + rng.uniform(-2, 2)
        name = "Riverside Half Marathon"
        aero, anaer = 4.8, 2.0
    else:
        pace = easy * rng.uniform(0.98, 1.03) * slow
        hr = 141 + rng.uniform(-4, 4)
        name = "Easy run" if "strides" not in day.get("title", "").lower() else "Easy + strides"
        aero, anaer = rng.uniform(2.4, 3.1), rng.uniform(0, 0.5)
    dur = km * pace
    hour = rng.choice([6, 6, 7, 7, 12, 17, 18])
    start = f"{date} {hour:02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}"
    hrz = _zone_split(hr, dur, zones)
    vig = round((hrz["z3"] + hrz["z4"] + hrz["z5"]) / 60)
    return {
        "id": aid, "name": name, "type": "running", "event_type": "race" if t == "race" else "uncategorized",
        "start": start, "date": date, "distance_km": km, "duration_s": round(dur + rng.uniform(5, 60), 1),
        "moving_s": round(dur, 1), "avg_hr": round(hr), "max_hr": round(hr + rng.uniform(10, 20)),
        "avg_pace_s_per_km": round(pace, 1), "calories": round(km * 62), "elev_gain_m": round(km * rng.uniform(4, 11)),
        "vo2max": None, "training_load": round(dur / 60 * (1.2 + (hr - 130) / 25), 1),
        "aerobic_te": round(aero, 1), "anaerobic_te": round(anaer, 1),
        "avg_cadence": round(rng.uniform(168, 178), 1), "avg_vo": round(rng.uniform(8.2, 9.0), 2),
        "avg_gct": round(rng.uniform(238, 255), 1), "avg_vratio": round(rng.uniform(7.0, 7.8), 2),
        "avg_gct_balance": round(rng.uniform(49.6, 50.6), 1), "avg_stride": round(1000 / (pace / 60) / 172 * 100, 1),
        "avg_power": round(285 + (easy - pace) * 0.9), "moderate_min": round((hrz["z1"] + hrz["z2"]) / 60 * 0.2),
        "vigorous_min": vig, "hr_zones_s": hrz,
    }


def _pre_plan_day(rng, date: dt.date) -> Optional[dict]:
    """Before the plan starts: four easy-ish runs a week."""
    wd = date.weekday()
    if wd in (0, 4, 5):
        return None
    km = {1: 8, 2: 6, 3: 7, 6: 13}[wd]
    return {"type": "long" if wd == 6 else "easy", "distance_km": km,
            "title": "Long run" if wd == 6 else "Easy run", "pace_key": "easy"}


# ---------------- snapshot ----------------
def build_snapshot(today: dt.date, cfg: dict, plan: dict) -> dict[str, Any]:
    rng = random.Random(SEED)
    start = today - dt.timedelta(days=HISTORY_DAYS)
    plan_days = {d["date"]: d for w in plan["weeks"] for d in w["days"]}
    plan_start = dt.date.fromisoformat(plan["weeks"][0]["start_date"])

    acts, strength = [], []
    aid = 9_000_000_000
    load_by_day: dict[str, float] = {}
    for i in range(HISTORY_DAYS + 1):
        d = start + dt.timedelta(days=i)
        iso = d.isoformat()
        if d >= today and not (d == today):
            break
        day = plan_days.get(iso) if d >= plan_start else _pre_plan_day(rng, d)
        if d == today:
            continue                                   # today's session not done yet
        if not day:
            continue
        if day.get("type") in ("easy",) and rng.random() < 0.07:
            continue                                   # occasional missed day
        gain = min(1.0, max(0.0, (d - start).days / HISTORY_DAYS))
        a = _activity(rng, aid, iso, day, cfg, gain) if day.get("type") != "rest" else None
        aid += 1
        if a:
            acts.append(a)
            load_by_day[iso] = load_by_day.get(iso, 0) + a["training_load"]
        if day.get("strength") and rng.random() < 0.85:
            strength.append({"id": aid, "name": "Strength", "type": "strength_training", "event_type": "uncategorized",
                             "start": f"{iso} 18:30:00", "date": iso, "distance_km": 0.0, "duration_s": 2700.0,
                             "moving_s": 2700.0, "avg_hr": 108, "max_hr": 141, "avg_pace_s_per_km": None,
                             "calories": 260, "elev_gain_m": None, "vo2max": None, "training_load": 22.0,
                             "aerobic_te": 1.4, "anaerobic_te": 0.8, "moderate_min": 30, "vigorous_min": 4,
                             "hr_zones_s": {"z1": 2400.0, "z2": 300.0, "z3": 0.0, "z4": 0.0, "z5": 0.0}})
            aid += 1

    # a tune-up 10K four weeks ago (Saturday)
    tune = today - dt.timedelta(days=today.weekday() + 23)
    tenk_s = round(plangen.race_time(10000, plangen.vdot_from(21097.5, cfg["goals"]["A"]["seconds"]) - 1.2))
    acts = [a for a in acts if a["date"] != tune.isoformat()]
    acts.append({**_activity(rng, aid, tune.isoformat(), {"type": "race", "distance_km": 10, "title": "Race"}, cfg, 0.6),
                 "name": "Harbour 10K", "distance_km": 10.02, "moving_s": tenk_s, "duration_s": tenk_s + 4,
                 "avg_pace_s_per_km": round(tenk_s / 10.02, 1), "avg_hr": 176, "max_hr": 186, "event_type": "race"})
    acts.sort(key=lambda a: a["start"])
    all_acts = sorted(acts + strength, key=lambda a: a["start"])

    # weekly volume
    weeks: dict[str, dict] = {}
    for a in acts:
        d = dt.date.fromisoformat(a["date"])
        ws = (d - dt.timedelta(days=d.weekday())).isoformat()
        w = weeks.setdefault(ws, {"week_start": ws, "km": 0.0, "runs": 0, "moving_s": 0.0, "moderate_min": 0,
                                  "vigorous_min": 0, "intensity_min": 0,
                                  "hr_zones_s": {f"z{i}": 0.0 for i in range(1, 6)}})
        w["km"] = round(w["km"] + a["distance_km"], 1)
        w["runs"] += 1
        w["moving_s"] += a["moving_s"]
        w["moderate_min"] += a["moderate_min"]
        w["vigorous_min"] += a["vigorous_min"]
        w["intensity_min"] = w["moderate_min"] + 2 * w["vigorous_min"]
        for k, v in a["hr_zones_s"].items():
            w["hr_zones_s"][k] = round(w["hr_zones_s"][k] + v)
    weekly = [weeks[k] for k in sorted(weeks)][-14:]
    zone_tot = {f"z{i}": round(sum(a["hr_zones_s"][f"z{i}"] for a in acts), 1) for i in range(1, 6)}

    # fitness trend: VDOT drifts up ~1.5 over the 90 days toward the goal
    goal_vdot = plangen.vdot_from(21097.5, cfg["goals"]["A"]["seconds"])
    def vdot_on(d: dt.date) -> float:
        return goal_vdot - 2.6 + 1.6 * min(1.0, (d - start).days / HISTORY_DAYS)

    pred_hist = []
    for i in range(0, HISTORY_DAYS + 1, 2):
        d = start + dt.timedelta(days=i)
        v = vdot_on(d) + rng.uniform(-0.15, 0.15)
        pred_hist.append({"date": d.isoformat(), "sec_5k": round(plangen.race_time(5000, v)),
                          "sec_10k": round(plangen.race_time(10000, v)),
                          "sec_half": round(plangen.race_time(21097.5, v)),
                          "sec_marathon": round(plangen.race_time(42195, v) * 1.02)})
    rp = dict(pred_hist[-1], date=today.isoformat())
    vo2 = [{"date": (start + dt.timedelta(days=i)).isoformat(),
            "vo2max": round(vdot_on(start + dt.timedelta(days=i)) + 3.0, 1)} for i in range(0, HISTORY_DAYS + 1, 14)]
    for a in acts:
        a["vo2max"] = round(vdot_on(dt.date.fromisoformat(a["date"])) + 3.0)

    race_history = [{"dist": "10k", "date": tune.isoformat(), "seconds": tenk_s, "km": 10.0,
                     "event": "race", "name": "Harbour 10K"},
                    {"dist": "half", "date": (today - dt.timedelta(days=210)).isoformat(),
                     "seconds": cfg["goals"]["A"]["seconds"] + 330, "km": 21.1, "event": "race",
                     "name": "Spring Half"},
                    {"dist": "5k", "date": (today - dt.timedelta(days=150)).isoformat(),
                     "seconds": round(plangen.race_time(5000, goal_vdot - 2.0)), "km": 5.0, "event": "race",
                     "name": "Parkrun"}]

    # acute/chronic load
    def load_avg(days_back: int) -> float:
        tot = 0.0
        for i in range(days_back):
            tot += load_by_day.get((today - dt.timedelta(days=i + 1)).isoformat(), 0)
        return tot
    acute = round(load_avg(7))
    chronic = round(load_avg(28) / 4)
    acwr = round(acute / chronic, 2) if chronic else 1.0

    # recovery signals: worse the day after big loads
    sleep, hrv, bb, rhr, ready, wellness, weight = [], [], [], [], [], [], []
    hrv_base = 63
    kg = 142 / 2.20462 + 1.3
    for i in range(HISTORY_DAYS + 1):
        d = start + dt.timedelta(days=i)
        iso = d.isoformat()
        prev_load = load_by_day.get((d - dt.timedelta(days=1)).isoformat(), 0)
        strain = min(1.0, prev_load / 220)
        if i >= HISTORY_DAYS - 1:
            strain *= 0.3                                  # a good night before 'today'
        weekend = d.weekday() >= 5
        total = int((7.4 + rng.uniform(-0.8, 0.7) + (0.4 if weekend else 0) - 0.3 * strain) * 3600)
        deep = int(total * rng.uniform(0.14, 0.21))
        rem = int(total * rng.uniform(0.19, 0.25))
        awake = int(rng.uniform(300, 1800))
        light = total - deep - rem
        score = int(max(48, min(95, 78 + (total - 27000) / 900 - 10 * strain + rng.uniform(-6, 5))))
        q = "EXCELLENT" if score >= 90 else "GOOD" if score >= 80 else "FAIR" if score >= 60 else "POOR"
        night_hrv = round(hrv_base + 6 * math.sin(i / 9) - 9 * strain + rng.uniform(-5, 5))
        rest_hr = round(50 + 3 * strain + rng.uniform(-1.5, 1.5))
        bed = dt.datetime.combine(d - dt.timedelta(days=1), dt.time(22, 40)) + dt.timedelta(minutes=rng.randint(-40, 50))
        sleep.append({
            "date": iso, "total_s": total, "deep_s": deep, "light_s": light, "rem_s": rem, "awake_s": awake,
            "awake_count": rng.randint(0, 4), "start_local_ms": int(bed.replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
            "end_local_ms": int((bed + dt.timedelta(seconds=total + awake)).replace(tzinfo=dt.timezone.utc).timestamp() * 1000),
            "need_min": 480, "need_baseline_min": 470, "score": score, "score_q": q,
            "components": {
                "duration": {"value": None, "q": q, "lo": 25200.0, "hi": 32400.0},
                "deep": {"value": round(100 * deep / total), "q": "GOOD", "lo": 16.0, "hi": 33.0},
                "rem": {"value": round(100 * rem / total), "q": "GOOD", "lo": 21.0, "hi": 31.0},
                "light": {"value": round(100 * light / total), "q": "GOOD", "lo": 30.0, "hi": 64.0},
                "restlessness": {"value": None, "q": "GOOD", "lo": 0.0, "hi": 5.0},
                "awake": {"value": None, "q": "GOOD", "lo": 0.0, "hi": 1.0},
                "stress": {"value": None, "q": "FAIR" if strain > 0.5 else "GOOD", "lo": 0.0, "hi": 15.0}},
            "resting_hr": rest_hr, "avg_hr": rest_hr + 6.0, "avg_overnight_hrv": float(night_hrv),
            "avg_stress": round(16 + 10 * strain + rng.uniform(-3, 3), 1), "avg_respiration": round(rng.uniform(13.5, 15), 1),
            "avg_spo2": round(rng.uniform(95, 97), 1), "lowest_spo2": rng.randint(86, 91),
            "restless_moments": rng.randint(25, 70), "bb_change": int(45 + (score - 70) * 0.9),
            "feedback": "POSITIVE_LONG_AND_REFRESHING" if score >= 70 else "NEGATIVE_LONG_BUT_NOT_RESTORATIVE",
            "insight": "NONE"})
        weekly_avg = round(sum((s["avg_overnight_hrv"] for s in sleep[-7:])) / len(sleep[-7:]))
        status = "BALANCED" if weekly_avg >= 52 else "UNBALANCED" if weekly_avg >= 46 else "LOW"
        hrv.append({"date": iso, "last_night_avg": night_hrv, "weekly_avg": weekly_avg, "status": status,
                    "balanced_low": 52, "balanced_upper": 72})
        hi_bb = min(100, 40 + sleep[-1]["bb_change"])
        drained = int(rng.uniform(45, 70) + 25 * min(1, load_by_day.get(iso, 0) / 160))
        bb.append({"date": iso, "charged": sleep[-1]["bb_change"], "drained": drained, "high": hi_bb,
                   "low": max(5, hi_bb - drained), "last": max(5, hi_bb - drained + 8)})
        rhr.append({"date": iso, "rhr": float(rest_hr)})
        rscore = int(max(15, min(98, 0.55 * score + (night_hrv - 50) * 1.4 + 22 - 14 * strain + rng.uniform(-6, 6))))
        level = "HIGH" if rscore >= 75 else "MODERATE" if rscore >= 50 else "LOW" if rscore >= 25 else "POOR"
        ready.append({"date": iso, "score": rscore, "level": level,
                      "feedback": "GOOD_RECOVERY" if rscore >= 60 else "LISTEN_TO_YOUR_BODY",
                      "sleep_score": score, "recovery_pct": int(min(100, 55 + rscore / 2)),
                      "recovery_time_min": int(max(0, 1500 * strain)), "acwr_pct": int(acwr * 100) // 2,
                      "acute_load": acute, "hrv_weekly_avg": weekly_avg, "valid_sleep": True,
                      "timestamp": f"{iso}T07:05:00.0"})
        day_load = load_by_day.get(iso, 0)
        steps = int(rng.uniform(6500, 11500) + day_load * 40)
        hi_act = int(min(9000, day_load * 22)) if day_load else int(rng.uniform(0, 400))
        rest_s = int(rng.uniform(19000, 25000))
        low_s, med_s, hi_s = int(rng.uniform(9000, 16000)), int(rng.uniform(3000, 8000)), int(rng.uniform(300, 2600) * (1.4 if not weekend else 0.6))
        wellness.append({"date": iso, "total_steps": steps, "distance_m": int(steps * 0.76),
                         "highly_active_s": hi_act, "active_s": int(rng.uniform(2400, 6000)),
                         "sedentary_s": int(rng.uniform(25000, 36000)), "sleeping_s": total,
                         "moderate_min": rng.randint(0, 25), "vigorous_min": int(hi_act / 60),
                         "floors_up": round(rng.uniform(4, 16), 1),
                         "avg_stress": int(22 + rng.uniform(-6, 9)), "max_stress": rng.randint(70, 96),
                         "rest_stress_s": rest_s, "low_stress_s": low_s, "med_stress_s": med_s, "high_stress_s": hi_s,
                         "activity_stress_s": hi_act, "uncat_stress_s": int(rng.uniform(600, 2400)),
                         "total_stress_s": rest_s + low_s + med_s + hi_s + hi_act,
                         "body_battery_charged": bb[-1]["charged"], "body_battery_drained": drained})
        if i % 3 == 0:
            kg -= rng.uniform(-0.15, 0.25) * 0.5
            weight.append({"date": iso, "kg": round(kg, 1)})

    lr = ready[-1]
    return {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "today": today.isoformat(),
        "demo": True,
        "activities": acts,
        "all_activities": all_acts,
        "weekly_volume": weekly,
        "hr_zone_totals_s": zone_tot,
        "race_predictions": rp,
        "race_prediction_history": pred_hist,
        "race_history": race_history,
        "training": {"as_of": today.isoformat(), "vo2max": vo2[-1]["vo2max"], "vo2max_precise": vo2[-1]["vo2max"],
                     "status": 7, "status_feedback": "PRODUCTIVE_5", "fitness_trend": 3,
                     "acute_load": acute, "chronic_load": chronic, "acwr": acwr,
                     "acwr_status": "OPTIMAL" if 0.8 <= acwr <= 1.3 else "HIGH" if acwr > 1.3 else "LOW",
                     "acwr_percent": int(min(100, acwr * 40)), "chronic_min": round(chronic * 0.8, 1),
                     "chronic_max": round(chronic * 1.5, 1),
                     "device": {"deviceId": 0, "imageURL": "", "deviceName": "Demo watch", "category": 0},
                     "load_balance": {"feedback": "BALANCED", "aerobic_low": 640.0, "aerobic_high": 410.0,
                                      "anaerobic": 150.0, "aerobic_low_target": [380, 820],
                                      "aerobic_high_target": [330, 700], "anaerobic_target": [110, 340]}},
        "vo2max_trend": vo2,
        "weight": weight,
        "lactate_threshold": {"hr": cfg["hr_zones"]["lthr"], "pace_s_per_km": cfg["paces"]["threshold"]["sec"],
                              "speed_mps": round(1000 / cfg["paces"]["threshold"]["sec"], 3),
                              "measured_at": (today - dt.timedelta(days=12)).isoformat()},
        "sleep": sleep[-42:],
        "hrv": hrv[-42:],
        "hrv_baseline": {"low": 52, "upper": 72},
        "body_battery": bb[-28:],
        "rhr_trend": rhr[-42:],
        "readiness_trend": ready[-14:],
        "latest_readiness": lr,
        "wellness_daily": wellness[-30:],
        "errors": [],
    }


def weather() -> dict:
    """Fixed mild conditions so demo screenshots don't depend on live weather."""
    return {"has_data": True, "lat": 40.71, "lon": -74.01, "timezone": "America/New_York",
            "as_of": dt.datetime.now().strftime("%Y-%m-%dT07:00"), "temp_c": 11.0, "humidity_pct": 62,
            "apparent_c": 10.0, "heat_index_c": 11.0, "wind_kmh": 9.0, "gust_kmh": 16.0, "precip_mm": 0.0,
            "is_day": True, "tier": "none", "pace_adjust_s_per_km": 0, "hr_drift_bpm": 0,
            "read": "Cool, calm morning (11 °C, 62% RH). Run to pace — no heat adjustment needed."}


# ---------------- per-activity on-demand data ----------------
class FakeClient:
    """Stands in for garminconnect.Garmin for the few endpoints that call it directly."""

    def get_training_status(self, day: str) -> dict:
        d = dt.date.fromisoformat(day)
        pct = max(0, min(100, 35 + 20 * math.sin(d.toordinal() / 30)))
        return {"mostRecentVO2Max": {"heatAltitudeAcclimation": {
            "heatAcclimationPercentage": round(pct), "heatTrend": "ACCLIMATIZING", "altitudeAcclimation": 0}}}

    def get_running_tolerance(self, start: str, end: str) -> list:
        snap = cache.load() or {}
        out = []
        for w in snap.get("weekly_volume") or []:
            ws = dt.date.fromisoformat(w["week_start"])
            load = w["km"] * 34
            out.append({"calendarDate": ws.isoformat(), "startOfWeek": ws.isoformat(),
                        "endOfWeek": (ws + dt.timedelta(days=6)).isoformat(),
                        "totalImpactLoad": load, "tolerance": 2100, "totalDistance": w["km"] * 1000})
        return out


class DemoGarmin:
    """Drop-in for garmin_client.GarminData that never touches the network."""
    tokenstore = "(demo mode)"

    def client(self):
        return FakeClient()

    def _act(self, activity_id):
        snap = cache.load() or {}
        return next((a for a in snap.get("activities", []) if a.get("id") == activity_id), None)

    def activity_stream(self, activity_id) -> dict:
        a = self._act(activity_id)
        if not a:
            return {"has_data": False}
        rng = random.Random(activity_id)
        dur = a["moving_s"]
        n = 180
        base = a["avg_pace_s_per_km"]
        series, stam = [], 100.0
        is_q = "×" in (a.get("name") or "") or "Threshold" in (a.get("name") or "")
        for i in range(n):
            t = dur * i / (n - 1)
            frac = i / (n - 1)
            if is_q and 0.15 < frac < 0.85:
                on = int(frac * 14) % 2 == 0
                pace = base * (0.86 if on else 1.12)
                hr = (a["avg_hr"] + 10) if on else (a["avg_hr"] - 6)
            else:
                pace = base * (1.04 if frac < 0.1 else 1.0) * rng.uniform(0.98, 1.02)
                hr = a["avg_hr"] - 12 * math.exp(-frac * 12) + 5 * frac
            stam = max(5.0, stam - (100 / n) * (0.55 if not is_q else 0.8))
            series.append({"t": round(t), "pace_s": round(pace, 1), "gap_s": round(pace * rng.uniform(0.99, 1.0), 1),
                           "hr": round(hr + rng.uniform(-2, 2)), "power": None, "stamina": round(stam, 1),
                           "potential": round(min(100, stam + 12), 1), "impact": None})
        return {"has_data": True, "series": series, "duration": series[-1]["t"], "impact": None}

    def activity_splits(self, activity_id) -> list:
        a = self._act(activity_id)
        if not a:
            return []
        rng = random.Random(activity_id + 7)
        out, t = [], 0.0
        km_total = a["distance_km"]
        laps = int(km_total) + (1 if km_total % 1 > 0.05 else 0)
        for i in range(laps):
            dist = min(1.0, km_total - i)
            pace = a["avg_pace_s_per_km"] * rng.uniform(0.97, 1.03)
            dur = dist * pace
            out.append({"lap": i + 1, "t0": round(t), "t1": round(t + dur), "distance_km": round(dist, 3),
                        "duration_s": round(dur, 1), "pace_s": round(pace, 1), "gap_s": round(pace * 0.995, 1),
                        "avg_hr": round(a["avg_hr"] + rng.uniform(-4, 4)), "max_hr": round(a["avg_hr"] + 8),
                        "elev_gain": rng.randint(0, 12), "elev_loss": rng.randint(0, 12), "elev_net": 0,
                        "intensity": "active"})
            t += dur
        return out

    def activity_feel(self, activity_id) -> dict:
        rng = random.Random(activity_id + 3)
        return {"feel": rng.choice([50, 75, 75, 100]), "rpe": rng.choice([30, 40, 50, 70])}

    def build_snapshot(self, today: Optional[dt.date] = None) -> dict:
        today = today or dt.date.today()
        return build_snapshot(today, planlib.load_config(), planlib.load_plan())


def ensure_demo_data(today: dt.date) -> None:
    """(Re)generate the demo athlete's config, plan and snapshot under demo-data/."""
    cfg, plan = plangen.generate(demo_profile(today), today)
    plangen.save(cfg, plan)
    cache.save(build_snapshot(today, cfg, plan))
