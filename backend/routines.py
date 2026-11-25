# -*- coding: utf-8 -*-
"""Chat-triggered routines.

Each routine is a function the chat coach can invoke on the user's behalf:
  - QUERY routines read data and return a formatted card/text
  - ACTION routines trigger app actions (refresh Garmin, sync workouts, open a tab)
  - PLAN routines modify plan.json (swap a day, shorten a workout) - require confirm

A routine returns:
  { "kind": "text" | "card" | "action" | "confirm" | "log",
    "title": str,
    "body": str (markdown for text/card),
    "fields": [(label, value), ...] (optional),
    "action": { "type": ..., "args": ... } (for action kind),
    "confirm": { "routine": name, "args": {...}, "prompt": "..." } (for confirm kind)
  }

Two dispatch layers:
  1) rule-based match on patterns (fast, free, deterministic)
  2) LLM fallback (Anthropic tool-use) if ANTHROPIC_API_KEY is set and no rule matches
"""
from __future__ import annotations
import datetime as dt
import json
import os
import re
from typing import Any, Callable, Optional

import planlib


# ---------------- registry ----------------
class Routine:
    def __init__(self, name: str, description: str, patterns: list,
                 handler: Callable, params_schema: Optional[dict] = None,
                 kind: str = "query"):
        self.name = name
        self.description = description
        self.patterns = [re.compile(p, re.I) if isinstance(p, str) else p for p in patterns]
        self.handler = handler
        self.params_schema = params_schema or {"type": "object", "properties": {}}
        self.kind = kind  # "query" | "action" | "plan"

    def match(self, message: str) -> Optional[dict]:
        """Return kwargs dict if any pattern matches, else None. Named regex
        groups become kwargs."""
        for p in self.patterns:
            m = p.search(message)
            if m:
                return dict(m.groupdict())
        return None


REGISTRY: dict[str, Routine] = {}

# Providers wired in by app.py at request time so routines can pull on-demand
# Garmin data (per-lap splits, activity streams) without importing app.py.
# Keys: "splits" -> Callable[[int], list[dict]]; "stream" -> Callable[[int], dict].
PROVIDERS: dict[str, Callable] = {}


def routine(name: str, description: str, patterns: list, kind: str = "query",
            params_schema: Optional[dict] = None):
    def deco(fn):
        REGISTRY[name] = Routine(name, description, patterns, fn, params_schema, kind)
        return fn
    return deco


# ---------------- routine result helpers ----------------
def text(title: str, body: str, footer: str = "") -> dict:
    return {"kind": "text", "title": title, "body": body, "footer": footer}


def card(title: str, body: str, fields: Optional[list] = None, tone: str = "neutral") -> dict:
    return {"kind": "card", "title": title, "body": body, "fields": fields or [], "tone": tone}


def action(title: str, message: str, action_type: str, args: Optional[dict] = None) -> dict:
    return {"kind": "action", "title": title, "body": message,
            "action": {"type": action_type, "args": args or {}}}


def confirm(title: str, prompt: str, routine_name: str, args: dict) -> dict:
    return {"kind": "confirm", "title": title, "body": prompt,
            "confirm": {"routine": routine_name, "args": args}}


def _fmt_date(d):
    if isinstance(d, str):
        try:
            d = dt.date.fromisoformat(d[:10])
        except Exception:
            return d
    if not hasattr(d, "strftime"):
        return str(d)
    # %-d (no zero-pad) is POSIX-only; strip the pad in a portable way
    return d.strftime("%a %b ") + str(d.day)


# ================================================================
# QUERY ROUTINES - read data, no side effects
# ================================================================
@routine("today_snapshot",
         "What's on tap today + morning readiness verdict",
         [r"\btoday\b", r"what.*(should|do).*(run|today)", r"\bthis morning\b", r"what.*on tap"])
def r_today(args, snap, cfg, today):
    import coach
    v = coach.todays_verdict(snap, today)
    s = v.get("session") or {}
    fields = []
    if s.get("title"): fields.append(("Session", s["title"]))
    if s.get("distance_km"): fields.append(("Distance", f"{s['distance_km']} km"))
    if s.get("pace"): fields.append(("Pace", s["pace"] + "/km"))
    fields.append(("Verdict", v.get("headline", "").title()))
    tone = {"green": "good", "amber": "warn", "red": "bad"}.get(v.get("verdict"), "neutral")
    return card(f"Today - {_fmt_date(today)}",
                v.get("recommendation") or v.get("headline", ""),
                fields=fields, tone=tone)


@routine("this_week",
         "Weekly volume and remaining runs this week",
         [r"this week", r"\bweek\b.*progress", r"\bhow.*week"])
def r_week(args, snap, cfg, today):
    import coach
    p = coach.progress(snap, today)
    cur = next((w for w in (p.get("weeks") or []) if w.get("state") == "current"), None)
    if not cur:
        return text("This week", "Couldn't find a current-week block in your plan.")
    fields = [("Target", f"{cur['target_km']} km"),
              ("Actual", f"{cur['actual_km']} km"),
              ("Quality done", f"{cur['quality_done_est']}/{cur['quality_planned']}"),
              ("Runs done", f"{cur['workouts_done']}/{cur['workouts_planned']}")]
    tone = "good" if cur.get("good_week") else "neutral"
    return card(f"This week - W{cur['week']} - {cur.get('phase','')}",
                cur.get("focus", ""), fields=fields, tone=tone)


@routine("recovery_status",
         "Current readiness, HRV, resting HR, recovery time",
         [r"recover", r"\bhrv\b", r"\breadiness\b", r"\btired\b", r"how.*rested"])
