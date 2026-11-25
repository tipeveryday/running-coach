"""Long-history personal baselines for resting HR and overnight HRV.

The nightly snapshot only carries ~6 weeks of data, which is too short and noisy
to define what "normal" is for this athlete. This module pulls up to ~2 years of
history (sampled — dense recently, sparse further back) and derives baselines the
way a physiologist would:

  - the recent window defines "current normal" (fitness drifts, so a 2-year mean
    would be misleading — this athlete's resting HR fell from ~67 to ~50)
  - the full history gives all-time best and long-term trend for context

It's expensive (many Garmin calls), so it's computed on demand and cached
separately from the fast nightly snapshot.
"""
from __future__ import annotations
import datetime as dt
from typing import Optional

RECENT_DAYS = 120          # window that defines "current normal"
RECENT_STEP = 4            # sample cadence inside the recent window (days)
OLD_STEP = 30              # sample cadence for the 120→730d tail
MAX_DAYS = 730             # ~2 years


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo))


def _rhr_of(resp) -> Optional[float]:
    try:
        arr = resp["allMetrics"]["metricsMap"]["WELLNESS_RESTING_HEART_RATE"]
        v = arr[0].get("value")
        return float(v) if v else None
    except Exception:
        return None


def _hrv_of(resp):
    """Return (last_night_avg, balanced_low, balanced_upper) from get_hrv_data."""
    if not isinstance(resp, dict):
        return None, None, None
    summ = resp.get("hrvSummary") or {}
    bl = summ.get("baseline") or {}
    return summ.get("lastNightAvg"), bl.get("balancedLow"), bl.get("balancedUpper")


def _sample_dates(today: dt.date):
    days = set()
    for b in range(0, RECENT_DAYS, RECENT_STEP):
        days.add(b)
    for b in range(RECENT_DAYS, MAX_DAYS, OLD_STEP):
        days.add(b)
    return sorted(days)


def build_baselines(client, today: Optional[dt.date] = None, log=lambda *_: None) -> dict:
    today = today or dt.date.today()
    dates = _sample_dates(today)
    log(f"sampling {len(dates)} days of RHR/HRV over {MAX_DAYS//365}y…")

    rhr_all = []       # (date, value) full history
    rhr_recent = []    # values within RECENT_DAYS
    hrv_recent = []    # last-night-avg within RECENT_DAYS
    hrv_all = []
    bal_low = bal_hi = None

    for i, back in enumerate(dates):
        d = today - dt.timedelta(days=back)
        iso = d.isoformat()
        try:
            r = _rhr_of(client.get_rhr_day(iso))
        except Exception:
            r = None
        if r:
            rhr_all.append((iso, r))
            if back <= RECENT_DAYS:
                rhr_recent.append(r)
        # HRV only worth sampling in the recent window (Garmin's band adapts, and
        # older HRV is patchy) — keeps the call count sane.
        if back <= RECENT_DAYS:
            try:
                last, lo, hi = _hrv_of(client.get_hrv_data(iso))
            except Exception:
                last = lo = hi = None
            if last is not None:
                hrv_recent.append(last)
                hrv_all.append(last)
                if lo and hi and bal_low is None:   # most-recent non-null band
                    bal_low, bal_hi = lo, hi
        if i % 10 == 0:
            log(f"  {i}/{len(dates)}…")

    rhr_vals = [v for _, v in rhr_all]
    # yearly medians for a long-term trend line
    yearly = {}
    for iso, v in rhr_all:
        yr = iso[:4]
        yearly.setdefault(yr, []).append(v)
    yearly_median = {yr: _pct(vs, 0.5) for yr, vs in sorted(yearly.items())}

    rhr = {
        "recent_rested": _pct(rhr_recent, 0.15),      # typical well-recovered low
        "recent_baseline": _pct(rhr_recent, 0.5),     # normal night
        "recent_elevated": _pct(rhr_recent, 0.85),    # high end for you now
        "recent_n": len(rhr_recent),
        "all_time_low": round(min(rhr_vals)) if rhr_vals else None,
        "all_time_high": round(max(rhr_vals)) if rhr_vals else None,
        "two_year_median": _pct(rhr_vals, 0.5),
        "yearly_median": yearly_median,
        "n": len(rhr_all),
    } if rhr_all else None

    hrv = {
        "balanced_low": bal_low,
        "balanced_high": bal_hi,
        "recent_avg": round(sum(hrv_recent) / len(hrv_recent), 1) if hrv_recent else None,
        "recent_best": round(max(hrv_recent)) if hrv_recent else None,
        "all_time_best": round(max(hrv_all)) if hrv_all else None,
        "n": len(hrv_all),
    } if hrv_all else None

    return {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "as_of": today.isoformat(),
        "window_days": MAX_DAYS,
        "recent_days": RECENT_DAYS,
        "rhr": rhr,
        "hrv": hrv,
    }
