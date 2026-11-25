# -*- coding: utf-8 -*-
"""Injury / niggle log.

Small persistent record of active and resolved niggles the athlete is watching.
The coach reads active ones as system context so responses reflect the actual
body state, and the adaptive engine can raise them as recs (e.g. "shin niggle
severity 3 + Wed's fartlek: propose easy substitute").

Storage: cache/niggles.json (same persistent volume as the Garmin snapshot).
Model per niggle:
  {
    id: str (uuid4),
    body_part: str (e.g. "left calf", "right ITB", "achilles"),
    side: "L" | "R" | "center" | None,
    severity: int 1-5           # 1=whisper, 5=stop running
    status: "active" | "resolved",
    started_date: ISO,
    resolved_date: ISO | None,
    aggravated_by: str          # "downhills", "speed work", "long runs" ...
    notes: str,
    log: [{at, severity, note}] # severity-over-time history
  }
"""
from __future__ import annotations
import datetime as dt
import json
import os
import uuid
from typing import Optional

import cache


STORE = os.path.join(cache.CACHE_DIR, "niggles.json")


VALID_SEVERITY = range(1, 6)
_ALLOWED_STATUS = {"active", "resolved"}


def _load_local():
    if not os.path.exists(STORE):
        return None
    try:
        with open(STORE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return None


def _save_local(data: dict) -> None:
    cache.ensure_dir()
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)
    os.replace(tmp, STORE)


def _load() -> dict:
    data = _load_local()
    if not data:
        return {"niggles": []}
    data.setdefault("niggles", [])
    return data


def _save(data: dict) -> None:
    _save_local(data)


def _now_iso() -> str:
    return dt.datetime.now().isoformat()


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _validate(body_part: str, severity: int) -> Optional[str]:
    if not body_part or not isinstance(body_part, str) or len(body_part) > 80:
        return "body_part is required (max 80 chars)"
    try:
        s = int(severity)
    except Exception:
        return "severity must be an integer 1-5"
    if s not in VALID_SEVERITY:
        return "severity must be 1-5"
    return None


def list_all(status: Optional[str] = None) -> list[dict]:
    """All niggles, optionally filtered. Active first, most-recent first."""
    data = _load()
    rows = list(data.get("niggles") or [])
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: (r.get("status") != "active",
                             -(_iso_key(r.get("started_date")))))
    for r in rows:
        r["days_active"] = _days_since(r.get("started_date"))
    return rows


def _iso_key(iso: Optional[str]) -> int:
    if not iso:
        return 0
    try:
        d = dt.date.fromisoformat(iso[:10])
        return d.toordinal()
    except Exception:
        return 0


def _days_since(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        d = dt.date.fromisoformat(iso[:10])
        return (dt.date.today() - d).days
    except Exception:
        return None


def create(body_part: str, severity: int, side: Optional[str] = None,
           started_date: Optional[str] = None, aggravated_by: str = "",
           notes: str = "") -> dict:
    err = _validate(body_part, severity)
    if err:
        return {"error": err}
    data = _load()
    started = started_date or _today_iso()
    n = {
        "id": uuid.uuid4().hex,
        "body_part": body_part.strip()[:80],
        "side": (side or "").strip().upper()[:6] or None,
        "severity": int(severity),
        "status": "active",
        "started_date": started,
        "resolved_date": None,
        "aggravated_by": (aggravated_by or "").strip()[:120],
        "notes": (notes or "").strip()[:500],
        "log": [{"at": _now_iso(), "severity": int(severity), "note": "opened"}],
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
    }
    data.setdefault("niggles", []).append(n)
    _save(data)
    n["days_active"] = _days_since(n["started_date"])
    return n


def update(niggle_id: str, severity: Optional[int] = None,
           notes: Optional[str] = None, aggravated_by: Optional[str] = None,
           note: Optional[str] = None) -> dict:
    """Log a severity change or add a diary entry. Any of severity/notes/
    aggravated_by can be updated; `note` is appended to the log."""
    data = _load()
    n = next((x for x in data.get("niggles", []) if x.get("id") == niggle_id), None)
    if not n:
        return {"error": "unknown niggle_id"}
    if severity is not None:
        try:
            s = int(severity)
        except Exception:
            return {"error": "severity must be an integer 1-5"}
        if s not in VALID_SEVERITY:
            return {"error": "severity must be 1-5"}
        prev = n.get("severity")
        n["severity"] = s
        n.setdefault("log", []).append({"at": _now_iso(), "severity": s,
                                          "note": note or f"severity {prev} -> {s}"})
    elif note:
        n.setdefault("log", []).append({"at": _now_iso(),
                                          "severity": n.get("severity"), "note": note})
    if notes is not None:
        n["notes"] = notes.strip()[:500]
    if aggravated_by is not None:
        n["aggravated_by"] = aggravated_by.strip()[:120]
    n["updated_at"] = _now_iso()
    _save(data)
    n["days_active"] = _days_since(n.get("started_date"))
    return n


def resolve(niggle_id: str, note: str = "") -> dict:
    data = _load()
    n = next((x for x in data.get("niggles", []) if x.get("id") == niggle_id), None)
    if not n:
        return {"error": "unknown niggle_id"}
    n["status"] = "resolved"
    n["resolved_date"] = _today_iso()
    n["updated_at"] = _now_iso()
    n.setdefault("log", []).append({"at": _now_iso(), "severity": n.get("severity"),
                                      "note": ("resolved: " + note) if note else "resolved"})
    _save(data)
    n["days_active"] = _days_since(n.get("started_date"))
    return n


def reopen(niggle_id: str, severity: int, note: str = "") -> dict:
    err = _validate("_", severity)
    if err:
        return {"error": err}
    data = _load()
    n = next((x for x in data.get("niggles", []) if x.get("id") == niggle_id), None)
    if not n:
        return {"error": "unknown niggle_id"}
    n["status"] = "active"
    n["resolved_date"] = None
    n["severity"] = int(severity)
    n["updated_at"] = _now_iso()
    n.setdefault("log", []).append({"at": _now_iso(), "severity": int(severity),
                                      "note": ("reopened: " + note) if note else "reopened"})
    _save(data)
    n["days_active"] = _days_since(n.get("started_date"))
    return n


def delete(niggle_id: str) -> dict:
    data = _load()
    before = len(data.get("niggles", []))
    data["niggles"] = [x for x in data.get("niggles", []) if x.get("id") != niggle_id]
    if len(data["niggles"]) == before:
        return {"error": "unknown niggle_id"}
    _save(data)
    return {"ok": True, "deleted": niggle_id}


def context_summary(max_len: int = 300) -> str:
    """One-line summary for the coach's LLM system prompt so answers reflect
    the athlete's current body state. Empty string when nothing active."""
    active = [n for n in list_all("active")]
    if not active:
        return ""
    parts = []
    for n in active[:5]:
        p = n.get("body_part")
        s = n.get("severity")
        side = f"{n.get('side')} " if n.get("side") else ""
        d = n.get("days_active")
        parts.append(f"{side}{p} sev {s}/5" + (f" ({d}d)" if d is not None else ""))
    line = "ACTIVE NIGGLES: " + "; ".join(parts) + "."
    if n := next((n for n in active if n.get("aggravated_by")), None):
        line += f" Aggravated by: {n.get('aggravated_by')}."
    return line[:max_len]