def r_recovery(args, snap, cfg, today):
    import recovery as recoverylib
    r = recoverylib.analyze_recovery(snap, today)
    if not r.get("has_data"):
        return text("Recovery", r.get("message", "No recovery data synced yet."))
    rd = r.get("readiness") or {}
    hrv = r.get("hrv") or {}
    rhr = r.get("rhr") or {}
    fields = []
    if rd.get("score") is not None: fields.append(("Readiness", f"{rd['score']}/100 ({rd.get('label','')})"))
    if rd.get("recovery_hours") is not None: fields.append(("Recovery time", f"{rd['recovery_hours']} h"))
    if hrv.get("last") is not None: fields.append(("HRV last night", f"{hrv['last']} ms · {hrv.get('status','')}"))
    if rhr.get("rhr") is not None: fields.append(("Resting HR", f"{rhr['rhr']} bpm"))
    tone = {"green": "good", "amber": "warn", "red": "bad", "grey": "neutral"}.get(rd.get("color"), "neutral")
    body = rd.get("guidance") or ""
    if rd.get("recovery_read"):
        body = (body + "\n\n" + rd["recovery_read"]).strip()
    return card("Recovery", body, fields=fields, tone=tone)


@routine("paces",
         "Your training paces (easy, threshold, MP, etc.)",
         [r"\bpaces?\b", r"target pace", r"race pace"])
def r_paces(args, snap, cfg, today):
    paces = cfg.get("paces") or {}
    fields = []
    for k in ("easy", "steady", "mp", "hmp", "threshold", "tenk", "fivek", "rep"):
        p = paces.get(k) or {}
        val = p.get("target") or (f"{p.get('low')}–{p.get('high')}" if p.get("low") else None)
        if val:
            fields.append((p.get("label") or k, val + "/km"))
    return card("Your training paces", "Locked to your goal: " + str(((cfg.get("goals") or {}).get(cfg.get("primary_goal") or "A") or {}).get("time", "?")) + " for " + str((cfg.get("race") or {}).get("name", "your race")) + ".",
                fields=fields, tone="neutral")


@routine("last_run",
         "Summary of your most recently logged run",
         [r"last run", r"latest run", r"most recent run"])
def r_last(args, snap, cfg, today):
    import coach
    a = coach.analyze_last_run(snap)
    if not a:
        return text("Last run", "No recent run found. Refresh from Garmin.")
    r = a.get("run") or {}
    # analyze_last_run() returns the pace pre-formatted as `avg_pace` ("m:ss"), not
    # as `avg_pace_s_per_km`. Reading the wrong key here made it render "None/km".
    fields = [("Date", r.get("date")),
              ("Distance", f"{r.get('distance_km')} km"),
              ("Pace", f"{r.get('avg_pace') or '-'}/km"),
              ("Avg HR", str(r.get("avg_hr") or "-"))]
    verdict = a.get("verdict") or ""
    return card(f"Last run - {r.get('name','')[:40]}", verdict, fields=fields, tone="neutral")


@routine("activity_splits",
         "Fetch per-lap splits (interval/rep segments) for a run. Use this whenever the "
         "user asks about intervals, splits, reps, laps, work segments, race-pace blocks, "
         "or how each fast segment went in a workout. Defaults to the most recent run "
         "when no arguments are given.",
         [r"\b(intervals?|splits?|laps?|reps?|segments?)\b"])
def r_activity_splits(args, snap, cfg, today):
    import coach
    date_iso = args.get("date")
    runs = [a for a in (snap or {}).get("activities", []) if a.get("id")]
    runs.sort(key=lambda a: a.get("start") or a.get("date") or "", reverse=True)
    run = None
    if date_iso:
        run = next((a for a in runs if a.get("date") == date_iso), None)
    if not run:
        run = runs[0] if runs else None
    if not run:
        return text("Splits", "No recent run with a Garmin activity id.")
    fetch = PROVIDERS.get("splits")
    if not fetch:
        return text("Splits", "Splits provider isn't wired on this server.")
    try:
        laps = fetch(run["id"]) or []
    except Exception as e:  # noqa: BLE001
        return text("Splits", f"Couldn't fetch splits: {type(e).__name__}: {e}")
    if not laps:
        return text("Splits", "Garmin didn't return per-lap splits for this run.")
    # Preserve Garmin's manual-lap boundaries (used as work/rest markers for
    # interval workouts) and normalize pace into m:ss/km for the reply.
    rows = []
    for lp in laps:
        rows.append({
            "lap": lp.get("lap"),
            "intensity": lp.get("intensity"),
            "distance_km": lp.get("distance_km"),
            "duration_s": lp.get("duration_s"),
            "pace": planlib.pace_str(lp.get("pace_s")),
            "gap_pace": planlib.pace_str(lp.get("gap_s")),
            "avg_hr": lp.get("avg_hr"),
            "max_hr": lp.get("max_hr"),
            "elev_gain": lp.get("elev_gain"),
        })
    # Also compute work/rest aggregates so the LLM can answer "how did the fast
    # segments hold up" without re-reducing the array in prose.
    work = [r for r in rows if (r.get("intensity") or "").lower() == "active"]
    rest = [r for r in rows if (r.get("intensity") or "").lower() == "rest"]
    summary_fields = [("Run", (run.get("name") or "")[:60]),
                      ("Date", run.get("date")),
                      ("Total km", run.get("distance_km")),
                      ("Laps", len(rows))]
    if work:
        wk_km = round(sum(r.get("distance_km") or 0 for r in work), 2)
        summary_fields.append(("Work reps", f"{len(work)} × ~{round(wk_km/len(work), 2)} km"))
    body_lines = ["| # | Kind | km | Time | Pace | HR |", "|---|---|---|---|---|---|"]
    for r in rows[:24]:  # cap for token safety
        kind = (r.get("intensity") or "-")[:1].upper() or "-"
        km = f"{r.get('distance_km'):.2f}" if r.get("distance_km") is not None else "-"
        secs = r.get("duration_s") or 0
        t = f"{int(secs // 60)}:{int(secs % 60):02d}" if secs else "-"
        pace = f"{r.get('pace') or '-'}/km"
        hr = str(r.get("avg_hr") or "-")
        body_lines.append(f"| {r.get('lap','?')} | {kind} | {km} | {t} | {pace} | {hr} |")
    body = "\n".join(body_lines)
    return {"kind": "card", "title": "Splits", "body": body,
            "fields": summary_fields, "tone": "neutral",
            "_laps": rows, "_work": work, "_rest": rest}


