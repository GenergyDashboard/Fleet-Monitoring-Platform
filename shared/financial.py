"""
Shared financial module
=======================
Converts a normalized energy series into rand figures using the site's tariff
config. Platform-agnostic - same code runs against FusionSolar, GoodWe,
SolisCloud, Sunsynk and Solarman output once their processors emit the schema.

Two public entry points:
  - rate_for(date, tariff)        : look up the import rate(s) for one date
  - compute_financials(...)       : build the full financial block

Eskom TOU period schedule (which hours are peak / standard / off-peak by
day-of-week and high/low season) is hard-coded here. The schedule is the same
nationally - it does not vary per site, so it does not live in per-site config.
Verified against the 1st Ave Spar TOU spreadsheet.

The schedule comes from the conversation on 11 Feb 2026 ("Expected value
resets to zero in monthly overview") and matches Eskom's published Megaflex
TOU bands. If NERSA republishes the bands, only this file needs updating -
not every site config.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable

SAST = timezone(timedelta(hours=2))

# ---------------------------------------------------------------------------
# Eskom TOU period schedule
# ---------------------------------------------------------------------------
# High-demand season = June, July, August. Low-demand = the rest.
# is_weekday  | high_demand | hour    -> period
# Weekday LOW:    Peak  7-8, 18-20   Standard 6, 9-17, 21   Off-peak rest
# Weekday HIGH:   Peak  6-7, 17-19   Standard 8-16, 20-21   Off-peak rest
# Saturday LOW:   Standard 7-11, 18-19                       Off-peak rest
# Saturday HIGH:  Standard 7-11, 17-18                       Off-peak rest
# Sunday LOW:     Standard 18-19                             Off-peak rest
# Sunday HIGH:    Standard 17-18                             Off-peak rest


# ---------------------------------------------------------------------------
# Eskom TOU period schedule - the DEFAULT used when a site's tariff config
# does not declare its own. Most sites buy from Eskom (directly or via a
# municipality on Eskom's national schedule) and don't need an override.
# When a site is on a municipal tariff with different bands, it declares its
# own `schedule` block inside the tariff config (see resolve_schedule).
# ---------------------------------------------------------------------------
# is_weekday  | high_demand | hour    -> period
# Weekday LOW:    Peak  7-8, 18-20   Standard 6, 9-17, 21   Off-peak rest
# Weekday HIGH:   Peak  6-7, 17-19   Standard 8-16, 20-21   Off-peak rest
# Saturday LOW:   Standard 7-11, 18-19                       Off-peak rest
# Saturday HIGH:  Standard 7-11, 17-18                       Off-peak rest
# Sunday LOW:     Standard 18-19                             Off-peak rest
# Sunday HIGH:    Standard 17-18                             Off-peak rest

ESKOM_SCHEDULE = {
    "high_demand_months": [6, 7, 8],
    "weekday": {
        "high": {"peak": [6, 7, 17, 18, 19],
                  "standard": [8, 9, 10, 11, 12, 13, 14, 15, 16, 20, 21]},
        "low":  {"peak": [7, 8, 18, 19, 20],
                  "standard": [6, 9, 10, 11, 12, 13, 14, 15, 16, 17, 21]},
    },
    "saturday": {
        "high": {"standard": [7, 8, 9, 10, 11, 17, 18]},
        "low":  {"standard": [7, 8, 9, 10, 11, 18, 19]},
    },
    "sunday": {
        "high": {"standard": [17, 18]},
        "low":  {"standard": [18, 19]},
    },
}


def resolve_schedule(tariff: dict | None) -> dict:
    """Return the TOU schedule for this site - the per-site override if it has
    one in `tariff.schedule`, otherwise the Eskom default. The shape is the
    same either way, so the caller doesn't care which one it got."""
    if tariff and isinstance(tariff.get("schedule"), dict):
        return tariff["schedule"]
    return ESKOM_SCHEDULE


def is_high_demand(d: date, schedule: dict = ESKOM_SCHEDULE) -> bool:
    """True when d falls in the high-demand season per this schedule."""
    return d.month in schedule.get("high_demand_months", [6, 7, 8])


