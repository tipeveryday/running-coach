# -*- coding: utf-8 -*-
"""Adaptive coaching (beta).

Turns rolling athlete state (readiness / HRV / sleep / missed workouts) into
concrete, accept-or-reject plan-change recommendations. Every accept and reject
feeds a learning ledger so rules the user consistently rejects get suppressed
and rules they consistently accept become more assertive.

Design constraints:
  * Nothing is applied silently. A rec surfaces in the UI; the user accepts (and
    the existing plan-mutation routines run) or rejects with a reason.
  * All state lives in the same persistent cache dir as the Garmin snapshot
    (under the data folder, so it survives restarts).
  * Deterministic rec ids keyed by (rule, target_date) so re-running the
    collector never duplicates a rec the user already decided on.
  * Rules are cheap to add: subclass Rule, register it once, done.

Storage layout (cache/adaptive_state.json):
  {
    "recs": {rec_id: rec_dict, ...},          # every rec ever generated
    "weights": {rule_name: float, ...},       # learned rule confidences
    "feedback": [{rule, decision, reason, at, context}, ...],
  }
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import uuid
from dataclasses import dataclass, asdict, field
from typing import Any, Callable, Optional

import cache
import planlib


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------
# State lives in a local JSON file under cache/.
STATE_FILE = os.path.join(cache.CACHE_DIR, "adaptive_state.json")


DEFAULT_STATE = {"recs": {}, "weights": {}, "feedback": []}

# Learning tuning: how much a single accept/reject shifts a rule's weight, and
# the floor a rule can decay to before it stops firing on its own signal.
ACCEPT_STEP = 0.15
REJECT_STEP = 0.25
WEIGHT_MIN = 0.10
WEIGHT_MAX = 2.00
FIRE_THRESHOLD = 0.40           # signal_severity * rule_weight must clear this
BASELINE_WEIGHT = 1.00


def _load_local() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return None


def _save_local(data: dict) -> None:
    cache.ensure_dir()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, STATE_FILE)


def _normalize(data: dict) -> dict:
    data = data or {}
    data.setdefault("recs", {})
    data.setdefault("weights", {})
    data.setdefault("feedback", [])
    return data


def _load_state() -> dict:
    data = _load_local()
    return _normalize(data or {})


def _save_state(state: dict) -> None:
    _save_local(state)


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------
@dataclass
class Signal:
    """One observable slice of state that a rule might act on."""
    kind: str                       # short id, e.g. "low_readiness_today"
    severity: float                 # 0..1 - how strong the signal is
    read: str                       # human one-liner for the rec's "why" list
    evidence: dict = field(default_factory=dict)
    date: str = ""                  # ISO date the signal is anchored to


def _severity_from_readiness(score: int) -> float:
    if score is None:
        return 0.0
    if score <= 15: return 1.0
    if score <= 30: return 0.85
    if score <= 45: return 0.6
    if score <= 60: return 0.35
    return 0.0


def collect_signals(snap: dict, today: dt.date) -> list[Signal]:
    """Batch-inspect the snapshot and return every signal worth surfacing.
    Rules take this list plus the plan and decide whether to fire.

    Every "today's morning read" signal (readiness, HRV, sleep) is gated on
    the underlying Garmin data being from TODAY. Early-morning app opens
    before Garmin has synced overnight metrics used to fire recs off
    yesterday's numbers - a rec built on stale data isn't a real coaching
    call. We hold off until the fresh read lands.
    """
    out: list[Signal] = []
    snap = snap or {}
    today_iso = today.isoformat()

    # -- Today's readiness --------------------------------------------------
    lr = snap.get("latest_readiness") or {}
    score = lr.get("score")
    rec_min = lr.get("recovery_time_min") or 0
    lr_date = (lr.get("date") or "")[:10]
    if score is not None and lr_date == today_iso:
        sev = _severity_from_readiness(score)
        # A big recovery-time flag corroborates only when the composite score
        # is also flagging. If Garmin says score >= 60 (moderate+), the
        # elevated recovery-time is just normal post-hard-day accounting and
        # shouldn't override the score - the athlete is fine.
        if rec_min >= 1440 and sev < 0.6 and score is not None and score < 55:
            sev = 0.6
        if sev > 0:
            out.append(Signal(
                kind="low_readiness_today",
                severity=sev,
                read=(f"Readiness {score}/100 ({(lr.get('level') or '').lower() or 'low'}) "
                      f"with {round(rec_min/60)}h recovery flag from Garmin"),
                evidence={"score": score, "recovery_time_min": rec_min,
                          "feedback": lr.get("feedback")},
                date=lr.get("date") or today.isoformat(),
            ))

    # -- HRV streak below baseline low -------------------------------------
    # Gated on the latest HRV entry being today's overnight read - a streak
    # calculated without last night's data would fire off yesterday's window.
    hrv_list = snap.get("hrv") or []
    baseline = (snap.get("hrv_baseline") or {}).get("low")
    hrv_latest_date = ((hrv_list[-1] if hrv_list else {}).get("date") or "")[:10]
    if hrv_list and baseline and hrv_latest_date == today_iso:
        recent = [h for h in hrv_list[-7:] if h.get("last_night_avg") is not None]
        below = 0
        for h in reversed(recent):
            if (h.get("last_night_avg") or 0) < baseline:
                below += 1
            else:
                break
        if below >= 3:
            out.append(Signal(
                kind="hrv_below_baseline_streak",
                severity=min(1.0, 0.55 + 0.15 * (below - 3)),
                read=f"HRV under baseline ({baseline} ms) for {below} nights running",
                evidence={"streak": below, "baseline_low": baseline,
                          "last_values": [h.get("last_night_avg") for h in recent[-below:]]},
                date=recent[-1].get("date"),
            ))

    # -- Sleep debt (rolling 3-night average) ------------------------------
    # Gated on the latest sleep entry being today's - avoids firing a debt
    # signal before this morning's read has synced.
    sleep = snap.get("sleep") or []
    sleep_latest_date = ((sleep[-1] if sleep else {}).get("date") or "")[:10]
    if len(sleep) >= 3 and sleep_latest_date == today_iso:
        recent = [s for s in sleep[-3:] if s.get("total_s")]
        if recent:
            avg_h = sum(s["total_s"] for s in recent) / len(recent) / 3600.0
            if avg_h < 6.0:
                out.append(Signal(
                    kind="sleep_debt_3night",
                    severity=min(1.0, (6.0 - avg_h) / 1.5),
                    read=f"3-night sleep average is {avg_h:.1f}h (target 7-8h)",
                    evidence={"avg_hours": round(avg_h, 2),
                              "nights": [round(s["total_s"] / 3600.0, 2) for s in recent]},
                    date=recent[-1].get("date"),
                ))

    # -- ACWR hot (from Garmin's own readiness feed) -----------------------
    # Same freshness gate — ACWR travels with the readiness reading, so if the
    # readiness date isn't today, the number's stale.
    if lr.get("acwr_pct") and lr["acwr_pct"] >= 100 and lr_date == today_iso:
        pct = lr["acwr_pct"]
        out.append(Signal(
            kind="acwr_hot",
            severity=min(1.0, (pct - 90) / 30.0),
            read=f"Acute:chronic load ratio is at {pct}% - approaching the injury-risk band",
            evidence={"acwr_pct": pct},
            date=lr.get("date") or today.isoformat(),
        ))

    # -- Missed planned quality/long yesterday -----------------------------
    try:
        plan = planlib.load_plan() or {}
    except Exception:
        plan = {}
    yesterday = (today - dt.timedelta(days=1)).isoformat()
    y_plan = planlib.get_day(plan, yesterday) if plan else None
    if y_plan and y_plan.get("type") in ("quality", "long", "race"):
        ran_y = any(a.get("date") == yesterday for a in (snap.get("activities") or []))
        if not ran_y:
            out.append(Signal(
                kind="missed_quality_yesterday",
                severity=0.7,
                read=f"Yesterday's planned {y_plan.get('type')} ({(y_plan.get('title') or '')[:40]}) didn't happen",
                evidence={"missed_type": y_plan.get("type"),
                          "planned_km": y_plan.get("distance_km"),
                          "title": y_plan.get("title")},
                date=yesterday,
            ))

    return out


# --------------------------------------------------------------------------
# recommendation objects
# --------------------------------------------------------------------------
@dataclass
class Rec:
    id: str                         # stable per (rule, target_date, day_hash)
    rule: str
    title: str
    reason_md: str                  # markdown, shown in the card
    signals: list[str]              # signal kinds that triggered
    routine: str                    # existing routine name (make_rest, shorten_day, swap_days)
    args: dict                      # args to pass when accepted
    target_date: str                # the plan day this affects (or the primary one)
    diff_preview: str               # human "Wed 9/9: quality 8 km -> rest"
    severity: float                 # combined signal severity (0..1)
    confidence: float               # severity * learned rule weight (0..1)
    created_at: str
    status: str = "pending"         # "pending" | "accepted" | "rejected" | "expired" | "applied_error"
    decided_at: Optional[str] = None
    feedback: Optional[str] = None
    apply_result: Optional[dict] = None


def _rec_id(rule: str, target_date: str, day_ctx: dict = None) -> str:
    """Stable id keyed only by (rule, target_date). day_ctx used to be in the
    hash so re-planned days would re-fire, but that meant accepting a rec (which
    changes the day title) would create a NEW id on the next refresh - the
    decided rec became invisible and the rule fired again. Now decisions stick:
    once you accept/reject a rec for a given (rule, date), it stays decided even
    if the plan for that date later changes."""
    h = hashlib.sha1(f"{rule}|{target_date}".encode("utf-8")).hexdigest()[:8]
    return f"{rule}_{target_date}_{h}"


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------
_RUN_TYPES = {"easy", "quality", "long", "race", "moderate", "steady", "tempo"}


class Rule:
    """A single recommendation-generating rule. Override `generate` to inspect
    the current signal set + plan and yield zero or more Recs."""
    name: str = "rule"

    def generate(self, snap: dict, cfg: dict, plan: dict, today: dt.date,
                 signals: list[Signal]) -> list[Rec]:
        raise NotImplementedError


class LowReadinessDowngrade(Rule):
    """Today's readiness is poor - propose making today rest, or if today is
    already easy but a hard workout is in the next 24h, propose easing that
    instead. Requires an actual run on the plan today (or tomorrow)."""
    name = "low_readiness_downgrade"

    def generate(self, snap, cfg, plan, today, signals):
        sig = next((s for s in signals if s.kind == "low_readiness_today"), None)
        if not sig:
            return []
        today_iso = today.isoformat()
        today_day = planlib.get_day(plan, today_iso)
        # Fire only when there's actual planned work to modify.
        if not today_day or today_day.get("type") not in _RUN_TYPES:
            return []
        # Don't recommend downgrading a workout that's already done. If a run
        # is logged today whose distance covers at least half the planned km,
        # the session already landed - a "downgrade to rest" recommendation
        # at this point is retroactive noise, not coaching.
        planned_km = today_day.get("distance_km") or 0
        logged_today_km = sum((a.get("distance_km") or 0) for a in (snap or {}).get("activities", [])
                              if a.get("date") == today_iso and "run" in ((a.get("type") or "").lower()))
        if logged_today_km >= max(2.0, planned_km * 0.5):
            return []
        # Very poor readiness -> make rest today. Moderate -> shorten (halve km, min 3).
        km = today_day.get("distance_km") or 0
        if sig.severity >= 0.75 or (sig.evidence.get("recovery_time_min") or 0) >= 1440:
            routine, args = "make_rest", {"date": today_iso, "day": "today"}
            diff = f"Today ({_fmt(today_iso)}): {today_day.get('type')} {km} km -> rest"
        else:
            new_km = max(3, round(km / 2))
            routine, args = "shorten_day", {"date": today_iso, "day": "today", "km": new_km}
            diff = f"Today ({_fmt(today_iso)}): {today_day.get('type')} {km} km -> easy {new_km} km"
        rid = _rec_id(self.name, today_iso, today_day)
        return [Rec(
            id=rid, rule=self.name,
            title="Today's readiness is low - ease off",
            reason_md=(f"- {sig.read}\n"
                       f"- Today's plan: {today_day.get('type')} {km} km "
                       f"(_{(today_day.get('title') or '')[:60]}_)"),
            signals=[sig.kind], routine=routine, args=args,
            target_date=today_iso, diff_preview=diff,
            severity=sig.severity, confidence=0.0,
            created_at=dt.datetime.now().isoformat(),
        )]


class HrvStreakDeload(Rule):
    """3+ nights of HRV under baseline low - propose making the NEXT quality
    or long day a rest, so the parasympathetic system gets a real break."""
    name = "hrv_streak_deload"

    def generate(self, snap, cfg, plan, today, signals):
        sig = next((s for s in signals if s.kind == "hrv_below_baseline_streak"), None)
        if not sig:
            return []
        # Find the next quality/long within 10 days.
        target = None
        for delta in range(0, 10):
            d = today + dt.timedelta(days=delta)
            pd = planlib.get_day(plan, d.isoformat())
            if pd and pd.get("type") in ("quality", "long"):
                target = (d.isoformat(), pd)
                break
        if not target:
            return []
        tdate, tday = target
        rid = _rec_id(self.name, tdate, tday)
        return [Rec(
            id=rid, rule=self.name,
            title="Deload the next hard day - HRV is under baseline",
            reason_md=(f"- {sig.read}\n"
                       f"- Next hard day: {_fmt(tdate)} {tday.get('type')} "
                       f"{tday.get('distance_km','?')} km (_{(tday.get('title') or '')[:60]}_)"),
            signals=[sig.kind],
            routine="make_rest",
            args={"date": tdate, "day": "today"},
            target_date=tdate,
            diff_preview=f"{_fmt(tdate)}: {tday.get('type')} {tday.get('distance_km','?')} km -> rest",
            severity=sig.severity, confidence=0.0,
            created_at=dt.datetime.now().isoformat(),
        )]


class MissedQualityCatchup(Rule):
    """Yesterday's quality/long was missed - propose swapping the next easy day
    with the missed workout so the stimulus still lands this week."""
    name = "missed_quality_catchup"

    def generate(self, snap, cfg, plan, today, signals):
        sig = next((s for s in signals if s.kind == "missed_quality_yesterday"), None)
        if not sig:
            return []
        # Find the next EASY day within 5 days to swap onto.
        yesterday = (today - dt.timedelta(days=1)).isoformat()
        y_day = planlib.get_day(plan, yesterday)
        swap_with = None
        for delta in range(0, 6):
            d = today + dt.timedelta(days=delta)
            pd = planlib.get_day(plan, d.isoformat())
            if pd and pd.get("type") == "easy":
                swap_with = (d.isoformat(), pd)
                break
        if not swap_with:
            return []
        s_date, s_day = swap_with
        rid = _rec_id(self.name, yesterday, y_day)
        return [Rec(
            id=rid, rule=self.name,
            title=f"Recover the missed {y_day.get('type')} - swap with the next easy day",
            reason_md=(f"- {sig.read}\n"
                       f"- Next easy slot: {_fmt(s_date)} "
                       f"({s_day.get('distance_km','?')} km _{(s_day.get('title') or '')[:50]}_)"),
            signals=[sig.kind],
            routine="swap_days",
            args={"a_date": yesterday, "b_date": s_date, "a": "today", "b": "today"},
            target_date=s_date,
            diff_preview=(f"Swap {_fmt(yesterday)} <-> {_fmt(s_date)} "
                          f"(move the {y_day.get('type')} to the easy slot)"),
            severity=sig.severity, confidence=0.0,
            created_at=dt.datetime.now().isoformat(),
        )]


# Registry - add a new rule by appending to this list.
RULES: list[Rule] = [
    LowReadinessDowngrade(),
    HrvStreakDeload(),
    MissedQualityCatchup(),
]


def rule_weight(state: dict, name: str) -> float:
    return float((state.get("weights") or {}).get(name, BASELINE_WEIGHT))


# --------------------------------------------------------------------------
# generator + decision api
# --------------------------------------------------------------------------
def _fmt(iso: str) -> str:
    try:
        d = dt.date.fromisoformat(iso)
        return d.strftime("%a %b ") + str(d.day)
    except Exception:
        return iso


def _expire_stale(state: dict, today: dt.date) -> None:
    """Any pending rec whose target date has passed becomes 'expired'."""
    today_iso = today.isoformat()
    for rid, rec in (state.get("recs") or {}).items():
        if rec.get("status") == "pending" and rec.get("target_date") and rec["target_date"] < today_iso:
            rec["status"] = "expired"
            rec["decided_at"] = dt.datetime.now().isoformat()


def refresh(snap: dict, cfg: dict, today: dt.date) -> dict:
    """Re-collect signals, run each rule, merge new recs into state, expire
    stale ones. Preserves any rec the user has already decided on.

    Pending recs whose rule no longer fires on the fresh signals (workout
    done, morning read updated, plan changed) are auto-swept to 'superseded'
    - a rec that isn't relevant any more shouldn't sit in the queue."""
    state = _load_state()
    _expire_stale(state, today)
    try:
        plan = planlib.load_plan() or {}
    except Exception:
        plan = {}
    signals = collect_signals(snap, today)
    fired_ids: set[str] = set()
    for rule in RULES:
        w = rule_weight(state, rule.name)
        try:
            candidates = rule.generate(snap, cfg, plan, today, signals) or []
        except Exception as e:  # noqa: BLE001
            print(f"[adaptive] rule {rule.name} raised: {e}")
            continue
        for c in candidates:
            c.confidence = round(min(1.0, c.severity * w), 3)
            if c.confidence < FIRE_THRESHOLD:
                continue
            fired_ids.add(c.id)
            existing = state["recs"].get(c.id)
            if existing and existing.get("status") in ("accepted", "rejected"):
                continue                    # respect the user's prior call
            # Preserve confidence-history for pending recs; otherwise write fresh.
            state["recs"][c.id] = asdict(c) if not existing else {
                **existing, "confidence": c.confidence, "severity": c.severity,
                "reason_md": c.reason_md, "diff_preview": c.diff_preview,
                "args": c.args, "routine": c.routine, "signals": c.signals,
                "target_date": c.target_date, "title": c.title,
            }
    # Sweep pending recs the current pass didn't re-emit (rule guard now
    # blocks them - workout done, score recovered, plan changed, etc.)
    now = dt.datetime.now().isoformat()
    for rid, rec in (state.get("recs") or {}).items():
        if rec.get("status") == "pending" and rid not in fired_ids:
            rec["status"] = "superseded"
            rec["decided_at"] = now
    _save_state(state)
    return state


def morning_read_status(snap: dict, today: dt.date) -> dict:
    """Which of the morning-metric signals is present for today. The frontend
    uses this to show a "waiting for Garmin morning sync" hint instead of an
    empty pending list first thing after wake-up."""
    snap = snap or {}
    today_iso = today.isoformat()
    lr = snap.get("latest_readiness") or {}
    hrv = (snap.get("hrv") or [])
    sleep = (snap.get("sleep") or [])
    parts = {
        "readiness": ((lr.get("date") or "")[:10] == today_iso) and lr.get("score") is not None,
        "hrv": bool(hrv) and ((hrv[-1].get("date") or "")[:10] == today_iso),
        "sleep": bool(sleep) and ((sleep[-1].get("date") or "")[:10] == today_iso),
    }
    missing = [k for k, v in parts.items() if not v]
    return {
        "complete": not missing,
        "have": [k for k, v in parts.items() if v],
        "missing": missing,
        "as_of": {
            "readiness": (lr.get("date") or "")[:10] or None,
            "hrv": ((hrv[-1] if hrv else {}).get("date") or "")[:10] or None,
            "sleep": ((sleep[-1] if sleep else {}).get("date") or "")[:10] or None,
        },
    }


def list_recs(state: Optional[dict] = None, morning: Optional[dict] = None) -> dict:
    """Return {pending, recent_decisions, weights, morning} for the UI."""
    state = state or _load_state()
    recs = list((state.get("recs") or {}).values())
    pending = sorted(
        [r for r in recs if r.get("status") == "pending"],
        key=lambda r: (-(r.get("confidence") or 0), r.get("target_date") or ""),
    )
    decided = sorted(
        [r for r in recs if r.get("status") in ("accepted", "rejected", "applied_error", "superseded")
         and r.get("decided_at")],
        key=lambda r: r.get("decided_at") or "",
        reverse=True,
    )[:50]
    return {
        "pending": pending,
        "recent_decisions": decided,
        "weights": state.get("weights") or {},
        "rule_labels": {r.name: r.__class__.__doc__.split("\n")[0].strip()
                        if r.__class__.__doc__ else r.name for r in RULES},
        "morning": morning or {},
    }


def decide(rec_id: str, decision: str, reason: str,
           applier: Callable[[str, dict], dict],
           snap: Optional[dict] = None) -> dict:
    """Accept or reject a rec.

    - `applier(routine_name, args)` runs the underlying plan mutation when
      accepted; returned as the rec's `apply_result`. Adaptive doesn't import
      the routines module directly - the caller wires it in.
    - Weights shift by ACCEPT_STEP / REJECT_STEP and a feedback row lands in
      the ledger so the coach can show what it learned.
    """
    if decision not in ("accepted", "rejected"):
        return {"error": f"unknown decision {decision}"}
    state = _load_state()
    rec = (state.get("recs") or {}).get(rec_id)
    if not rec:
        return {"error": "unknown rec_id"}
    if rec.get("status") not in ("pending", None):
        return {"error": f"rec is already {rec.get('status')}"}
    now = dt.datetime.now().isoformat()
    rec["status"] = decision
    rec["decided_at"] = now
    rec["feedback"] = (reason or "").strip() or None

    rule = rec.get("rule")
    w = rule_weight(state, rule)
    if decision == "accepted":
        w = min(WEIGHT_MAX, w + ACCEPT_STEP)
    else:
        w = max(WEIGHT_MIN, w - REJECT_STEP)
    state.setdefault("weights", {})[rule] = round(w, 3)

    state.setdefault("feedback", []).append({
        "at": now, "rule": rule, "rec_id": rec_id,
        "decision": decision, "reason": reason or None,
        "target_date": rec.get("target_date"),
        "confidence": rec.get("confidence"),
        "signals": rec.get("signals"),
    })

    if decision == "accepted":
        try:
            rec["apply_result"] = applier(rec.get("routine"), rec.get("args") or {})
        except Exception as e:  # noqa: BLE001
            rec["status"] = "applied_error"
            rec["apply_result"] = {"error": f"{type(e).__name__}: {e}"}
    _save_state(state)
    return {"ok": True, "rec": rec, "weights": state["weights"]}


def learning_summary() -> dict:
    """Compact 'here's what the coach has learned so far' block for the UI."""
    state = _load_state()
    weights = state.get("weights") or {}
    fb = state.get("feedback") or []
    per_rule = {}
    for f in fb:
        r = f.get("rule")
        if not r: continue
        per_rule.setdefault(r, {"accepted": 0, "rejected": 0, "reasons": []})
        per_rule[r][f.get("decision", "rejected")] = per_rule[r].get(f.get("decision"), 0) + 1
        if f.get("reason"):
            per_rule[r]["reasons"].append({"at": f.get("at"), "text": f["reason"]})
    for r, agg in per_rule.items():
        agg["weight"] = round(weights.get(r, BASELINE_WEIGHT), 3)
        agg["reasons"] = agg["reasons"][-3:]        # keep the last 3
    return {"per_rule": per_rule, "baseline": BASELINE_WEIGHT}