@routine("list_niggles",
         "List active injury/niggle records with severity and days-active. Use "
         "whenever the athlete mentions pain, soreness, an injury, or asks 'how's my [body part]'.",
         [r"\b(niggle|injur|pain|sore|hurt|tight)"])
def r_list_niggles(args, snap, cfg, today):
    import niggles
    rows = niggles.list_all("active")
    if not rows:
        return text("Niggles", "No active niggles logged. Say the word if something's flaring.")
    fields = [(f"{(n.get('side') or '').upper()+' ' if n.get('side') else ''}"
               f"{n.get('body_part')}",
               f"sev {n.get('severity')}/5 · {n.get('days_active','?')}d"
               + (f" · {n.get('aggravated_by')}" if n.get("aggravated_by") else ""))
              for n in rows[:6]]
    return card(f"Active niggles ({len(rows)})", "", fields=fields, tone="warn")


@routine("log_niggle",
         "Create or update a niggle record (body part + severity 1-5). Use when "
         "The athlete reports NEW pain/soreness or updates a known niggle's severity. "
         "Ask for the body part and a 1-5 severity if he didn't give both.",
         [])   # no rule regex - LLM-only, avoids stepping on r_list_niggles
def r_log_niggle(args, snap, cfg, today):
    import niggles
    body = (args.get("body_part") or "").strip()
    sev = args.get("severity")
    if not body or sev is None:
        return text("Log a niggle", "I need a body part and a severity 1-5 to log it.")
    existing = next((n for n in niggles.list_all("active")
                     if n.get("body_part", "").lower() == body.lower()
                     and (n.get("side") or "") == (args.get("side") or "").upper()), None)
    if existing:
        r = niggles.update(existing["id"], severity=int(sev),
                           note=args.get("note") or "chat update")
        return card("Niggle updated",
                    f"**{body}** severity is now **{r.get('severity')}/5**.",
                    tone="warn")
    r = niggles.create(body, int(sev), side=args.get("side"),
                       aggravated_by=args.get("aggravated_by") or "",
                       notes=args.get("notes") or "")
    if r.get("error"):
        return text("Log a niggle", r["error"])
    return card("Niggle logged",
                f"Tracking **{body}** at severity **{sev}/5**. Ping me if it flares "
                f"or eases and I'll adjust the plan around it.", tone="warn")


@routine("resolve_niggle",
         "Mark an active niggle as resolved. Use when the athlete says the pain is gone "
         "or something has healed.",
         [])
def r_resolve_niggle(args, snap, cfg, today):
    import niggles
    body = (args.get("body_part") or "").strip().lower()
    if not body:
        return text("Resolve niggle", "Which niggle? Give me the body part.")
    match = next((n for n in niggles.list_all("active")
                  if n.get("body_part", "").lower() == body), None)
    if not match:
        return text("Resolve niggle", f"No active niggle for '{body}'.")
    niggles.resolve(match["id"], (args.get("note") or "").strip())
    return card("Niggle resolved", f"Nice - **{body}** marked healed.", tone="good")


@routine("environment",
         "Today's weather and the pace/effort adjustment it implies. Use when the athlete "
         "asks about heat, weather, conditions, or whether to slow down for the "
         "forecast.",
         [r"\b(weather|temperature|temp|humid|heat|conditions|hot|cold|forecast)"])
def r_environment(args, snap, cfg, today):
    import environment
    d = environment.today(cfg)
    if not d.get("has_data"):
        return text("Conditions", f"Couldn't get weather right now: {d.get('error','no data')}")
    fields = [("Temp", f"{d['temp_c']} C"),
              ("Humidity", f"{d['humidity_pct']}%"),
              ("Feels like", f"{d['heat_index_c']} C"),
              ("Wind", f"{d['wind_kmh']} km/h"),
              ("Pace adjust", f"+{d['pace_adjust_s_per_km']} s/km" if d['pace_adjust_s_per_km'] else "none"),
              ("HR drift", f"+{d['hr_drift_bpm']} bpm" if d['hr_drift_bpm'] else "none")]
    tone = {"none":"neutral","mild":"neutral","warm":"warn","hot":"warn","dangerous":"bad"}.get(d.get("tier"), "neutral")
    return card(f"Conditions ({d.get('tier','')})", d.get("read",""), fields=fields, tone=tone)


@routine("next_hard",
         "When's the next hard/quality session",
         [r"next hard", r"next quality", r"next workout", r"when.*(hard|quality|threshold)"])
def r_next_hard(args, snap, cfg, today):
    plan = planlib.load_plan()
    for wk in plan["weeks"]:
        for d in wk["days"]:
            dd = dt.date.fromisoformat(d["date"])
            if dd < today: continue
            if d.get("type") in ("quality", "long", "race"):
                days_out = (dd - today).days
                when = "today" if days_out == 0 else "tomorrow" if days_out == 1 else f"in {days_out} days ({_fmt_date(dd)})"
                fields = [("Date", _fmt_date(dd)),
                          ("Type", d["type"].title()),
                          ("Distance", f"{d.get('distance_km')} km")]
                return card(f"Next hard: {when}", d.get("title", ""), fields=fields, tone="neutral")
    return text("Next hard", "Nothing hard scheduled in the rest of your plan.")


