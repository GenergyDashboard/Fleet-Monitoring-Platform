"""
Sigenergy response -> shared schema
===================================
NOTE: Sigenergy's openapi field names are less well documented than the
other platforms. The mappings below are based on Sigenergy's general API
conventions; expect minor adjustments after the first real run. The
processor falls back gracefully when fields are missing.
"""

from __future__ import annotations

import json
import sys
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

def _f(d: dict, *keys, default=0.0) -> float:
    if not isinstance(d, dict): return default
    for k in keys:
        v = d.get(k)
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): continue
    return default


def _bucket(d: dict) -> dict:
    """Map Sigenergy response fields to our bucket."""
    return {
        "pv":          _f(d, "pvGeneration", "pvEnergy", "generation", "pv"),
        "consumption": _f(d, "consumption", "loadEnergy", "load"),
        "import":      _f(d, "gridImport", "buyEnergy", "import"),
        "export":      _f(d, "gridExport", "sellEnergy", "export"),
        "charge":      _f(d, "batteryCharge", "chargeEnergy", "charge"),
        "discharge":   _f(d, "batteryDischarge", "dischargeEnergy", "discharge"),
    }


def _hourly_from_today(today: dict) -> dict:
    """Extract hourly series if today's response includes a list of time-buckets."""
    out = {k: [] for k in ("pv", "consumption", "import", "export",
                             "charge", "discharge")}
    items = today.get("hourly") or today.get("records") or today.get("dataList") or []
    today_str = datetime.now(tz=SAST).strftime("%Y-%m-%d")
    for r in items:
        t = r.get("time") or r.get("timestamp") or r.get("dataTime")
        if not t: continue
        if isinstance(t, (int, float)) and t > 1_000_000_000:
            if t > 10_000_000_000: t //= 1000
            time_str = datetime.fromtimestamp(t, tz=SAST).strftime("%Y-%m-%d %H:%M:%S")
        elif ":" in str(t):
            time_str = str(t) if " " in str(t) else f"{today_str} {str(t)[:5]}:00"
        else:
            continue
        out["pv"].append({"time": time_str, "value": _f(r, "pvGeneration", "pv", "generation")})
        out["consumption"].append({"time": time_str, "value": _f(r, "consumption", "load")})
        out["import"].append({"time": time_str, "value": _f(r, "gridImport", "import")})
        out["export"].append({"time": time_str, "value": _f(r, "gridExport", "export")})
        out["charge"].append({"time": time_str, "value": _f(r, "batteryCharge", "charge")})
        out["discharge"].append({"time": time_str, "value": _f(r, "batteryDischarge", "discharge")})
    return out


def build_data_json(config: dict, overview: dict, today: dict,
                     month: dict, year: dict, lifetime: dict) -> dict:
    current_pv_kw = _f(overview, "pvPower", "currentPvPower", "power") / (
        1000 if _f(overview, "pvPower") > 100 else 1)
    current_pv_kw = round(current_pv_kw, 3)
    today_pv = _f(today, "pvGeneration", "pvEnergy", "generation")

    now_hr = datetime.now(tz=SAST).hour
    is_daylight = 6 <= now_hr <= 18
    if is_daylight and current_pv_kw < 0.1 and today_pv < 0.5:
        status = "underperforming"; severity = "warning"
        reason = "Daylight but no PV generation today"
    elif current_pv_kw > 0.1:
        status = "online"; severity = "ok"; reason = "Generating normally"
    elif not is_daylight:
        status = "offline"; severity = "info"; reason = "Outside daylight hours"
    else:
        status = "unknown"; severity = "info"; reason = "Insufficient data"

    hourly = _hourly_from_today(today)
    data = {
        "site_id":   config["site_id"],
        "platform":  "sigenergy",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":    current_pv_kw,
            "today_kwh":   today_pv,
            "status":      status,
            "status_severity": severity,
            "status_reason":   reason,
        },
        "energy": {
            "today":    _bucket(today),
            "month":    _bucket(month),
            "year":     _bucket(year),
            "lifetime": _bucket(lifetime),
            "hourly":   hourly,
        },
        "energy_powerflow_hourly": _build_powerflow_hourly(hourly),
        "irradiation": None,
        "irradiation_forecast": None,
        "financial": None,
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


def merge_history(config: dict, existing: dict | None, month: dict) -> dict:
    days = (existing or {}).get("days", [])
    by_date = {d.get("date"): d for d in days if d.get("date")}
    items = month.get("daily") or month.get("records") or month.get("dataList") or []
    for r in items:
        d = r.get("date") or r.get("day") or r.get("time")
        if not d or len(str(d)) < 10: continue
        d = str(d)[:10]
        entry = by_date.get(d, {"date": d})
        entry["pv_kwh"]          = _f(r, "pvGeneration", "pv", "generation")
        entry["consumption_kwh"] = _f(r, "consumption", "load")
        entry["import_kwh"]      = _f(r, "gridImport", "import")
        entry["export_kwh"]      = _f(r, "gridExport", "export")
        by_date[d] = entry
    merged = sorted(by_date.values(), key=lambda x: x.get("date", ""))
    if HISTORY_DAYS: merged = merged[-HISTORY_DAYS:]
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
                overview: dict, today: dict, month: dict,
                year: dict, lifetime: dict) -> None:
    data = build_data_json(config, overview, today, month, year, lifetime)
    hourly_path = site_dir / "hourly_history.json"
    hourly_history = merge_hourly_history(config, _read_json(hourly_path), data)
    _write_json(hourly_path, hourly_history)
    data["performance"] = build_performance(data, config, hourly_history)
    history_path = site_dir / "history.json"
    _write_json(history_path, merge_history(config, _read_json(history_path), month))
    _write_json(site_dir / "data.json", data)