def tou_period(d: date, hour: int, schedule: dict = ESKOM_SCHEDULE) -> str:
    """Return 'peak', 'standard' or 'off_peak' for one (date, hour) pair,
    using the given schedule (Eskom by default)."""
    wd = d.weekday()           # Mon=0 .. Sun=6
    season = "high" if is_high_demand(d, schedule) else "low"

    if wd <= 4:
        bands = schedule.get("weekday", {}).get(season, {})
    elif wd == 5:
        bands = schedule.get("saturday", {}).get(season, {})
    else:
        bands = schedule.get("sunday", {}).get(season, {})

    if hour in bands.get("peak", []):
        return "peak"
    if hour in bands.get("standard", []):
        return "standard"
    return "off_peak"


# ---------------------------------------------------------------------------
# Rate-period lookup
# ---------------------------------------------------------------------------

def _parse_date(s: str | None) -> date | None:
    return datetime.strptime(s, "%Y-%m-%d").date() if s else None


def _find_period(periods: list[dict], d: date) -> dict | None:
    """Return the rate-period dict whose [from, to] bracket d, or None.

    `to: null` means open-ended (still in force). Periods are scanned in order
    so newer rows that overlap earlier ones can win if needed - the convention
    is non-overlapping ranges sorted by `from`.
    """
    for p in periods:
        start = _parse_date(p.get("from"))
        end = _parse_date(p.get("to"))
        if start and d < start:
            continue
        if end and d > end:
            continue
        return p
    return None


def rate_for(d: date, tariff: dict) -> dict:
    """Look up the rate(s) that apply on date d for this tariff.

    Returns a small dict; what's inside depends on type:
      flat: {'flat': R/kWh}
      tou:  {'peak': R, 'standard': R, 'off_peak': R}
      ppa:  {'ppa': R/kWh}
    Returns {} if no period covers d (caller treats as 'no rate available').
    """
    period = _find_period(tariff.get("rate_periods", []), d)
    if not period:
        return {}
    t = tariff.get("type")
    if t == "flat":
        return {"flat": period.get("flat")}
    if t == "tou":
        return dict(period.get("tou", {}))
    if t == "ppa":
        return {"ppa": period.get("ppa")}
    return {}


def export_rate_for(d: date, tariff: dict) -> float | None:
    """Look up the export rate on date d (only for feed-in mode)."""
    exp = tariff.get("export", {}) or {}
    if exp.get("mode") != "feed-in":
        return None
    period = _find_period(exp.get("rate_periods", []), d)
    return period.get("rate") if period else None


# ---------------------------------------------------------------------------
# Period-level financial calc
# ---------------------------------------------------------------------------

def _import_cost_flat(import_kwh: float, rate: float | None) -> float:
    if rate is None:
        return 0.0
    return import_kwh * rate