@routine("vs_goal",
         "Am I on track for the goal time",
         [r"on track", r"vs goal", r"goal.*(track|realistic)", r"can i (hit|make)"])
def r_vs_goal(args, snap, cfg, today):
    """Layered fallback so a missing Garmin race predictor doesn't silent-empty:
    projection (best) -> current-fitness read from race_targets (good) -> a
    diagnostic saying WHICH inputs are missing (never returns 'not enough data'
    without telling the athlete what to fix)."""
    import fitness as fit
    r = fit.race_targets(snap, cfg, today) if snap else None
    if not r:
        return text("Goal trajectory",
                    "No fitness snapshot yet. Refresh from Garmin, then ask again.")
    if r.get("has_data") is False:
        missing = ", ".join(r.get("missing") or []) or "fitness data"
        return text("Goal trajectory",
                    r.get("read", f"Missing: {missing}. Log a race, or let Garmin build a VO2max reading."))
    goals = (cfg.get("goals") or {})
    primary = r.get("primary") or {}
    goal_time = primary.get("label") or (goals.get("A") or {}).get("time") or "goal"
    p = r.get("projection")

    if p:
        # `_vdot_projection` returns race_time, race_pace, hits_goal, note - not
        # projected_finish/on_track. The old code was reading keys that never existed.
        finish = p.get("race_time", "-")
        pace = p.get("race_pace")
        hits = p.get("hits_goal")
        tone_map = {"A": "good", "B": "warn", "C": "warn"}
        tone = tone_map.get(hits, "bad" if p.get("race_vdot") else "neutral")
        track_lbl = (f"Projects to {goal_time}" if hits == "A"
                     else f"Projects to B goal ({(goals.get('B') or {}).get('time','B')})" if hits == "B"
                     else f"Projects to C goal ({(goals.get('C') or {}).get('time','C')})" if hits == "C"
                     else f"Short of {goal_time} on current trend")
        fields = [("Projected", finish),
                  ("Pace", f"{pace}/km" if pace else "-"),
                  ("Track", track_lbl),
                  ("VDOT (proj)", str(p.get("race_vdot", "-")))]
        return card("On track for goal?", p.get("note", ""), fields=fields, tone=tone)

    # No projection but we have a fitness anchor - report the current read + a
    # note about what's blocking the finish projection so the athlete knows the gap.
    cur = r.get("current_half") or "-"
    cur_pace = r.get("current_half_pace")
    vdot = r.get("current_vdot")
    blocker = ("Race date isn't in the plan config" if not (cfg.get("race") or {}).get("date")
               else "Not enough VDOT history to slope-project yet")
    body = (f"On today's fitness anchor (VDOT {vdot}), a half would land around "
            f"**{cur}** ({cur_pace}/km). Goal-day projection is skipped - {blocker}.")
    fields = [("Current fitness", f"{cur}" + (f" @ {cur_pace}/km" if cur_pace else "")),
              ("Goal", goal_time),
              ("Anchor VDOT", str(vdot))]
    return card("On track for goal?", body, fields=fields, tone="neutral")


# ================================================================
# ACTION ROUTINES - trigger app actions
# ================================================================
@routine("refresh_garmin",
         "Pull fresh data from Garmin Connect",
         [r"refresh.*garmin", r"sync.*(garmin|data|recent)", r"pull.*(latest|garmin)"],
         kind="action")
def r_refresh(args, snap, cfg, today):
    return action("Refreshing from Garmin",
                  "Pulling your latest activities, sleep, HRV and readiness...",
                  "refresh_garmin")


@routine("push_workouts",
         "Push runs/strength/mobility workouts to your Garmin calendar",
         [r"push.*workouts?", r"sync.*workouts?", r"upload.*(workouts?|garmin.*plan)"],
         kind="action")
def r_push(args, snap, cfg, today):
    return action("Sync to Garmin",
                  "Opening the sync dialog on the Calendar tab.",
                  "open_garmin_sync")


@routine("open_tab",
         "Navigate the UI to a specific tab. ONLY use when the user explicitly says 'open', "
         "'go to', or 'switch to' a named tab (coach, activity, fitness, sleep, recovery, "
         "calendar, log). NEVER use to answer a data question - fetch data with the "
         "matching data tool (last_run, activity_splits, vs_goal, paces, etc.) instead.",
         [r"(?:open|go to|switch to)\s+(?P<tab>coach|activity|fitness|sleep|recovery|hrv|calendar|log)\b"],
         kind="action")
def r_open_tab(args, snap, cfg, today):
    tab = (args.get("tab") or "").lower()
    if tab in ("hrv",): tab = "recovery"
    if tab == "coach": tab = "overview"
    return action(f"Opening {tab}", "Switching tabs.", "open_tab", {"tab": tab})


@routine("theme",
         "Toggle light/dark mode",
         [r"(?:switch|change|toggle).*(?:to\s+)?(?P<theme>light|dark).*(?:mode|theme)?",
          r"(?:light|dark)\s*mode"],
         kind="action")
def r_theme(args, snap, cfg, today):
    t = args.get("theme") or ("light" if "light" in "" else "dark")
    return action(f"Switching to {t} mode", f"Theme set to {t}.", "set_theme", {"theme": t})


# ================================================================
# PLAN MODIFICATION ROUTINES - edit plan.json (require confirmation)
# ================================================================
_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
        "friday": 4, "saturday": 5, "sunday": 6,
        "today": None, "tomorrow": None}


