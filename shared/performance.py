"""
Expected PV + performance calculation
=====================================
For each site, compute:
  - hourly_expected[24]      : kWh expected each hour
  - today_expected_total     : sum of hourly_expected for elapsed hours
  - today_actual_total       : sum of actual PV for those same hours
  - performance_pct          : actual / expected x 100, 0-200ish range
  - method                   : currently always 'empirical' (see note below)

CURRENT BEHAVIOUR — EMPIRICAL ONLY:
    expected_kwh_hour = mean of this site's PV at hour-of-day, over the
    last 30 days. Irradiation is NOT used. Matches the existing PV alert
    dashboard's behaviour exactly. Same mental model the team already
    understands - "at 11:00 you usually do X kWh".

    Performance % swings with weather: on a sunny day a healthy site shows
    130%, on a cloudy day it shows 60%. This is the same behaviour as the
    existing dashboard - it's a quick "anything weird?" check, not a
    soiling/degradation gauge.

NAIVE METHOD (PAUSED):
    The naive formula (irradiation x panel area x efficiency) is implemented
    below but currently disabled. To re-enable for a specific site, restore
    the `if _has_naive_inputs(config):` check in build_performance(). The
    naive helpers (_has_naive_inputs, _weighted_efficiency,
    _naive_hourly_expected) are kept in this file so the re-enable is a
    one-line revert when needed.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

SAST = timezone(timedelta(hours=2))


# ---------------------------------------------------------------------------
# Method selection
# ---------------------------------------------------------------------------

def _has_naive_inputs(config: dict) -> bool:
    """True when the site's config has the fields needed for naive calc.

    Naive needs an effective area + at least one panel group with an
    efficiency. Effective area is the panel surface area times tilt cosine,
    but for the SA fleet (rooftop, ~15deg tilt) area_total_m2 is a fair
    approximation when effective_area_m2 isn't set.
    """
    system = config.get("system") or {}
    area = system.get("effective_area_m2") or system.get("area_total_m2")
    if not area or area <= 0:
        return False
    # Need at least one panel group with an efficiency value.
    # Field name in configs is `panel_efficiency` (0-1 fraction).
    groups = system.get("panel_groups") or []
    if not groups:
        return False
    return any(g.get("panel_efficiency") or g.get("efficiency_pct")
               for g in groups)


def _weighted_efficiency(config: dict) -> float | None:
    """Blend panel-group efficiencies, weighted by group area or panel count.

    Returns efficiency as a percentage (0-100), not a fraction.
    Field `panel_efficiency` is stored as 0-1 fraction; multiply by 100.
    Field `efficiency_pct` is already 0-100 and used as-is.
    """
    system = config.get("system") or {}
    groups = system.get("panel_groups") or []
    weighted_sum, total_weight = 0.0, 0.0
    for g in groups:
        # Either field name accepted. Convert to 0-100 percent.
        if g.get("panel_efficiency") is not None:
            eff = g["panel_efficiency"] * 100
        elif g.get("efficiency_pct") is not None:
            eff = g["efficiency_pct"]
        else:
            continue
        # Prefer area weight; fall back to panel count; fall back to 1
        w = (g.get("group_area_m2") or g.get("area_m2")
             or g.get("panel_count") or g.get("count") or 1)
        weighted_sum += eff * w
        total_weight += w
    return weighted_sum / total_weight if total_weight else None


# ---------------------------------------------------------------------------
# Naive: per-hour expected from panel specs + irradiation
# ---------------------------------------------------------------------------

def _naive_hourly_expected(ghi_hourly: list[dict], config: dict) -> list[dict]:
    """Each entry: {time, ghi_w_m2, expected_kwh}."""
    system = config.get("system") or {}
    area = system.get("effective_area_m2") or system.get("area_total_m2") or 0
    eff_pct = _weighted_efficiency(config) or 0
    out = []
    for h in ghi_hourly or []:
        ghi = h.get("value") or h.get("ghi_w_m2") or 0
        expected = (ghi / 1000.0) * area * (eff_pct / 100.0)
        out.append({"time": h.get("time"),
                     "ghi_w_m2": round(ghi, 1),
                     "expected_kwh": round(expected, 3)})
    return out


# ---------------------------------------------------------------------------
# Empirical: hour-of-day mean PV from this site's own 30-day history
#
# Matches the old PV alert dashboard's behaviour: expected[hour] = mean of
# this site's PV at hour-of-day, across the last 30 days. Irradiation is
# NOT used in the empirical method - this is intentional, so the comparison
# matches what the team already understands ("at 11am you usually do X kWh").
# ---------------------------------------------------------------------------

def _empirical_hour_of_day_avg(hourly_history: dict, today: str) -> dict[int, float]:
    """Mean PV by hour-of-day from the last 30 days (excluding today).

    Returns {hour_int: mean_pv_kwh}. Hours that never had non-zero data
    in the window are omitted - the caller treats missing keys as 0.
    """
    cutoff = (datetime.strptime(today, "%Y-%m-%d")
              - timedelta(days=30)).strftime("%Y-%m-%d")
    by_hour: dict[int, list[float]] = defaultdict(list)
    for h in (hourly_history or {}).get("hours", []):
        t = h.get("time", "")
        if not t:
            continue
        d, clock = t.split(" ")
        if d < cutoff or d >= today:
            continue
        pv = h.get("pv") or 0
        # Include zero hours (night, dawn, dusk) so the mean is honest -
        # an hour that's always 0 across 30 days SHOULD have expected = 0,
        # not be omitted. The old dashboard's behaviour matches this.
        try:
            hr = int(clock.split(":")[0])
        except (ValueError, IndexError):
            continue
        by_hour[hr].append(pv)
    return {hr: sum(vals) / len(vals) for hr, vals in by_hour.items()}


def _empirical_hourly_expected(ghi_hourly: list[dict],
                                hourly_history: dict,
                                today: str) -> list[dict]:
    """Per-hour expected = 30-day mean for that hour-of-day.

    ghi_hourly is used only for the timestamp template (24 slots aligned
    to today's clock); the EXPECTED values come from history, not irradiation.
    GHI is still recorded in each entry for context, but the expected_kwh
    field is irradiation-independent.
    """
    by_hour = _empirical_hour_of_day_avg(hourly_history, today)
    out = []
    for h in ghi_hourly or []:
        ghi = h.get("value") or h.get("ghi_w_m2") or 0
        try:
            hr = int(h["time"].split(" ")[1].split(":")[0])
        except (KeyError, ValueError, IndexError):
            hr = -1
        expected = by_hour.get(hr, 0)
        out.append({"time": h.get("time"),
                     "ghi_w_m2": round(ghi, 1),
                     "expected_kwh": round(expected, 3)})
    return out


# ---------------------------------------------------------------------------
# Top-level entry: build the performance block
# ---------------------------------------------------------------------------

def build_performance(data: dict, config: dict, hourly_history: dict | None) -> dict:
    """Return the `performance` block to embed in data.json.

    Shape:
        {
          "method": "naive" | "empirical",
          "hourly_expected": [{time, ghi_w_m2, expected_kwh}, ...],
          "today_expected_total": float (kWh, elapsed hours only),
          "today_actual_total":   float (kWh, elapsed hours only),
          "performance_pct":      float (0-200ish; None when expected = 0),
          "reason":               human-readable explanation
        }
    """
    irr = data.get("irradiation") or {}
    ghi_hourly = irr.get("today_hourly") or []
    today = datetime.now(tz=SAST).strftime("%Y-%m-%d")

    # NOTE: the naive branch (irradiation x area x efficiency) is currently
    # PAUSED. Every site uses the empirical method - hour-of-day average PV
    # from the site's own 30-day history - matching the existing PV alert
    # dashboard's behaviour exactly. To re-enable naive for a specific site
    # later, restore the _has_naive_inputs() check below.
    #
    # if _has_naive_inputs(config):
    #     method = "naive"
    #     hourly_expected = _naive_hourly_expected(ghi_hourly, config)
    #     reason = "Expected = irradiation x panel area x efficiency"
    # else:
    method = "empirical"
    hourly_expected = _empirical_hourly_expected(ghi_hourly,
                                                  hourly_history or {}, today)
    reason = "Expected = site's own 30-day average for this hour-of-day"

    # Compare actual to expected on elapsed hours only.
    actual_by_hour = {}
    for entry in (data.get("energy", {}).get("hourly", {}) or {}).get("pv", []):
        t = entry.get("time")
        if t:
            actual_by_hour[t] = entry.get("value") or 0

    expected_total, actual_total, elapsed = 0.0, 0.0, 0
    for h in hourly_expected:
        t = h.get("time")
        a = actual_by_hour.get(t)
        if a is None or a <= 0:
            # No actual data yet for this hour - skip both sides for fairness
            continue
        expected_total += h.get("expected_kwh", 0)
        actual_total += a
        elapsed += 1

    if expected_total > 0.1:
        perf = round((actual_total / expected_total) * 100, 1)
    else:
        perf = None       # can't divide; dashboard shows '--' until daylight

    return {
        "method": method,
        "hourly_expected": hourly_expected,
        "today_expected_total": round(expected_total, 2),
        "today_actual_total":   round(actual_total, 2),
        "performance_pct":      perf,
        "elapsed_hours_compared": elapsed,
        "reason": reason,
    }