def _import_cost_tou(hourly_import: Iterable[dict], tariff: dict) -> float:
    """Sum up import cost across an hourly array using TOU rates per hour.

    Each hourly entry is {'time': 'YYYY-MM-DD HH:MM:SS', 'value': kWh}.
    """
    total = 0.0
    schedule = resolve_schedule(tariff)
    for entry in hourly_import:
        try:
            dt = datetime.strptime(entry["time"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        kwh = entry.get("value") or 0.0
        if kwh <= 0:
            continue
        rates = rate_for(dt.date(), tariff)
        if not rates:
            continue
        period = tou_period(dt.date(), dt.hour, schedule)
        rate = rates.get(period)
        if rate is None:
            continue
        total += kwh * rate
    return total


def _savings(self_consumed_kwh: float, import_rate_average: float | None) -> float:
    """Money the site would have paid if PV had not displaced grid imports."""
    if import_rate_average is None:
        return 0.0
    return self_consumed_kwh * import_rate_average


def _tou_average(d: date, tariff: dict) -> float | None:
    """Mean of peak/std/off-peak on date d - used when an hourly breakdown
    is not available for the period (e.g. month/year/lifetime totals)."""
    rates = rate_for(d, tariff)
    if not rates or "peak" not in rates:
        return None
    vals = [v for v in (rates.get("peak"), rates.get("standard"),
                         rates.get("off_peak")) if v is not None]
    return sum(vals) / len(vals) if vals else None


def compute_period(energy: dict, tariff: dict, period_date: date,
                   hourly_import: list[dict] | None = None) -> dict:
    """Compute one period's financial figures.

    `energy` is the period dict {pv, import, export, charge, discharge,
    consumption, self_consumed}. `period_date` is any date inside the period -
    used to look up which rate row applies. `hourly_import` is required for
    TOU import-cost accuracy; absent, the calc falls back to the period's
    average TOU rate, which is good enough for monthly/yearly summaries.
    """
    if not tariff or not energy:
        return _empty_financial()

    t_type = tariff.get("type")
    rates = rate_for(period_date, tariff)
    if not rates:
        return _empty_financial()

    imp = energy.get("import") or 0.0
    exp = energy.get("export") or 0.0
    pv = energy.get("pv") or 0.0
    sc = energy.get("self_consumed") or 0.0

    # 1. Import cost
    if t_type == "tou" and hourly_import:
        cost_import = _import_cost_tou(hourly_import, tariff)
    elif t_type == "tou":
        avg = _tou_average(period_date, tariff)
        cost_import = _import_cost_flat(imp, avg)
    elif t_type == "flat":
        cost_import = _import_cost_flat(imp, rates.get("flat"))
    else:                       # ppa - import still bought at grid; tariff
        cost_import = 0.0       # would need a 'grid' block too. Default to 0.

    # 2. Export revenue (only when feed-in mode is configured)
    rate_export = export_rate_for(period_date, tariff)
    revenue_export = exp * rate_export if rate_export else 0.0

    # 3. PPA cost - customer pays for self-consumed PV at the PPA rate
    ppa_cost = 0.0
    if t_type == "ppa":
        ppa_cost = sc * (rates.get("ppa") or 0.0)

    # 4. Savings = self-consumed kWh valued at what they'd have cost from grid
    if t_type == "tou":
        rate_for_savings = _tou_average(period_date, tariff)
    elif t_type == "flat":
        rate_for_savings = rates.get("flat")
    else:                       # ppa: 'savings' isn't really meaningful, but
        rate_for_savings = None # report 0 so the field exists in the schema.
    savings = _savings(sc, rate_for_savings)

    net = savings + revenue_export - ppa_cost

    return {
        "cost_import":    round(cost_import, 2),
        "revenue_export": round(revenue_export, 2),
        "ppa_cost":       round(ppa_cost, 2),
        "savings":        round(savings, 2),
        "net":            round(net, 2),
    }


def _empty_financial() -> dict:
    return {
        "cost_import":    None,
        "revenue_export": None,
        "ppa_cost":       None,
        "savings":        None,
        "net":            None,
    }


# ---------------------------------------------------------------------------
# Top-level helper: build the whole financial block from a data.json energy
# block + tariff. Used by processor.py.
# ---------------------------------------------------------------------------

def compute_financials(data: dict, tariff: dict) -> dict:
    """Take the data.json energy block + tariff and return the financial block.

    `data` is expected to have data['energy'] with today/month/year/lifetime
    keys; today/lifetime/month_hourly arrays are used for TOU accuracy when
    present.
    """
    energy = data.get("energy", {})
    today_d = datetime.now(tz=SAST).date()

    today = compute_period(
        energy.get("today", {}), tariff, today_d,
        hourly_import=(energy.get("hourly", {}) or {}).get("import"),
    )
    # For month/year/lifetime we don't have an hourly-rate-aware breakdown,
    # so we use the TOU average for the relevant date - good enough for
    # summaries, and clearly documented in schema.md.
    month = compute_period(energy.get("month", {}), tariff,
                           today_d.replace(day=1))
    year = compute_period(energy.get("year", {}), tariff,
                          today_d.replace(month=1, day=1))
    lifetime = compute_period(energy.get("lifetime", {}), tariff, today_d)

    return {
        "today":    today,
        "month":    month,
        "year":     year,
        "lifetime": lifetime,
    }