def _resolve_date_word(word: str, today: dt.date) -> Optional[str]:
    w = (word or "").lower().strip()
    # normalize possessives / plurals: "tuesdays", "tue's" -> "tuesday"/"tue"
    w = w.rstrip("'").replace("'s", "").replace("’s", "")
    if w.endswith("s") and w[:-1] in _DOW:
        w = w[:-1]
    if w == "today": return today.isoformat()
    if w == "tomorrow": return (today + dt.timedelta(days=1)).isoformat()
    if w in _DOW and _DOW[w] is not None:
        target = _DOW[w]
        delta = (target - today.weekday()) % 7
        return (today + dt.timedelta(days=delta)).isoformat()
    return None


@routine("make_rest",
         "Convert a plan day to a rest day",
         [r"(?:make|turn|convert)\s+(?P<day>today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)\s+(?:a\s+)?rest",
          r"(?:add|take)\s+(?:a\s+)?rest\s+(?:day\s+)?(?:on\s+)?(?P<day>today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)",
          r"skip\s+(?P<day>today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)"],
         kind="plan")
def r_make_rest(args, snap, cfg, today, apply=False):
    # Prefer an explicit ISO date (used by the adaptive engine); fall back to the
    # user-facing day word ("today", "tomorrow", "wed") for chat-typed requests.
    date = args.get("date") or _resolve_date_word(args.get("day", ""), today)
    if not date:
        return text("Convert to rest", "I didn't understand which day. Try 'skip tomorrow' or 'rest Sat'.")
    if not apply:
        plan = planlib.load_plan()
        d = planlib.get_day(plan, date)
        old = (d or {}).get("title", "(nothing scheduled)")
        return confirm("Convert to rest day",
                       f"Change **{_fmt_date(date)}** from _{old}_ to a rest day?",
                       "make_rest", {"day": args.get("day"), "date": date})
    # apply
    return _apply_day_change(date, {"type": "rest", "title": "Rest / mobility",
                                     "distance_km": 0, "pace_key": None,
                                     "why": "Rest day (chat-adjusted)"},
                              summary=f"{_fmt_date(date)} is now a rest day")


@routine("set_race_day",
         "Turn a plan day into a RACE. Sets type=race, replaces title/distance/why, "
         "clears any workout inserts, and pushes to Garmin. Use for A/B/C-race event "
         "days (5K, 10K, half, marathon, tune-up races). Never use it to hijack a "
         "planned quality session - only for actual real-world races.",
         [],   # LLM-only route (no natural-language regex - too easy to misfire)
         kind="plan")
def r_set_race_day(args, snap, cfg, today, apply=False):
    date = args.get("date") or _resolve_date_word(args.get("day", ""), today)
    if not date:
        return text("Set race day", "Give me a date - 'sat' / 'sun' / an ISO date.")
    name = (args.get("name") or "").strip()
    if not name:
        return text("Set race day", "What's the race called?")
    try:
        km = float(args.get("distance_km") or 0)
    except Exception:
        km = 0
    if km <= 0:
        return text("Set race day", "How long is the race, in km?")
    pace_key = args.get("pace_key")
    if not pace_key:
        pace_key = ("fivek" if km <= 6 else "tenk" if km <= 12 else "hmp" if km <= 24 else "mp")
    warmup_km = float(args.get("warmup_km") or 2)
    cooldown_km = float(args.get("cooldown_km") or 1)
    total_km = round(km + warmup_km + cooldown_km, 1)
    start_time = (args.get("start_time") or "").strip()
    location = (args.get("location") or "").strip()
    title = f"RACE - {name} ({km:g} km)"
    if start_time:
        title += f" - {start_time}"
    why = (f"{name}"
           + (f" at {location}" if location else "")
           + (f", {start_time}" if start_time else "")
           + f". Warm up {int(warmup_km*10)/10} km easy + 4x100m strides. "
           + f"Race the {km:g} km at 5K effort (settle, then push). "
           + f"Cool down {int(cooldown_km*10)/10} km easy.")
    if not apply:
        plan = planlib.load_plan()
        d = planlib.get_day(plan, date)
        old = (d or {}).get("title", "(nothing scheduled)")
        return confirm("Set race day",
                       f"Convert **{_fmt_date(date)}** from _{old}_ to **{title}** "
                       f"(~{total_km} km total with warm-up + cool-down)?",
                       "set_race_day", {**args, "date": date})
    # 3-segment structure so the pushed Garmin workout is a proper wu -> race -> cd
    # (rather than one flat block at race pace across the full distance).
    segments = [
        {"effort": "easy", "km": warmup_km},
        {"effort": pace_key if pace_key in ("fivek", "tenk", "hmp", "mp", "threshold") else "fivek", "km": km},
        {"effort": "easy", "km": cooldown_km},
    ]
    return _apply_day_change(date, {
        "type": "race", "title": title, "distance_km": total_km,
        "pace_key": pace_key, "why": why, "segments": segments,
        # Clear any workout structure the day used to carry.
        "hmp_km": 0, "reps": None, "strides": None,
    }, summary=f"{_fmt_date(date)} is now {title}")


@routine("shorten_day",
         "Cut a plan day to N km (keep the same type)",
         [r"(?:shorten|cut|reduce)\s+(?P<day>today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)\s+(?:to\s+)?(?P<km>\d+(?:\.\d+)?)\s*km",
          r"make\s+(?P<day>today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*)\s+(?P<km>\d+(?:\.\d+)?)\s*km"],
         kind="plan")
