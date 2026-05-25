"""
FusionSolar processor
=====================
Maps raw Huawei Northbound API responses onto the normalized schema defined in
shared/schema.md, then writes data.json and history.json into each site folder.

This module is platform-specific. The *output* it produces is platform-agnostic:
GoodWe / Solis / Sunsynk / Solarman processors must produce the exact same shape.

It is imported and called by fetch.py - it is not run directly.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make shared/ importable so powerflow + financial are reachable.
_SHARED_DIR = Path(__file__).resolve().parent.parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import powerflow                                  # noqa: E402
from financial import compute_financials          # noqa: E402
from performance import build_performance         # noqa: E402

# SAST is a fixed UTC+2 offset, no daylight saving - safe to hardcode.
SAST = timezone(timedelta(hours=2))

# How many days of daily history to retain in history.json.
# 400 keeps a full year-plus so actual-vs-predicted comparison against
# predictions.min.json (2025-2044) has something to plot.
HISTORY_DAYS = 400

# How many days of HOURLY history to retain. None = unlimited (keep every hour
# we ever fetch). At ~1.7 MB per site per year for the full set of fields, even
# 5 years sits at <10 MB per site - manageable. If the files ever get
# unwieldy, sharding by year (hourly_history_2026.json, etc) is the
# next move.
HOURLY_HISTORY_DAYS = None


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def epoch_ms_to_sast(ms: int | float | None) -> str | None:
    """Convert Huawei epoch-milliseconds (UTC) to a SAST 'YYYY-MM-DD HH:MM:SS' string."""
    if ms is None:
        return None
    dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).astimezone(SAST)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def epoch_ms_to_date(ms: int | float | None) -> str | None:
    """Convert Huawei epoch-milliseconds (UTC) to a SAST 'YYYY-MM-DD' date string."""
    s = epoch_ms_to_sast(ms)
    return s.split(" ")[0] if s else None


def now_sast_iso() -> str:
    """Current time as full ISO 8601 with the +02:00 offset, for data.json updated_at."""
    return datetime.now(tz=SAST).strftime("%Y-%m-%dT%H:%M:%S%z")[:-2] + ":00"


# --------------------------------------------------------------------------
# Value helpers
# --------------------------------------------------------------------------

def num(value) -> float | None:
    """Coerce a FusionSolar value to float. Huawei returns numbers, strings or None.

    Their sentinel for 'no data' is the string '-1' or a literal -1; we treat
    those as null rather than letting a -1 kWh leak into a chart.
    """
    if value is None or value == "" or value == "N/A":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if f == -1 else f


_HEALTH_MAP = {1: "offline", 2: "fault", 3: "online"}


def health_to_status(code) -> str:
    """Map FusionSolar real_health_state (1/2/3) to a schema status string."""
    try:
        return _HEALTH_MAP.get(int(code), "unknown")
    except (TypeError, ValueError):
        return "unknown"


def detect_status(real_kpi: dict, today_hourly: list[dict],
                   capacity_kwp: float | None) -> dict:
    """Determine site status with reasoning.

    real_health_state is the primary signal but it lies on quiet failures
    (e.g. inverter reporting online while producing zero through clear
    daylight hours). So we check a few other things and produce both the
    final status and a 'reason' field that says WHY.

    Returns: {'status': ..., 'reason': ..., 'severity': 'ok'|'warn'|'critical'}

    Status values:
      'online'     - reporting and producing as expected
      'offline'    - real_health_state says offline OR no data in many hours
      'fault'      - real_health_state says fault
      'underperforming' - reporting but well below expected for the hour
      'no_data'    - never reported today
      'unknown'    - cannot tell
    """
    m = (real_kpi or {}).get("dataItemMap", {}) or {}
    health = m.get("real_health_state")

    # Critical signals first - the API directly says fault or offline.
    if health is not None:
        try:
            h = int(health)
        except (TypeError, ValueError):
            h = None
        if h == 2:
            return {"status": "fault", "severity": "critical",
                    "reason": "Inverter reports fault state."}
        if h == 1:
            return {"status": "offline", "severity": "critical",
                    "reason": "Inverter reports offline."}

    # Heuristic: zero PV throughout clear daylight hours when we should be
    # generating. This catches the 'reporting but dead' case real_health_state
    # misses. Daylight = 09:00-15:00 SAST, central window where any clear day
    # should produce.
    daylight_hours = [e for e in (today_hourly or [])
                       if _hour_in_daylight(e.get("time"))]
    if daylight_hours:
        produced_any = any((e.get("pv_kwh") or 0) > 0 for e in daylight_hours)
        if not produced_any:
            return {"status": "underperforming", "severity": "warn",
                    "reason": "Zero PV across daylight hours despite reporting."}

    # day_power exists and is reasonable -> online.
    day_power = m.get("day_power")
    if day_power is not None:
        return {"status": "online", "severity": "ok", "reason": None}

    # Nothing at all.
    return {"status": "no_data", "severity": "warn",
            "reason": "No data returned from API."}


def _hour_in_daylight(ts: str | None) -> bool:
    """True if the timestamp falls 09:00-15:00 SAST. Central daylight window
    where any clear day should be producing meaningfully."""
    if not ts:
        return False
    try:
        h = int(ts.split(" ")[1].split(":")[0])
    except (ValueError, IndexError):
        return False
    return 9 <= h <= 15


# --------------------------------------------------------------------------
# Normalizers - one per FusionSolar endpoint
# --------------------------------------------------------------------------

def normalize_real_kpi(item: dict) -> dict:
    """Turn one getStationRealKpi data item into the schema 'current' block.

    item['dataItemMap'] keys of interest:
      day_power, month_power, total_power, real_health_state
    Instantaneous power is not in this endpoint for all firmware versions; when
    absent it is filled later from the most recent today_hourly entry.
    """
    m = item.get("dataItemMap", {}) or {}
    return {
        "power_kw": None,  # backfilled in build_data_json from the hourly curve
        "today_kwh": num(m.get("day_power")),
        "month_kwh": num(m.get("month_power")),
        "total_kwh": num(m.get("total_power")),
        "status": health_to_status(m.get("real_health_state")),
    }


def normalize_hourly(rows: list[dict]) -> tuple[list[dict], list[dict], dict]:
    """Turn getKpiStationHour rows into (today_hourly, irradiation_hourly, energy_hourly).

    The third return is the energy-flow hourly arrays keyed by field name
    (import/export/charge/discharge/pv), used by the energy block.

    Each row: { collectTime: epoch_ms, dataItemMap: { inverter_power, ongrid_power,
                buyPower, chargeCap, dischargeCap, radiation_intensity, ... } }
    """
    pv, irr = [], []
    energy_hourly = {"pv": [], "import": [], "export": [],
                     "charge": [], "discharge": []}
    for row in sorted(rows, key=lambda r: r.get("collectTime", 0)):
        ts = epoch_ms_to_sast(row.get("collectTime"))
        if ts is None:
            continue
        m = row.get("dataItemMap", {}) or {}
        # Legacy pv array (kept for backward compat with the today_hourly field)
        pv.append({"time": ts, "pv_kwh": num(m.get("inverter_power")) or 0.0})
        # Keep radiation as None when absent - do NOT coerce to 0.0. A null here
        # means the site has no EMI sensor; build_data_json detects that and
        # leaves irradiation for the shared weather module to fill.
        irr.append({"time": ts, "value": num(m.get("radiation_intensity"))})
        # Energy-flow hourly arrays - coerce None -> 0.0 for these since a missing
        # value at an hour mark genuinely means zero flow (the meter recorded nothing).
        energy_hourly["pv"].append(
            {"time": ts, "value": num(m.get("PVYield")) or num(m.get("inverter_power")) or 0.0})
        energy_hourly["import"].append(
            {"time": ts, "value": num(m.get("buyPower")) or 0.0})
        energy_hourly["export"].append(
            {"time": ts, "value": num(m.get("ongrid_power")) or 0.0})
        energy_hourly["charge"].append(
            {"time": ts, "value": num(m.get("chargeCap")) or 0.0})
        energy_hourly["discharge"].append(
            {"time": ts, "value": num(m.get("dischargeCap")) or 0.0})
    return pv, irr, energy_hourly


def normalize_period(item: dict) -> dict:
    """Pull the six energy-flow fields out of one daily/monthly/yearly KPI row.

    Plus a derived self_consumed value. Energy conservation gives us:
        import = import_to_load + import_to_battery
    where import_to_battery <= charge. So the upper bound on grid going to
    load is max(0, import - charge). The rest of consumption was served by
    PV or battery discharge - that is self_consumed.
    """
    m = item.get("dataItemMap", {}) or {}
    imp = num(m.get("buyPower")) or 0.0
    cons = num(m.get("use_power")) or 0.0
    charge = num(m.get("chargeCap")) or 0.0
    import_to_load = max(0.0, imp - charge)
    return {
        "pv":            num(m.get("PVYield")) or num(m.get("inverter_power")) or 0.0,
        "import":        imp,
        "export":        num(m.get("ongrid_power")) or 0.0,
        "charge":        charge,
        "discharge":     num(m.get("dischargeCap")) or 0.0,
        "consumption":   cons,
        "self_consumed": max(0.0, cons - import_to_load),
    }


def normalize_daily(rows: list[dict]) -> list[dict]:
    """Turn getKpiStationDay rows into a list of schema history-day dicts.

    Each row: { collectTime: epoch_ms, dataItemMap: { inverter_power, radiation_intensity } }
    """
    out = []
    for row in rows:
        date = epoch_ms_to_date(row.get("collectTime"))
        if date is None:
            continue
        m = row.get("dataItemMap", {}) or {}
        out.append({
            "date": date,
            "pv_kwh": num(m.get("inverter_power")),
            "irradiation": num(m.get("radiation_intensity")),
        })
    return out


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def build_data_json(config: dict, real_kpi: dict, hourly_rows: list[dict],
                    daily_rows: list[dict] | None = None,
                    monthly_rows: list[dict] | None = None,
                    yearly_rows: list[dict] | None = None) -> dict:
    """Assemble a complete schema-compliant data.json dict for one site.

    daily/monthly/yearly rows are optional - when present they populate the
    energy block's month/year/lifetime period totals. When absent (e.g. an
    early test run), the corresponding blocks are left empty rather than
    invented from partial data.
    """
    today_hourly, irr_hourly, energy_hourly = normalize_hourly(hourly_rows)
    current = normalize_real_kpi(real_kpi) if real_kpi else {
        "power_kw": None, "today_kwh": None, "month_kwh": None,
        "total_kwh": None, "status": "unknown",
    }

    # FusionSolar realKpi often omits instantaneous power - approximate it from
    # the most recent non-zero hour so the dashboard still has a live figure.
    if current.get("power_kw") is None:
        for entry in reversed(today_hourly):
            if entry["pv_kwh"]:
                current["power_kw"] = entry["pv_kwh"]
                break

    # Status determination - richer than the raw real_health_state, picks up
    # quiet failures like 'reporting but zero PV all day'.
    status_info = detect_status(real_kpi, today_hourly, config.get("capacity_kwp"))
    current["status"] = status_info["status"]
    current["status_severity"] = status_info["severity"]
    current["status_reason"] = status_info["reason"]

    # FusionSolar only returns radiation_intensity for sites that have a
    # physical environmental monitoring instrument (EMI / pyranometer) wired
    # into the SmartLogger. Most rooftop sites have none. Use native
    # irradiation ONLY when the API returns a real positive reading; otherwise
    # leave the block empty so weather/refresh_irradiation.py fills it from
    # Open-Meteo - exactly like every non-FusionSolar platform.
    has_native_irr = any(
        e["value"] is not None and e["value"] > 0 for e in irr_hourly
    )
    if has_native_irr:
        irradiation = {
            "source": "fusionsolar",
            "today_total": round(
                sum(e["value"] for e in irr_hourly if e["value"] is not None), 2
            ),
            "today_hourly": irr_hourly,
        }
    else:
        irradiation = {
            "source": None,        # weather module sets this to "open-meteo"
            "today_total": None,
            "today_hourly": [],
        }

    # ----- Energy block -----
    # Today is built from the hourly sums (most reliable source for today,
    # since the period endpoint may not yet have today's row populated).
    # Powerflow is applied per-hour to get the seven directional flows.
    today_energy, powerflow_hourly = _apply_powerflow_to_hourly(energy_hourly)

    # Month / year / lifetime come from the period endpoints, which return
    # ONE ROW PER DAY-IN-MONTH (resp. month-in-year, year-in-lifetime). To get
    # the period total we sum the rows - picking the latest would give just
    # the last day, not the month.
    month_energy = _sum_period(daily_rows)        # month = sum of daily rows
    year_energy  = _sum_period(monthly_rows)      # year  = sum of monthly rows
    lifetime_energy = _sum_period(yearly_rows)    # lifetime = sum of yearly rows

    data = {
        "site_id": config["site_id"],
        "name": config["name"],
        "platform": config["platform"],
        "capacity_kwp": config.get("capacity_kwp"),
        "updated_at": now_sast_iso(),
        "current": current,
        "today_hourly": today_hourly,
        "irradiation": irradiation,
        "energy": {
            "today":    today_energy,
            "month":    month_energy,
            "year":     year_energy,
            "lifetime": lifetime_energy,
            "hourly":   energy_hourly,
            "powerflow_hourly": powerflow_hourly,
        },
    }

    # ----- Financial block -----
    # Only computed when the site has a tariff config; left empty otherwise so
    # incomplete configs render gracefully rather than blocking ingestion.
    tariff = config.get("tariff")
    data["financial"] = compute_financials(data, tariff) if tariff else None

    return data


def _apply_powerflow_to_hourly(energy_hourly: dict) -> tuple[dict, list[dict]]:
    """Run powerflow.split on each hour, returning (today_totals, hourly_splits).

    The hourly endpoint does not expose use_power per hour, so today's
    per-hour load is derived from energy conservation:
        load = pv + import + discharge - export - charge

    For each hour we then run the dual-anchor split, and finally sum the
    splits into today's totals - this is the right level to do it at, since
    a period-total split squashes time-of-day patterns.
    """
    hours_pv   = energy_hourly.get("pv",        []) or []
    hours_imp  = energy_hourly.get("import",    []) or []
    hours_exp  = energy_hourly.get("export",    []) or []
    hours_chg  = energy_hourly.get("charge",    []) or []
    hours_dis  = energy_hourly.get("discharge", []) or []
    n = min(len(hours_pv), len(hours_imp), len(hours_exp),
            len(hours_chg), len(hours_dis))

    totals = {"pv": 0.0, "import": 0.0, "export": 0.0,
              "charge": 0.0, "discharge": 0.0, "consumption": 0.0,
              "pv_to_load": 0.0, "pv_to_batt": 0.0, "pv_to_grid": 0.0,
              "batt_to_load": 0.0, "batt_to_grid": 0.0,
              "grid_to_load": 0.0, "grid_to_batt": 0.0}
    hourly_splits = []

    for i in range(n):
        pv = hours_pv[i].get("value") or 0.0
        imp = hours_imp[i].get("value") or 0.0
        exp = hours_exp[i].get("value") or 0.0
        chg = hours_chg[i].get("value") or 0.0
        dis = hours_dis[i].get("value") or 0.0
        load = max(0.0, pv + imp + dis - exp - chg)
        f = powerflow.split(pv, imp, exp, chg, dis, load)

        totals["pv"]        += pv
        totals["import"]    += imp
        totals["export"]    += exp
        totals["charge"]    += chg
        totals["discharge"] += dis
        totals["consumption"] += load
        for k in ("pv_to_load", "pv_to_batt", "pv_to_grid",
                  "batt_to_load", "batt_to_grid",
                  "grid_to_load", "grid_to_batt"):
            totals[k] += f[k]

        hourly_splits.append({
            "time":         hours_pv[i].get("time"),
            "pv_to_load":   round(f["pv_to_load"], 4),
            "pv_to_batt":   round(f["pv_to_batt"], 4),
            "pv_to_grid":   round(f["pv_to_grid"], 4),
            "batt_to_load": round(f["batt_to_load"], 4),
            "batt_to_grid": round(f["batt_to_grid"], 4),
            "grid_to_load": round(f["grid_to_load"], 4),
            "grid_to_batt": round(f["grid_to_batt"], 4),
        })

    today_totals = {k: round(v, 2) for k, v in totals.items()}
    # self_consumed is meaningful as a top-level convenience field too.
    today_totals["self_consumed"] = round(
        totals["pv_to_load"] + totals["batt_to_load"], 2)
    return today_totals, hourly_splits


def _sum_period(rows: list[dict] | None) -> dict:
    """Sum a list of daily/monthly/yearly KPI rows into one period total,
    with powerflow split applied per row before summing.

    The API endpoints return one row per day-in-month (getKpiStationDay), one
    per month-in-year (getKpiStationMonth), or one per year-in-lifetime
    (getKpiStationYear). Running powerflow at the daily/monthly level loses
    time-of-day resolution but is still the right unit - load patterns within
    a day are not visible to these endpoints regardless.
    """
    if not rows:
        return {}
    totals = {"pv": 0.0, "import": 0.0, "export": 0.0,
              "charge": 0.0, "discharge": 0.0, "consumption": 0.0,
              "pv_to_load": 0.0, "pv_to_batt": 0.0, "pv_to_grid": 0.0,
              "batt_to_load": 0.0, "batt_to_grid": 0.0,
              "grid_to_load": 0.0, "grid_to_batt": 0.0}
    for r in rows:
        p = normalize_period(r)
        f = powerflow.split(p["pv"], p["import"], p["export"],
                             p["charge"], p["discharge"], p["consumption"])
        totals["pv"]          += p["pv"]
        totals["import"]      += p["import"]
        totals["export"]      += p["export"]
        totals["charge"]      += p["charge"]
        totals["discharge"]   += p["discharge"]
        totals["consumption"] += p["consumption"]
        for k in ("pv_to_load", "pv_to_batt", "pv_to_grid",
                  "batt_to_load", "batt_to_grid",
                  "grid_to_load", "grid_to_batt"):
            totals[k] += f[k]

    out = {k: round(v, 2) for k, v in totals.items()}
    out["self_consumed"] = round(out["pv_to_load"] + out["batt_to_load"], 2)
    return out


def merge_history(config: dict, existing: dict | None, daily_rows: list[dict]) -> dict:
    """Merge freshly fetched daily rows into the existing history.json.

    New data wins on a date collision. Output is sorted ascending and trimmed
    to HISTORY_DAYS. Missing a run never loses data - the file just gap-fills
    next time the relevant month is fetched.
    """
    by_date: dict[str, dict] = {}
    if existing and isinstance(existing.get("days"), list):
        for day in existing["days"]:
            if day.get("date"):
                by_date[day["date"]] = day

    for day in normalize_daily(daily_rows):
        prev = by_date.get(day["date"], {})
        # Keep a previously known irradiation value if the new row has none.
        if day["irradiation"] is None and prev.get("irradiation") is not None:
            day["irradiation"] = prev["irradiation"]
        by_date[day["date"]] = day

    days = sorted(by_date.values(), key=lambda d: d["date"])[-HISTORY_DAYS:]
    return {"site_id": config["site_id"], "days": days}


# --------------------------------------------------------------------------
# Disk I/O
# --------------------------------------------------------------------------

def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def merge_hourly_history(config: dict, existing: dict | None,
                          data: dict) -> dict:
    """Append today's hourly energy + powerflow arrays to the rolling hourly
    history, dedup by timestamp, sort, and trim to HOURLY_HISTORY_DAYS.

    Structure:
        {
          "site_id": "...",
          "hours": [
            {
              "time": "YYYY-MM-DD HH:MM:SS",
              "pv": ..., "import": ..., "export": ...,
              "charge": ..., "discharge": ...,
              "pv_to_load": ..., "pv_to_batt": ..., ... (powerflow split)
            },
            ...
          ]
        }

    Built incrementally - each run adds today's hours (likely overwriting the
    most recent ones with fresher values) and keeps the trailing N days.
    """
    by_time: dict[str, dict] = {}
    if existing and isinstance(existing.get("hours"), list):
        for h in existing["hours"]:
            if h.get("time"):
                by_time[h["time"]] = h

    energy_hourly = data.get("energy", {}).get("hourly", {}) or {}
    powerflow_hourly = data.get("energy", {}).get("powerflow_hourly", []) or []

    # Build a {time: {pv, import, ...}} index from the metered energy fields.
    metered_by_time: dict[str, dict] = {}
    for field in ("pv", "import", "export", "charge", "discharge"):
        for entry in energy_hourly.get(field, []):
            t = entry.get("time")
            if not t:
                continue
            metered_by_time.setdefault(t, {"time": t})
            metered_by_time[t][field] = entry.get("value") or 0.0

    # Merge in the powerflow split per time.
    for split in powerflow_hourly:
        t = split.get("time")
        if not t or t not in metered_by_time:
            continue
        for k, v in split.items():
            if k != "time":
                metered_by_time[t][k] = v

    # Apply new hours over the existing history.
    by_time.update(metered_by_time)

    # Trim by date - HOURLY_HISTORY_DAYS=None means unlimited, keep everything.
    if HOURLY_HISTORY_DAYS is None:
        hours = sorted(by_time.values(), key=lambda h: h["time"])
    else:
        cutoff = (datetime.now(tz=SAST) -
                  timedelta(days=HOURLY_HISTORY_DAYS)).strftime("%Y-%m-%d")
        hours = sorted(
            (h for h in by_time.values()
             if h.get("time", "").split(" ")[0] >= cutoff),
            key=lambda h: h["time"],
        )
    return {"site_id": config["site_id"], "hours": hours}


def write_site(site_dir: Path, config: dict, real_kpi: dict,
               hourly_rows: list[dict], daily_rows: list[dict],
               monthly_rows: list[dict] | None = None,
               yearly_rows: list[dict] | None = None) -> None:
    """Build and write data.json + history.json + hourly_history.json.

    Write order matters: we need hourly_history to exist BEFORE computing
    performance (empirical sites read it for calibration), and data.json
    must be written AFTER performance (so the block is included). Daily
    history is independent and can go anywhere.
    """
    # 1. Build the data block (energy, financial, current, etc) without
    # the performance block yet.
    data = build_data_json(config, real_kpi, hourly_rows,
                           daily_rows, monthly_rows, yearly_rows)

    # 2. Update hourly_history.json - performance needs it for empirical calc.
    # Note: this only includes hours up to and including this fetch's results,
    # which is correct - we don't want to use today's partial data to
    # calibrate today's expected.
    hourly_path = site_dir / "hourly_history.json"
    existing_hourly = _read_json_or_none(hourly_path)
    hourly_history = merge_hourly_history(config, existing_hourly, data)
    _write_json(hourly_path, hourly_history)

    # 3. Compute performance, then attach to data.json before writing.
    # Irradiation may be empty here if the weather workflow hasn't run yet;
    # build_performance handles that gracefully (returns method='naive' with
    # zero expecteds, or method='empirical' if it can find historical ratios).
    data["performance"] = build_performance(data, config, hourly_history)

    # 4. Daily history is independent.
    history_path = site_dir / "history.json"
    existing = _read_json_or_none(history_path)
    history = merge_history(config, existing, daily_rows)
    _write_json(history_path, history)

    # 5. Finally write data.json with the full payload.
    _write_json(site_dir / "data.json", data)


def _read_json_or_none(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None  # corrupt file - rebuild from scratch
