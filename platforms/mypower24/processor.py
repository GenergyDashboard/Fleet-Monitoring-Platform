"""
MyPower24 (SolarMD) response -> shared schema
=============================================
Maps SolarMD V3 API responses into our standard data.json shape.

Energy endpoint returns an array of 15-minute interval records:
  {
    "serial": "...",
    "range": {"start": "2025-11-03T05:30Z", "end": "2025-11-03T05:45Z"},
    "load": {"export": {"active": {"value": 0.1, "unit": "kWh"}}},
    "grid": {"import": {"active": {...}}, "export": {"active": {...}}},
    "pv":   {"export": {"active": {"value": 0.6, "unit": "kWh"}}}
  }

Field mapping (note the metering convention):
  load.export.active   -> consumption  (energy the load consumed)
  grid.import.active   -> import        (drawn from utility grid)
  grid.export.active   -> export        (fed back to grid)
  pv.export.active     -> pv            (solar generated)

These are PER-INTERVAL energy values (kWh in each 15-min window), NOT
cumulative. So summing gives period totals, and grouping by hour gives
hourly buckets directly.

Battery: not in the energy endpoint. The variables endpoint provides
live SOC (state of charge) and grid power, matched by variable NAME
(e.g. "SOC", "Grid Power") since the IDs are device-specific.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SHARED_DIR = Path(__file__).resolve().parent.parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import powerflow                                  # noqa: E402
from financial import compute_financials          # noqa: E402
from performance import build_performance         # noqa: E402

SAST = timezone(timedelta(hours=2))
HISTORY_DAYS = 400


def _read_json(p: Path) -> dict | None:
    if not p.exists(): return None
    try: return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError: return None

def _write_json(p: Path, payload: dict) -> None:
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                  encoding="utf-8")

def _sast_now_iso() -> str:
    return datetime.now(tz=SAST).strftime("%Y-%m-%dT%H:%M:%S+02:00")


def _active(node: dict, direction: str) -> float:
    """Pull node[direction].active.value safely. e.g. _active(rec['pv'], 'export')."""
    try:
        v = node[direction]["active"]["value"]
        return float(v) if v is not None else 0.0
    except (KeyError, TypeError, ValueError):
        return 0.0


def _parse_interval_time(rec: dict) -> datetime | None:
    """Parse the interval start into a SAST-aware datetime."""
    try:
        start = rec["range"]["start"]        # e.g. '2025-11-03T05:30Z'
        # Normalise trailing Z to +00:00 for fromisoformat
        if start.endswith("Z"):
            start = start[:-1] + "+00:00"
        dt = datetime.fromisoformat(start)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(SAST)
    except (KeyError, ValueError, TypeError):
        return None


def _intervals_to_hourly(energy: list[dict]) -> dict:
    """Group 15-min intervals into hourly buckets, summing each metric."""
    hourly = defaultdict(lambda: {"pv": 0.0, "consumption": 0.0,
                                    "import": 0.0, "export": 0.0,
                                    "charge": 0.0, "discharge": 0.0})
    for rec in energy:
        dt = _parse_interval_time(rec)
        if dt is None:
            continue
        hour_key = dt.strftime("%Y-%m-%d %H:00:00")
        b = hourly[hour_key]
        b["consumption"] += _active(rec.get("load", {}), "export")
        b["import"]      += _active(rec.get("grid", {}), "import")
        b["export"]      += _active(rec.get("grid", {}), "export")
        b["pv"]          += _active(rec.get("pv", {}), "export")

    out = {k: [] for k in ("pv", "consumption", "import", "export",
                             "charge", "discharge")}
    for hour_key in sorted(hourly):
        b = hourly[hour_key]
        for metric in out:
            out[metric].append({"time": hour_key, "value": round(b[metric], 3)})
    return out


def _sum_energy(energy: list[dict]) -> dict:
    """Sum all intervals into a single period bucket."""
    bucket = {"pv": 0.0, "consumption": 0.0, "import": 0.0,
              "export": 0.0, "charge": 0.0, "discharge": 0.0}
    for rec in energy:
        bucket["consumption"] += _active(rec.get("load", {}), "export")
        bucket["import"]      += _active(rec.get("grid", {}), "import")
        bucket["export"]      += _active(rec.get("grid", {}), "export")
        bucket["pv"]          += _active(rec.get("pv", {}), "export")
    return {k: round(v, 3) for k, v in bucket.items()}


def _find_variable(variables: list[dict], *name_options) -> float | None:
    """Find a variable by its human-readable name (case-insensitive)."""
    names_lower = [n.lower() for n in name_options]
    for var in variables:
        vname = (var.get("name") or "").lower()
        if vname in names_lower:
            try:
                return float(var.get("value"))
            except (TypeError, ValueError):
                return None
    return None


def build_data_json(config: dict, energy: list[dict],
                     variables: list[dict]) -> dict:
    today_bucket = _sum_energy(energy)
    hourly = _intervals_to_hourly(energy)

    # Battery SoC + grid power from the variables endpoint (live snapshot)
    battery_soc = _find_variable(variables, "SOC", "State of Charge")
    grid_power_w = _find_variable(variables, "Grid Power")
    grid_power_kw = round(grid_power_w / 1000, 3) if grid_power_w else None

    # Current PV power: the energy endpoint is interval-based, so "current
    # power" isn't directly available. Use the most recent interval's PV kWh
    # extrapolated to kW (kWh in 15min * 4 = kW), as an approximation.
    current_pv_kw = 0.0
    if hourly["pv"]:
        # last non-zero interval-derived hourly value / appropriate factor
        recent = [e["value"] for e in hourly["pv"] if e["value"] > 0]
        if recent:
            current_pv_kw = round(recent[-1], 3)      # kWh in last hour ~= avg kW

    today_pv = today_bucket["pv"]
    now_hr = datetime.now(tz=SAST).hour
    is_daylight = 6 <= now_hr <= 18
    if is_daylight and today_pv < 0.5 and current_pv_kw < 0.1:
        status = "underperforming"; severity = "warning"
        reason = "Daylight but little/no PV generation today"
    elif current_pv_kw > 0.1 or today_pv > 0.5:
        status = "online"; severity = "ok"; reason = "Generating normally"
    elif not is_daylight:
        status = "offline"; severity = "info"; reason = "Outside daylight hours"
    else:
        status = "unknown"; severity = "info"; reason = "Insufficient data"

    data = {
        "site_id":    config["site_id"],
        "platform":   "mypower24",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":        current_pv_kw,
            "today_kwh":       today_pv,
            "status":          status,
            "status_severity": severity,
            "status_reason":   reason,
            "battery_soc":     battery_soc,
            "grid_power_kw":   grid_power_kw,
        },
        "energy": {
            "today":    today_bucket,
            "month":    today_bucket,        # placeholder; filled by history
            "year":     today_bucket,        # placeholder
            "lifetime": today_bucket,        # placeholder
            "hourly":   hourly,
        },
        "energy_powerflow_hourly": _build_powerflow_hourly(hourly),
        "irradiation": None,
        "irradiation_forecast": None,
        "financial": None,
        "_mypower24_meta": {
            "serial": config["serial"],
            "battery_soc": battery_soc,
        },
    }

    tariff = config.get("tariff")
    if tariff:
        data["financial"] = compute_financials(data, tariff)
    return data


def _build_powerflow_hourly(hourly: dict) -> list[dict]:
    by_time = {}
    for metric in ("pv", "consumption", "import", "export", "charge", "discharge"):
        for entry in hourly.get(metric, []):
            by_time.setdefault(entry["time"], {})[metric] = entry["value"]
    out = []
    for t in sorted(by_time):
        m = by_time[t]
        flows = powerflow.split(
            pv=m.get("pv", 0), grid_import=m.get("import", 0),
            export=m.get("export", 0), charge=m.get("charge", 0),
            discharge=m.get("discharge", 0), load=m.get("consumption", 0),
        )
        out.append({"time": t, **flows})
    return out


def merge_history(config: dict, existing: dict | None,
                   today_bucket: dict) -> dict:
    days = (existing or {}).get("days", [])
    today_str = datetime.now(tz=SAST).strftime("%Y-%m-%d")
    by_date = {d.get("date"): d for d in days if d.get("date")}
    entry = by_date.get(today_str, {"date": today_str})
    # Today's values keep growing through the day; keep the max we've seen
    entry["pv_kwh"]          = max(entry.get("pv_kwh", 0), today_bucket["pv"])
    entry["consumption_kwh"] = max(entry.get("consumption_kwh", 0), today_bucket["consumption"])
    entry["import_kwh"]      = max(entry.get("import_kwh", 0), today_bucket["import"])
    entry["export_kwh"]      = max(entry.get("export_kwh", 0), today_bucket["export"])
    by_date[today_str] = entry
    merged = sorted(by_date.values(), key=lambda x: x.get("date", ""))
    if HISTORY_DAYS:
        merged = merged[-HISTORY_DAYS:]
    return {"site_id": config["site_id"], "days": merged}


def merge_hourly_history(config: dict, existing: dict | None, data: dict) -> dict:
    hours = (existing or {}).get("hours", [])
    by_time = {h.get("time"): h for h in hours if h.get("time")}
    for metric in ("pv", "consumption", "import", "export", "charge", "discharge"):
        for entry in data["energy"]["hourly"].get(metric, []):
            row = by_time.get(entry["time"]) or {"time": entry["time"]}
            row[metric] = entry["value"]
            by_time[entry["time"]] = row
    for flows in data.get("energy_powerflow_hourly", []):
        row = by_time.get(flows["time"]) or {"time": flows["time"]}
        for k, v in flows.items():
            if k != "time": row[k] = v
        by_time[flows["time"]] = row
    merged = sorted(by_time.values(), key=lambda x: x.get("time", ""))
    return {"site_id": config["site_id"], "hours": merged}


def write_site(site_dir: Path, config: dict, *,
                energy: list[dict], variables: list[dict]) -> None:
    data = build_data_json(config, energy, variables)

    hourly_path = site_dir / "hourly_history.json"
    hourly_history = merge_hourly_history(config, _read_json(hourly_path), data)
    _write_json(hourly_path, hourly_history)

    data["performance"] = build_performance(data, config, hourly_history)

    history_path = site_dir / "history.json"
    history = merge_history(config, _read_json(history_path),
                             data["energy"]["today"])
    _write_json(history_path, history)

    _write_json(site_dir / "data.json", data)