def r_shorten(args, snap, cfg, today, apply=False):
    date = args.get("date") or _resolve_date_word(args.get("day", ""), today)
    km = float(args.get("km") or 0)
    if not date or not km:
        return text("Shorten", "Try 'shorten tomorrow to 5 km' or 'make Wed 8 km'.")
    if not apply:
        plan = planlib.load_plan()
        d = planlib.get_day(plan, date)
        old_km = (d or {}).get("distance_km") or 0
        return confirm("Shorten workout",
                       f"Change **{_fmt_date(date)}** from **{old_km} km** to **{km} km**?",
                       "shorten_day", {"day": args.get("day"), "km": args.get("km"),
                                        "date": date})
    return _apply_day_change(date, {"distance_km": km},
                              summary=f"{_fmt_date(date)} shortened to {km} km")


_DAY_RE = r"today|tomorrow|mon\w*|tue\w*|wed\w*|thu\w*|fri\w*|sat\w*|sun\w*"

@routine("swap_days",
         "Swap the workouts on two days (move today's long to Sat, swap Wed & Thu, etc.)",
         [rf"(?:swap|switch)\s+(?P<a>{_DAY_RE})\s+(?:and|with|for|to)\s+(?P<b>{_DAY_RE})",
          rf"(?:move|shift|push|change|reschedule|do)\s+(?P<a>{_DAY_RE})(?:'s)?[^.]*?(?:to|for|on)\s+(?P<b>{_DAY_RE})",
          rf"(?P<a>{_DAY_RE})[^.]*?(?:instead of|swap with)\s+(?P<b>{_DAY_RE})"],
         kind="plan")
def r_swap(args, snap, cfg, today, apply=False):
    da = args.get("date_a") or args.get("a_date") or _resolve_date_word(args.get("a", ""), today)
    db = args.get("date_b") or args.get("b_date") or _resolve_date_word(args.get("b", ""), today)
    if not da or not db:
        return text("Swap days", "Try 'swap Sat and Sun' or 'move Sat long to Sunday'.")
    if not apply:
        return confirm("Swap two days",
                       f"Swap the workouts on **{_fmt_date(da)}** and **{_fmt_date(db)}**?",
                       "swap_days", {"a": args.get("a"), "b": args.get("b"),
                                      "date_a": da, "date_b": db})
    # apply: swap the fields (keep dates + dow constant)
    return _apply_swap(da, db)


# ================================================================
# plan-writing helpers
# ================================================================
_MUTABLE_FIELDS = ("type", "title", "distance_km", "pace_key", "why", "reps",
                    "segments", "strides", "hmp_km", "strength", "strength_bailouts",
                    "nightly", "easy_cap", "easy_hr")


def _auto_sync_garmin():
    """Kick off a Garmin sync in the background after a plan mod so the watch
    reflects the change without an extra user step. Uses the shared _sync_state
    lock in app.py so a concurrent sync request from the Calendar button doesn't
    race."""
    import threading
    def _run():
        try:
            import garmin_sync
            garmin_sync.push(weeks_ahead=2, dry=False)
        except Exception:
            import traceback; traceback.print_exc()
    threading.Thread(target=_run, daemon=True).start()


