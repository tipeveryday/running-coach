"""Garmin data layer: token-based login, raw fetch, and normalization into a
dashboard snapshot. Uses python-garminconnect directly (no email/password) so
the app can refresh on its own from the saved token session in ~/.garminconnect.
"""
from __future__ import annotations
import os
import datetime as dt
from collections import defaultdict
from typing import Any, Optional

TOKENSTORE = os.environ.get("GARMINTOKENS", os.path.expanduser("~/.garminconnect"))

# How much history to pull for the trend charts.
HISTORY_DAYS = 90


def _iso(d: dt.date) -> str:
    return d.isoformat()


def _first_device_value(dto_map: dict) -> Optional[dict]:
    """Garmin nests per-device dicts under a dynamic deviceId key."""
    if not isinstance(dto_map, dict):
        return None
    for v in dto_map.values():
        if isinstance(v, dict):
            return v
    return None


def _week_start(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())  # Monday


def _safe(fn, *a, **k):
    try:
        return fn(*a, **k)
    except Exception as e:  # noqa: BLE001 — degrade gracefully, record the reason
        return {"__error__": f"{type(e).__name__}: {e}"}


class GarminData:
    def __init__(self, tokenstore: str = TOKENSTORE):
        self.tokenstore = tokenstore
        self._client = None

    def client(self):
        # Dev-only: RC_OFFLINE=1 serves the cached snapshot and never logs in to
        # Garmin (repeated logins get the IP rate-limited). Off unless set.
        if os.environ.get("RC_OFFLINE") == "1":
            raise RuntimeError("Garmin is disabled (RC_OFFLINE=1): serving cached data only")
        if self._client is None:
            from garminconnect import Garmin
            g = Garmin()
            g.login(self.tokenstore)
            self._client = g
        return self._client

    # ---------- normalizers ----------
    @staticmethod
    def _norm_activity(a: dict) -> dict:
        dist = a.get("distance") or 0.0
        dur = a.get("duration") or 0.0
        spd = a.get("averageSpeed") or 0.0
        pace_s_per_km = (1000.0 / spd) if spd else None
        zones = {f"z{i}": (a.get(f"hrTimeInZone_{i}") or 0.0) for i in range(1, 6)}
        return {
            "id": a.get("activityId"),
            "name": a.get("activityName"),
            "type": (a.get("activityType") or {}).get("typeKey"),
            "event_type": (a.get("eventType") or {}).get("typeKey"),
            "start": a.get("startTimeLocal"),
            "date": (a.get("startTimeLocal") or "")[:10],
            "distance_km": round(dist / 1000.0, 3),
            "duration_s": round(dur, 1),
            "moving_s": round(a.get("movingDuration") or dur, 1),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "avg_pace_s_per_km": round(pace_s_per_km, 1) if pace_s_per_km else None,
            "calories": a.get("calories"),
            "elev_gain_m": a.get("elevationGain"),
            "vo2max": a.get("vO2MaxValue"),
            "training_load": a.get("activityTrainingLoad"),
            "aerobic_te": a.get("aerobicTrainingEffect"),
            "anaerobic_te": a.get("anaerobicTrainingEffect"),
            "avg_cadence": a.get("averageRunningCadenceInStepsPerMinute"),
            "avg_vo": a.get("avgVerticalOscillation"),
            "avg_gct": a.get("avgGroundContactTime"),
            "avg_vratio": a.get("avgVerticalRatio"),
            "avg_gct_balance": a.get("avgGroundContactBalance"),  # L/R %, chest-strap only (HRM-Tri/Pro/Run)
            "avg_stride": a.get("avgStrideLength"),
            "avg_power": a.get("avgPower"),
            "moderate_min": a.get("moderateIntensityMinutes") or 0,
            "vigorous_min": a.get("vigorousIntensityMinutes") or 0,
            "hr_zones_s": zones,
        }

    @staticmethod
    def _is_run(a: dict) -> bool:
        t = (a.get("type") or "")
        return "run" in t  # running, trail_running, treadmill_running, etc.

    # ---------- on-demand activity time-series (for the workout-phase chart) ----------
    def activity_stream(self, activity_id) -> dict[str, Any]:
        g = self.client()
        det = getattr(g, "get_activity_details", None)
        if not callable(det):
            return {"has_data": False, "error": "details unavailable"}
        try:
            d = det(activity_id, maxchart=1000, maxpoly=1000)
        except TypeError:
            d = det(activity_id)
        except Exception as e:  # noqa: BLE001
            return {"has_data": False, "error": f"{type(e).__name__}: {e}"}
        md = d.get("metricDescriptors") or []
        rows = d.get("activityDetailMetrics") or []
        if not md or not rows:
            return {"has_data": False}
        idx = {m.get("key"): m.get("metricsIndex") for m in md}

        def gv(vals, key):
            i = idx.get(key)
            if i is None or i >= len(vals):
                return None
            return vals[i]

        tkey = next((k for k in ("sumDuration", "sumElapsedDuration", "sumMovingDuration") if k in idx), None)
        series = []
        for r in rows:
            vals = r.get("metrics") or []
            t = gv(vals, tkey)
            if t is None:
                continue
            spd = gv(vals, "directSpeed")
            gspd = gv(vals, "directGradeAdjustedSpeed")
            hr = gv(vals, "directHeartRate")
            pwr = gv(vals, "directPower")
            stam = gv(vals, "directAvailableStamina")
            pot = gv(vals, "directPotentialStamina")
            imp = gv(vals, "directImpactLoadFactor")  # per-stride impact (newer watches only)
            series.append({
                "t": round(t),
                "pace_s": round(1000.0 / spd, 1) if spd and spd > 0.3 else None,
                "gap_s": round(1000.0 / gspd, 1) if gspd and gspd > 0.3 else None,
                "hr": round(hr) if hr else None,
                "power": round(pwr) if pwr and pwr >= 150 else None,
                "stamina": round(stam, 1) if stam is not None else None,
                "potential": round(pot, 1) if pot is not None else None,
                "impact": round(imp, 2) if imp is not None else None,
            })
        imps = [p["impact"] for p in series if p.get("impact") is not None]
        impact = None
        if imps:
            impact = {"avg": round(sum(imps) / len(imps), 2), "peak": round(max(imps), 2)}
        if len(series) > 200:                         # downsample to keep it light
            step = len(series) // 200 + 1
            series = series[::step]
        return {"has_data": bool(series), "series": series,
                "duration": series[-1]["t"] if series else 0, "impact": impact}

    # ---------- subjective post-run tags (Garmin "Feel" + "Perceived Effort") ----------
    def activity_feel(self, activity_id) -> dict:
        """Garmin stores the athlete's own post-run tags on the activity:
        directWorkoutFeel (0–100, how strong you felt) and directWorkoutRpe
        (0–100, how hard it felt). Only present when you logged them on the watch."""
        g = self.client()
        fn = getattr(g, "get_activity", None)
        if not callable(fn):
            return {}
        try:
            d = fn(activity_id)
        except Exception:  # noqa: BLE001
            return {}
        s = (d or {}).get("summaryDTO") or {}
        return {"feel": s.get("directWorkoutFeel"), "rpe": s.get("directWorkoutRpe")}

    # ---------- per-lap splits (for the activity drill-in) ----------
    def activity_splits(self, activity_id) -> list[dict]:
        g = self.client()
        fn = getattr(g, "get_activity_splits", None)
        if not callable(fn):
            return []
        try:
            d = fn(activity_id)
        except Exception:  # noqa: BLE001
            return []
        laps = (d or {}).get("lapDTOs") or []
        out, t = [], 0.0
        for i, lap in enumerate(laps, 1):
            dur = lap.get("duration") or 0.0
            dist = lap.get("distance") or 0.0
            spd = lap.get("averageSpeed") or 0.0
            gspd = lap.get("avgGradeAdjustedSpeed") or 0.0
            eg = lap.get("elevationGain") or 0.0
            el_ = lap.get("elevationLoss") or 0.0
            t0 = t
            t += dur
            out.append({
                "lap": i,
                "t0": round(t0), "t1": round(t),
                "distance_km": round(dist / 1000.0, 3),
                "duration_s": round(dur, 1),
                "pace_s": round(1000.0 / spd, 1) if spd and spd > 0.3 else None,
                "gap_s": round(1000.0 / gspd, 1) if gspd and gspd > 0.3 else None,
                "avg_hr": round(lap["averageHR"]) if lap.get("averageHR") else None,
                "max_hr": round(lap["maxHR"]) if lap.get("maxHR") else None,
                "elev_gain": round(eg), "elev_loss": round(el_), "elev_net": round(eg - el_),
                "intensity": (lap.get("intensityType") or "").lower() or None,
            })
        return out

    # ---------- snapshot ----------
    def build_snapshot(self, today: Optional[dt.date] = None) -> dict[str, Any]:
        today = today or dt.date.today()
        start = today - dt.timedelta(days=HISTORY_DAYS)
        g = self.client()
        errors: list[str] = []

        def track(label, res):
            if isinstance(res, dict) and "__error__" in res:
                errors.append(f"{label}: {res['__error__']}")
                return None
            return res

        # ---- Activities (window) ----
        raw_acts = track("activities_by_date",
                         _safe(g.get_activities_by_date, _iso(start), _iso(today)))
        activities = []
        if isinstance(raw_acts, list):
            activities = [self._norm_activity(a) for a in raw_acts]
        runs = [a for a in activities if self._is_run(a)]
        runs.sort(key=lambda a: a["start"] or "")

        # Weekly running volume (Mon-anchored) across the window
        vol = defaultdict(lambda: {"km": 0.0, "runs": 0, "hr_zones_s": defaultdict(float),
                                    "moving_s": 0.0, "moderate_min": 0.0, "vigorous_min": 0.0})
        for r in runs:
            if not r["date"]:
                continue
            ws = _iso(_week_start(dt.date.fromisoformat(r["date"])))
            vol[ws]["km"] += r["distance_km"]
            vol[ws]["runs"] += 1
            vol[ws]["moving_s"] += r["moving_s"] or 0
            vol[ws]["moderate_min"] += r.get("moderate_min") or 0
            vol[ws]["vigorous_min"] += r.get("vigorous_min") or 0
            for z, s in r["hr_zones_s"].items():
                vol[ws]["hr_zones_s"][z] += s
        weekly_volume = []
        for ws in sorted(vol):
            wk = vol[ws]
            weekly_volume.append({
                "week_start": ws,
                "km": round(wk["km"], 1),
                "runs": wk["runs"],
                "moving_s": round(wk["moving_s"], 0),
                "moderate_min": round(wk["moderate_min"]),
                "vigorous_min": round(wk["vigorous_min"]),
                "intensity_min": round(wk["moderate_min"] + wk["vigorous_min"]),
                "hr_zones_s": {z: round(wk["hr_zones_s"][z], 0) for z in
                               ["z1", "z2", "z3", "z4", "z5"]},
            })

        # Aggregate HR-zone distribution across all runs in window
        hr_zone_totals = {f"z{i}": 0.0 for i in range(1, 6)}
        for r in runs:
            for i in range(1, 6):
                hr_zone_totals[f"z{i}"] += r["hr_zones_s"].get(f"z{i}", 0) or 0

        # ---- Race predictions ----
        rp = track("race_predictions", _safe(g.get_race_predictions))
        race_predictions = None
        if isinstance(rp, dict):
            race_predictions = {
                "date": rp.get("calendarDate"),
                "sec_5k": rp.get("time5K"),
                "sec_10k": rp.get("time10K"),
                "sec_half": rp.get("timeHalfMarathon"),
                "sec_marathon": rp.get("timeMarathon"),
            }

        # ---- Race-prediction history (for the VDOT trend) ----
        race_prediction_history = []
        rph = _safe(g.get_race_predictions,
                    startdate=_iso(today - dt.timedelta(days=180)),
                    enddate=_iso(today), _type="daily")
        if isinstance(rph, list):
            for e in rph:
                if e.get("timeHalfMarathon"):
                    race_prediction_history.append({
                        "date": e.get("calendarDate"),
                        "sec_5k": e.get("time5K"),
                        "sec_10k": e.get("time10K"),
                        "sec_half": e.get("timeHalfMarathon"),
                        "sec_marathon": e.get("timeMarathon"),
                    })
            race_prediction_history.sort(key=lambda x: x["date"] or "")

        # ---- Race history (multi-year, for a demonstrated-fitness anchor) ----
        # Scan running for race-distance efforts so the projection can learn from
        # what the athlete has actually raced, not just Garmin's suppressed predictor.
        race_history = []
        try:
            _start = today.replace(year=today.year - 4)
        except ValueError:
            _start = today - dt.timedelta(days=365 * 4)
        rh_acts = _safe(g.get_activities_by_date, _iso(_start), _iso(today), "running")
        _DIST = [("5k", 4.8, 5.3), ("10k", 9.6, 10.4), ("half", 20.7, 21.6), ("marathon", 41.5, 42.8)]
        if isinstance(rh_acts, list):
            for a in rh_acts:
                km = (a.get("distance") or 0) / 1000.0
                dur = a.get("duration") or 0
                if not dur or km < 4.5:
                    continue
                label = next((n for n, lo, hi in _DIST if lo <= km <= hi), None)
                if not label:
                    continue
                race_history.append({
                    "dist": label, "date": (a.get("startTimeLocal") or "")[:10],
                    "seconds": round(dur), "km": round(km, 3),
                    "event": (a.get("eventType") or {}).get("typeKey"),
                    "name": a.get("activityName"),
                })
            race_history.sort(key=lambda x: x["date"] or "")

        # ---- Training status (VO2max, load, ACWR, balance) ----
        def _has_status(x):
            return (isinstance(x, dict)
                    and _first_device_value((x.get("mostRecentTrainingStatus") or {})
                                            .get("latestTrainingStatusData") or {}) is not None)

        ts = track("training_status", _safe(g.get_training_status, _iso(today)))
        # today (or recent days) may be unsynced and return an empty payload; walk
        # back until we find a populated training-status record.
        if not _has_status(ts):
            for back in range(1, 21):
                d = today - dt.timedelta(days=back)
                r = _safe(g.get_training_status, _iso(d))
                if _has_status(r):
                    ts = r
                    break
        training = None
        if isinstance(ts, dict):
            vo2 = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {})
            tstat = _first_device_value(
                (ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {})
            bal = _first_device_value(
                (ts.get("mostRecentTrainingLoadBalance") or {}).get(
                    "metricsTrainingLoadBalanceDTOMap") or {})
            acute = (tstat or {}).get("acuteTrainingLoadDTO") or {}
            training = {
                "as_of": (tstat or {}).get("calendarDate"),
                "vo2max": vo2.get("vo2MaxValue"),
                "vo2max_precise": vo2.get("vo2MaxPreciseValue"),
                "status": (tstat or {}).get("trainingStatus"),
                "status_feedback": (tstat or {}).get("trainingStatusFeedbackPhrase"),
                "fitness_trend": (tstat or {}).get("fitnessTrend"),
                "acute_load": acute.get("dailyTrainingLoadAcute"),
                "chronic_load": acute.get("dailyTrainingLoadChronic"),
                "acwr": acute.get("dailyAcuteChronicWorkloadRatio"),
                "acwr_status": acute.get("acwrStatus"),
                "acwr_percent": acute.get("acwrPercent"),
                "chronic_min": acute.get("minTrainingLoadChronic"),
                "chronic_max": acute.get("maxTrainingLoadChronic"),
                "device": _first_device_value(
                    {d["deviceId"]: d for d in
                     (ts.get("mostRecentTrainingStatus") or {}).get("recordedDevices", [])}
                    ) if (ts.get("mostRecentTrainingStatus") or {}).get("recordedDevices") else None,
            }
            if bal:
                training["load_balance"] = {
                    "feedback": bal.get("trainingBalanceFeedbackPhrase"),
                    "aerobic_low": bal.get("monthlyLoadAerobicLow"),
                    "aerobic_high": bal.get("monthlyLoadAerobicHigh"),
                    "anaerobic": bal.get("monthlyLoadAnaerobic"),
                    "aerobic_low_target": [bal.get("monthlyLoadAerobicLowTargetMin"),
                                            bal.get("monthlyLoadAerobicLowTargetMax")],
                    "aerobic_high_target": [bal.get("monthlyLoadAerobicHighTargetMin"),
                                             bal.get("monthlyLoadAerobicHighTargetMax")],
                    "anaerobic_target": [bal.get("monthlyLoadAnaerobicTargetMin"),
                                          bal.get("monthlyLoadAnaerobicTargetMax")],
                }

        # ---- VO2max trend (sampled ~10 months back; VO2max changes slowly, so a
        # wide window is needed to show a real trend) ----
        vo2_trend = []
        seen = set()
        for wk in range(0, 301, 10):
            d = today - dt.timedelta(days=wk)
            mm = _safe(g.get_max_metrics, _iso(d))
            if isinstance(mm, list) and mm:
                gen = (mm[0] or {}).get("generic") or {}
                val = gen.get("vo2MaxPreciseValue") or gen.get("vo2MaxValue")
                cdate = gen.get("calendarDate")
                if val and cdate and cdate not in seen:
                    seen.add(cdate)
                    vo2_trend.append({"date": cdate, "vo2max": round(val, 1)})
        vo2_trend.sort(key=lambda x: x["date"])

        # ---- Body weight trend (grams → kg) ----
        weight = []
        wi = _safe(g.get_weigh_ins, _iso(today - dt.timedelta(days=365)), _iso(today))
        if isinstance(wi, dict):
            for dws in wi.get("dailyWeightSummaries", []):
                lw = dws.get("latestWeight") or {}
                w = lw.get("weight")
                d = dws.get("summaryDate") or lw.get("calendarDate")
                if w and d:
                    weight.append({"date": d, "kg": round(w / 1000.0, 1)})
            weight.sort(key=lambda x: x["date"] or "")

        # ---- Lactate threshold + FTP (method may not exist in this lib version) ----
        # Garmin nests this under {speed_and_heart_rate: {heartRate, speed, ...},
        # power: {functionalThresholdPower, powerToWeight, ...}}. Older
        # library versions returned a flat dict; handle both.
        lactate = None
        lt_fn = getattr(g, "get_lactate_threshold", None)
        if callable(lt_fn):
            lt = _safe(lt_fn)
            if isinstance(lt, dict) and "__error__" not in lt:
                shr = lt.get("speed_and_heart_rate") if isinstance(lt.get("speed_and_heart_rate"), dict) else lt
                pw = lt.get("power") if isinstance(lt.get("power"), dict) else {}
                spd = (shr.get("lactate_threshold_speed_mps")
                       or shr.get("speed")
                       or shr.get("lactateThresholdSpeed"))
                # Only trust a plausible running threshold speed (2-7 m/s).
                # Garmin sometimes returns a stale/default value out of range;
                # in that case surface HR + FTP and skip the pace display.
                pace = None
                if isinstance(spd, (int, float)) and 2.0 <= spd <= 7.0:
                    pace = round(1000.0 / spd, 1)
                lactate = {
                    "hr": (shr.get("lactate_threshold_heart_rate_bpm")
                           or shr.get("heartRate")
                           or shr.get("lactateThresholdHeartRate")),
                    "pace_s_per_km": pace,
                    "speed_mps": spd if isinstance(spd, (int, float)) else None,
                    "measured_at": (shr.get("calendarDate") or "")[:10] or None,
                    "ftp_watts": pw.get("functionalThresholdPower"),
                    "ftp_wkg": round(pw["powerToWeight"], 2) if isinstance(pw.get("powerToWeight"), (int, float)) else None,
                    "ftp_measured_at": (pw.get("calendarDate") or "")[:10] or None,
                    "ftp_weight_kg": round(pw["weight"], 1) if isinstance(pw.get("weight"), (int, float)) else None,
                    "ftp_origin": pw.get("origin"),
                    "ftp_sport": pw.get("sport"),
                    "ftp_is_stale": pw.get("isStale"),
                }

        # ---- Sleep (per-night over window) ----
        sleep_nights = []
        for back in range(0, 42):
            d = today - dt.timedelta(days=back)
            sd = _safe(g.get_sleep_data, _iso(d))
            if not isinstance(sd, dict):
                continue
            dto = sd.get("dailySleepDTO") or {}
            secs = dto.get("sleepTimeSeconds")
            if not secs:
                continue
            scores = dto.get("sleepScores") or {}
            need = dto.get("sleepNeed") or {}

            def _comp(key):
                c = scores.get(key) or {}
                return {"value": c.get("value"), "q": c.get("qualifierKey"),
                        "lo": c.get("optimalStart"), "hi": c.get("optimalEnd")}

            sleep_nights.append({
                "date": dto.get("calendarDate"),
                "total_s": secs,
                "deep_s": dto.get("deepSleepSeconds"),
                "light_s": dto.get("lightSleepSeconds"),
                "rem_s": dto.get("remSleepSeconds"),
                "awake_s": dto.get("awakeSleepSeconds"),
                "awake_count": dto.get("awakeCount"),
                # timestamps carry the local offset baked in → read with UTC methods
                "start_local_ms": dto.get("sleepStartTimestampLocal"),
                "end_local_ms": dto.get("sleepEndTimestampLocal"),
                # personalized sleep need (minutes)
                "need_min": need.get("actual") or need.get("baseline"),
                "need_baseline_min": need.get("baseline"),
                "score": (scores.get("overall") or {}).get("value"),
                "score_q": (scores.get("overall") or {}).get("qualifierKey"),
                # per-dimension grades + optimal ranges (Garmin)
                "components": {
                    "duration": _comp("totalDuration"),
                    "deep": _comp("deepPercentage"),
                    "rem": _comp("remPercentage"),
                    "light": _comp("lightPercentage"),
                    "restlessness": _comp("restlessness"),
                    "awake": _comp("awakeCount"),
                    "stress": _comp("stress"),
                },
                # overnight physiology
                "resting_hr": sd.get("restingHeartRate"),
                "avg_hr": dto.get("avgHeartRate"),
                "avg_overnight_hrv": sd.get("avgOvernightHrv"),
                "avg_stress": dto.get("avgSleepStress"),
                "avg_respiration": dto.get("averageRespirationValue"),
                "avg_spo2": dto.get("averageSpO2Value"),
                "lowest_spo2": dto.get("lowestSpO2Value"),
                "restless_moments": sd.get("restlessMomentsCount"),
                "bb_change": sd.get("bodyBatteryChange"),
                # Garmin's own coded feedback
                "feedback": dto.get("sleepScoreFeedback"),
                "insight": dto.get("sleepScorePersonalizedInsight"),
            })
        sleep_nights.sort(key=lambda x: x["date"] or "")

        # ---- HRV (per-night summary over window) ----
        hrv_nights = []
        baseline = None
        for back in range(0, 42):
            d = today - dt.timedelta(days=back)
            hv = _safe(g.get_hrv_data, _iso(d))
            if not isinstance(hv, dict):
                continue
            summ = hv.get("hrvSummary") or {}
            last = summ.get("lastNightAvg")
            if last is None:
                continue
            bl = summ.get("baseline") or {}
            hrv_nights.append({
                "date": summ.get("calendarDate"),
                "last_night_avg": last,
                "weekly_avg": summ.get("weeklyAvg"),
                "status": summ.get("status"),
                "balanced_low": bl.get("balancedLow"),
                "balanced_upper": bl.get("balancedUpper"),
            })
            if bl.get("balancedLow") and bl.get("balancedUpper"):
                baseline = {"low": bl["balancedLow"], "upper": bl["balancedUpper"]}
        hrv_nights.sort(key=lambda x: x["date"] or "")

        # ---- Body battery (window; extract daily high/low from timeseries) ----
        body_battery = []
        bb = _safe(g.get_body_battery, _iso(today - dt.timedelta(days=27)), _iso(today))
        if isinstance(bb, list):
            for day in bb:
                if not isinstance(day, dict):
                    continue
                arr = day.get("bodyBatteryValuesArray") or []
                levels = [p[1] for p in arr if isinstance(p, list) and len(p) > 1
                          and isinstance(p[1], (int, float))]
                body_battery.append({
                    "date": day.get("date") or day.get("calendarDate"),
                    "charged": day.get("charged"),
                    "drained": day.get("drained"),
                    "high": max(levels) if levels else None,
                    "low": min(levels) if levels else None,
                    "last": levels[-1] if levels else None,
                })
            body_battery.sort(key=lambda x: x["date"] or "")

        # ---- Resting HR trend (per day over window) ----
        rhr_trend = []
        for back in range(0, 42):
            d = today - dt.timedelta(days=back)
            rr = _safe(g.get_rhr_day, _iso(d))
            if not isinstance(rr, dict):
                continue
            try:
                metrics = (rr.get("allMetrics") or {}).get("metricsMap") or {}
                arr = metrics.get("WELLNESS_RESTING_HEART_RATE") or []
                if arr and arr[0].get("value"):
                    rhr_trend.append({"date": arr[0].get("calendarDate", _iso(d)),
                                      "rhr": arr[0]["value"]})
            except Exception:
                pass
        rhr_trend.sort(key=lambda x: x["date"] or "")

        # ---- Training readiness over ~14 days ----
        def _mk_readiness(rec):
            return {
                "date": rec.get("calendarDate"),
                "score": rec.get("score"),
                "level": rec.get("level"),
                "feedback": rec.get("feedbackShort"),
                "sleep_score": rec.get("sleepScore"),
                "recovery_pct": rec.get("recoveryTimeFactorPercent"),
                "recovery_time_min": rec.get("recoveryTime"),  # minutes until primed for a HARD effort
                "acwr_pct": rec.get("acwrFactorPercent"),
                "acute_load": rec.get("acuteLoad"),
                "hrv_weekly_avg": rec.get("hrvWeeklyAverage"),
                "valid_sleep": rec.get("validSleep"),
                "timestamp": rec.get("timestamp"),
            }

        readiness_trend = []
        latest_readiness = None
        for back in range(0, 14):
            d = today - dt.timedelta(days=back)
            tr = _safe(g.get_training_readiness, _iso(d))
            recs = tr if isinstance(tr, list) else None
            if not recs:
                continue
            recs_sorted = sorted(recs, key=lambda r: r.get("timestamp") or "")
            # the daily trend keeps the morning (post-wakeup) reading for a stable series
            readiness_trend.append(_mk_readiness(recs_sorted[0]))
            if latest_readiness is None:
                # but "current" readiness is the LATEST reading of the most recent day —
                # Garmin re-scores it after activities, so this reflects post-run state
                # rather than the stale morning value.
                latest_readiness = _mk_readiness(recs_sorted[-1])
        readiness_trend.sort(key=lambda x: x["date"] or "")

        # ---- Daily wellness (steps + stress buckets over ~30 days) ----
        # Powers the Recovery-tab time-on-feet and non-activity-stress views.
        # get_user_summary returns per-day rollups including sedentary/active
        # seconds and the four stress-bucket durations (which exclude activity
        # time by design), so we can render time-on-feet and non-activity
        # stress without walking the per-minute stress array.
        wellness_daily = []
        for back in range(0, 30):
            d = today - dt.timedelta(days=back)
            us = _safe(g.get_user_summary, _iso(d))
            if not isinstance(us, dict) or us.get("__error__"):
                continue
            # Some fields are absent when the day has no data — skip those
            if us.get("totalSteps") is None and us.get("averageStressLevel") is None:
                continue
            wellness_daily.append({
                "date": us.get("calendarDate") or _iso(d),
                "total_steps": us.get("totalSteps"),
                "distance_m": us.get("totalDistanceMeters"),
                "highly_active_s": us.get("highlyActiveSeconds"),
                "active_s": us.get("activeSeconds"),
                "sedentary_s": us.get("sedentarySeconds"),
                "sleeping_s": us.get("sleepingSeconds"),
                "moderate_min": us.get("moderateIntensityMinutes"),
                "vigorous_min": us.get("vigorousIntensityMinutes"),
                "floors_up": us.get("floorsAscended"),
                "avg_stress": us.get("averageStressLevel"),
                "max_stress": us.get("maxStressLevel"),
                # Stress bucket durations are seconds while awake and not in
                # an activity — Garmin's activity bucket is tracked separately.
                "rest_stress_s": us.get("restStressDuration"),
                "low_stress_s": us.get("lowStressDuration"),
                "med_stress_s": us.get("mediumStressDuration"),
                "high_stress_s": us.get("highStressDuration"),
                "activity_stress_s": us.get("activityStressDuration"),
                "uncat_stress_s": us.get("uncategorizedStressDuration"),
                "total_stress_s": us.get("totalStressDuration"),
                "body_battery_charged": us.get("bodyBatteryChargedValue"),
                "body_battery_drained": us.get("bodyBatteryDrainedValue"),
            })
        wellness_daily.sort(key=lambda x: x["date"] or "")

        snapshot = {
            "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
            "today": _iso(today),
            "activities": runs,
            "all_activities": activities,
            "weekly_volume": weekly_volume,
            "hr_zone_totals_s": hr_zone_totals,
            "race_predictions": race_predictions,
            "race_prediction_history": race_prediction_history,
            "race_history": race_history,
            "training": training,
            "vo2max_trend": vo2_trend,
            "weight": weight,
            "lactate_threshold": lactate,
            "sleep": sleep_nights,
            "hrv": hrv_nights,
            "hrv_baseline": baseline,
            "body_battery": body_battery,
            "rhr_trend": rhr_trend,
            "readiness_trend": readiness_trend,
            "latest_readiness": latest_readiness,
            "wellness_daily": wellness_daily,
            "errors": errors,
        }
        return snapshot