def _apply_day_change(date: str, updates: dict, summary: str) -> dict:
    p = planlib.PLAN_PATH
    plan = json.load(open(p, encoding="utf-8"))
    found = False
    for wk in plan["weeks"]:
        for d in wk["days"]:
            if d.get("date") == date:
                for k, v in updates.items():
                    if v is None:
                        d.pop(k, None)
                    else:
                        d[k] = v
                found = True
                break
        if found:
            break
    if not found:
        return text("Not applied", f"Couldn't find {date} in the plan.")
    json.dump(plan, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    _auto_sync_garmin()
    return card("Plan updated", summary + ". Also **pushing to your Garmin watch** in the background (30-60s).",
                tone="good")


def _apply_swap(date_a: str, date_b: str) -> dict:
    p = planlib.PLAN_PATH
    plan = json.load(open(p, encoding="utf-8"))
    a = b = None
    for wk in plan["weeks"]:
        for d in wk["days"]:
            if d.get("date") == date_a: a = d
            if d.get("date") == date_b: b = d
    if not a or not b:
        return text("Not applied", "One of those dates isn't in the plan.")
    keep_a = {k: a.get(k) for k in ("date", "dow")}
    keep_b = {k: b.get(k) for k in ("date", "dow")}
    body_a = {k: v for k, v in a.items() if k not in ("date", "dow")}
    body_b = {k: v for k, v in b.items() if k not in ("date", "dow")}
    a.clear(); a.update(keep_a); a.update(body_b)
    b.clear(); b.update(keep_b); b.update(body_a)
    json.dump(plan, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    _auto_sync_garmin()
    return card("Plan updated",
                f"Swapped **{_fmt_date(date_a)}** and **{_fmt_date(date_b)}**. Also **pushing to your Garmin watch** in the background (30-60s).",
                tone="good")


# ================================================================
# dispatcher - LLM-first with rule-based fallback
# ================================================================
_TOOL_SCHEMAS = {
    "activity_splits": {"type": "object", "properties": {
        "date": {"type": "string", "description": "ISO date (YYYY-MM-DD) of the run to inspect. Omit for the most recent run."}}},
    "log_niggle": {"type": "object", "properties": {
        "body_part": {"type": "string", "description": "e.g. 'calf', 'achilles', 'ITB', 'shin'"},
        "severity": {"type": "integer", "description": "1 (whisper) - 5 (stop running)"},
        "side": {"type": "string", "enum": ["L", "R", "center"], "description": "L / R / center; omit if bilateral or centered"},
        "aggravated_by": {"type": "string", "description": "what makes it worse - downhills, speed, long runs..."},
        "notes": {"type": "string"}},
        "required": ["body_part", "severity"]},
    "resolve_niggle": {"type": "object", "properties": {
        "body_part": {"type": "string", "description": "body part of the active niggle to resolve"},
        "note": {"type": "string"}},
        "required": ["body_part"]},
    "set_race_day": {"type": "object", "properties": {
        "date": {"type": "string", "description": "ISO date YYYY-MM-DD (preferred). Or `day` word."},
        "day": {"type": "string", "description": "today/tomorrow/mon-sun; used only when `date` isn't given"},
        "name": {"type": "string", "description": "Race name, e.g. 'Gold Rush 5K'"},
        "distance_km": {"type": "number", "description": "Race distance in km"},
        "location": {"type": "string"},
        "start_time": {"type": "string", "description": "e.g. '8:30 AM'"},
        "warmup_km": {"type": "number", "description": "warm-up distance; default 2"},
        "cooldown_km": {"type": "number", "description": "cool-down distance; default 1"},
        "pace_key": {"type": "string", "enum": ["fivek","tenk","hmp","mp","threshold"]}},
        "required": ["name", "distance_km"]},
    "open_tab": {"type": "object", "properties": {
        "tab": {"type": "string", "enum": ["overview", "activity", "fitness", "sleep", "recovery", "calendar", "log"]}}, "required": ["tab"]},
    "theme": {"type": "object", "properties": {
        "theme": {"type": "string", "enum": ["light", "dark"]}}, "required": ["theme"]},
    "make_rest": {"type": "object", "properties": {
        "day": {"type": "string", "description": "Day word: today, tomorrow, Mon/Tue/Wed/Thu/Fri/Sat/Sun"}}, "required": ["day"]},
    "shorten_day": {"type": "object", "properties": {
        "day": {"type": "string"}, "km": {"type": "number"}}, "required": ["day", "km"]},
    "swap_days": {"type": "object", "properties": {
        "a": {"type": "string", "description": "First day (today, tomorrow, or day-of-week)"},
        "b": {"type": "string", "description": "Second day (today, tomorrow, or day-of-week)"}}, "required": ["a", "b"]},
}


def _build_context(snap: dict, cfg: dict, today: dt.date) -> str:
    """A compact snapshot of the athlete's state, injected as system context so
    Claude can reason ("is today a good day to move the long run?") without
    a round-trip through tools for basic reads."""
    cfg = cfg or {}
    race = cfg.get("race") or {}
    goal = (cfg.get("goals") or {}).get(cfg.get("primary_goal") or "A") or {}
    name = (cfg.get("athlete") or {}).get("name") or "the athlete"
    lines = [
        f"ATHLETE: {name}. Goal race: {race.get('name', '?')} ({race.get('distance_km', '?')} km, {race.get('date', '?')}), "
        f"target {goal.get('time', '?')} @ {goal.get('pace_per_km', '?')}/km.",
        f"Today is {today.strftime('%A %Y-%m-%d')}.",
    ]
    try:
        plan = planlib.load_plan()
        d = planlib.get_day(plan, today.isoformat())
        if d:
            lines.append(f"TODAY'S PLAN: {d.get('type')} - {d.get('title','')} ({d.get('distance_km','?')} km).")
        for delta in (-1, 1, 2):
            dd = today + dt.timedelta(days=delta)
            dx = planlib.get_day(plan, dd.isoformat())
            if dx:
                when = "YESTERDAY" if delta == -1 else "TOMORROW" if delta == 1 else dd.strftime('%a')
                lines.append(f"{when}: {dx.get('type')} - {(dx.get('title') or '')[:60]}")
    except Exception:
        pass
    lr = (snap or {}).get("latest_readiness") or {}
    if lr.get("score") is not None:
        lines.append(f"READINESS: {lr.get('score')}/100 ({lr.get('level','')}); recovery-time {round((lr.get('recovery_time_min') or 0)/60)} h; sleep-score {lr.get('sleep_score')}.")
    hv = ((snap or {}).get("hrv") or [])
    if hv:
        lines.append(f"HRV last night: {hv[-1].get('last_night_avg')} ms ({hv[-1].get('status')}).")
    acts = (snap or {}).get("activities") or []
    if acts:
        r = acts[-1]
        lines.append(f"LAST RUN: {r.get('date')} {r.get('distance_km')} km @ {planlib.pace_str(r.get('avg_pace_s_per_km'))}/km, avg HR {r.get('avg_hr')}.")
    paces = (cfg or {}).get("paces") or {}
    if paces:
        keys = [("easy", "E"), ("threshold", "T"), ("mp", "MP"), ("hmp", "HMP")]
        parts = []
        for k, lb in keys:
            p = paces.get(k) or {}
            v = p.get("target") or p.get("low") or ""
            if v: parts.append(f"{lb} {v}")
        if parts:
            lines.append("PACES: " + " / ".join(parts) + ".")
    # Active injuries/niggles - factor these into every answer so the coach
    # doesn't casually prescribe hills onto a shin niggle.
    try:
        import niggles
        n_line = niggles.context_summary()
        if n_line:
            lines.append(n_line)
    except Exception:
        pass
    # Today's weather - the coach adjusts pace calls when it's hot/humid.
    try:
        import environment
        env_line = environment.context_line(cfg)
        if env_line:
            lines.append(env_line)
    except Exception:
        pass
    return "\n".join(lines)


def _tools_for_llm():
    out = []
    for r in REGISTRY.values():
        schema = _TOOL_SCHEMAS.get(r.name, r.params_schema)
        out.append({"name": r.name, "description": r.description, "input_schema": schema})
    return out


def _run_tool(name: str, args: dict, snap, cfg, today):
    r = REGISTRY.get(name)
    if not r:
        return {"error": f"unknown routine {name}"}
    try:
        if r.kind == "plan":
            return r.handler(args or {}, snap, cfg, today, apply=False)
        return r.handler(args or {}, snap, cfg, today)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def dispatch(message: str, snap: dict, cfg: dict, today: dt.date,
             apply_confirmed: Optional[dict] = None,
             history: Optional[list] = None) -> dict:
    """Route a chat message. If ANTHROPIC_API_KEY is set, defer to Claude for
    freeform reasoning + tool use. Otherwise use deterministic rule dispatch.

    ``history`` is the in-session transcript (list of ``{role, content}``)
    BEFORE this turn's user message; the LLM path uses it so the coach remembers
    the back-and-forth within a chat session.
    """
    if apply_confirmed:
        r = REGISTRY.get(apply_confirmed.get("routine"))
        if not r:
            return text("Not applied", "Unknown routine.")
        args = apply_confirmed.get("args") or {}
        try:
            return r.handler(args, snap, cfg, today, apply=True) if r.kind == "plan" else r.handler(args, snap, cfg, today)
        except Exception as e:  # noqa: BLE001
            return text("Error", f"{type(e).__name__}: {e}")

    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        try:
            return _llm_dispatch(message, snap, cfg, today, key, history=history or [])
        except Exception as e:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return _rule_dispatch(message, snap, cfg, today) or text("Chat", f"LLM error: {e}")

    r = _rule_dispatch(message, snap, cfg, today)
    if r:
        return r
    return text("Chat", "I didn't catch a matching routine. Try things like:\n"
                        "- **today** / **this week** / **recover** / **paces**\n"
                        "- **next hard** / **on track for goal**\n"
                        "- **refresh from garmin** / **push workouts** / **open fitness**\n"
                        "- **shorten tomorrow to 5 km** / **skip Sat** / **swap Sat and Sun**\n\n"
                        "_(For freeform natural language, set the `ANTHROPIC_API_KEY` env var to enable the LLM coach.)_")


def _rule_dispatch(message: str, snap: dict, cfg: dict, today: dt.date):
    for r in REGISTRY.values():
        args = r.match(message)
        if args is not None:
            try:
                if r.kind == "plan":
                    return r.handler(args, snap, cfg, today, apply=False)
                return r.handler(args, snap, cfg, today)
            except Exception as e:  # noqa: BLE001
                return text("Error", f"{type(e).__name__}: {e}")
    return None


def _llm_dispatch(message, snap, cfg, today, api_key, history=None):
    """LLM-first: Claude with athlete context + tool-use loop.

    ``history`` (optional) is the prior turns of THIS chat session as
    ``[{role: 'user'|'assistant', content: str}, ...]``. It's prepended so
    Claude sees the full back-and-forth. Tool-use rounds from prior turns are
    not replayed - only the plain-text pairs the frontend keeps.
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    system = (
        "You are the running coach chat assistant embedded in the athlete's personal training app. "
        "Answer in a coach's voice: direct, kind, evidence-based. Use SHORT paragraphs.\n\n"
        "You have TOOLS to fetch fresh data or make plan changes. Use them liberally - "
        "prefer running a tool over guessing. For plan modifications (make_rest, shorten_day, "
        "swap_days), the tool returns a CONFIRM card that the athlete will approve; just call it and "
        "return the result unchanged (do not add narration after a confirm card).\n\n"
        "When a tool returns a card, that IS the answer. Don't repeat its content. If you have "
        "nothing useful to add, don't add a text block after the last tool.\n\n"
        "TOOL PICKING - a few rules that keep answers correct:\n"
        "- For any question about intervals, splits, reps, laps, work segments, or how the "
        "  fast portions of a workout went, CALL `activity_splits`. Never guess splits from "
        "  the summary. Pass `date` when the user names a specific run.\n"
        "- `open_tab` only navigates the UI. Never call it to answer a data question - it "
        "  won't return data and it moves the athlete away from their current view.\n\n"
        "You will see the prior turns of this chat session as normal user/assistant messages - "
        "use them to stay coherent (remember what the athlete just asked, don't repeat prior answers).\n\n"
        "CURRENT CONTEXT (already fetched for you):\n" + _build_context(snap, cfg, today)
    )
    tools = _tools_for_llm()
    messages = []
    for h in (history or [])[-20:]:
        role = h.get("role")
        content = h.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})
    last_card = None
    for _step in range(6):
        resp = client.beta.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
            max_tokens=16000,
            output_config={"effort": "medium"},
            # on a safety decline, re-run on a fallback model inside the same call
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=system,
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "refusal":
            return last_card or text("Coach", "I can't help with that one.")
        tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        text_blocks = [b for b in resp.content if getattr(b, "type", "") == "text"]
        if resp.stop_reason == "tool_use" and tool_uses:
            messages.append({"role": "assistant", "content": [b.model_dump() if hasattr(b, "model_dump") else b for b in resp.content]})
            tool_results = []
            for tu in tool_uses:
                result = _run_tool(tu.name, tu.input or {}, snap, cfg, today)
                if isinstance(result, dict) and result.get("kind") == "confirm":
                    return result
                if isinstance(result, dict) and result.get("kind") in ("card", "action", "text"):
                    last_card = result
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps(result, default=str)[:4000],
                })
            messages.append({"role": "user", "content": tool_results})
            continue
        narration = "\n\n".join(b.text for b in text_blocks).strip()
        if last_card and last_card.get("kind") == "card":
            if narration and narration.lower() not in (last_card.get("body") or "").lower():
                last_card["body"] = (narration + "\n\n" + (last_card.get("body") or "")).strip()
            return last_card
        if last_card:
            return last_card
        return text("Coach", narration or "(no response)")
    return last_card or text("Coach", "I ran out of steps thinking through that.")


def routines_index() -> list:
    """For the frontend to render a 'try one of these' list."""
    return [{"name": r.name, "description": r.description, "kind": r.kind}
            for r in REGISTRY.values()]
