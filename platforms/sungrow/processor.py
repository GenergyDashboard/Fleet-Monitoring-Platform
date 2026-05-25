"""
Sungrow iSolarCloud response -> shared schema
=============================================
Maps iSolarCloud API responses to our common data.json shape.

Field mapping (from /openapi/getPowerStationDetail + /queryPowerStationData):
  curr_power           - current PV power (W)
  total_energy         - lifetime kWh
  today_energy         - today's kWh
  month_energy         - month-to-date kWh
  year_energy          - year-to-date kWh
  ps_status            - 1=normal, 2=offline, 3=fault
  ps_capacity_kw       - installed capacity

NOTE: These mappings are based on the documented iSolarCloud V1 API. Field
names may vary between regional gateways. Compare against the actual
response after the first run and adjust _bucket() / _hourly_from_today()
if anything is off.
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
    """Map iSolarCloud response fields to standard bucket."""
    return {
        "pv":          _f(d, "p83025", "today_energy", "month_energy",
                            "year_energy", "total_energy", "energy"),
        "consumption": _f(d, "consumption", "use_energy", "load_energy"),
        "import":      _f(d, "grid_import", "buy_energy"),
        "export":      _f(d, "grid_export", "sell_energy"),
        "charge":      _f(d, "battery_charge", "charge_energy"),
        "discharge":   _f(d, "battery_discharge", "discharge_energy"),
    }


def _hourly_from_today(today: dict) -> dict:
    """5-min interval data; aggregate to hourly buckets by cumulative-diff."""
    items = today.get("data_list") or today.get("dataList") or today.get("list") or []
    today_str = datetime.now(tz=SAST).strftime("%Y-%m-%d")
    out = {k: [] for k in ("pv", "consumption", "import", "export",
                             "charge", "discharge")}

    # Sungrow's "p83025" is generation; other point IDs vary. Aggregate
    # by hour using the last value of each hour.
    by_hour = {}
    for r in items:
        t = r.get("time_str") or r.get("time") or r.get("data_time")
        if not t: continue
        time_str = str(t)
        # Common formats: 'YYYYMMDDHHMMSS', 'YYYY-MM-DD HH:MM:SS', 'HH:MM'
        if len(time_str) == 14 and time_str.isdigit():
            time_str = f"{time_str[:4]}-{time_str[4:6]}-{time_str[6:8]} {time_str[8:10]}:00:00"
            hr = int(time_str.split()[1].split(":")[0])
        elif " " in time_str and ":" in time_str:
            hr = int(time_str.split()[1].split(":")[0])
            time_str = time_str[:13] + ":00:00"
        elif ":" in time_str:
            hr = int(time_str.split(":")[0])
            time_str = f"{today_str} {hr:02d}:00:00"
        else:
            continue
        by_hour[hr] = {
            "time": time_str,
            "pv":          _f(r, "p83025", "p83022", "generation", "today_energy"),
            "consumption": _f(r, "consumption", "load"),
            "import":      _f(r, "grid_import", "buy"),
            "export":      _f(r, "grid_export", "sell"),
            "charge":      _f(r, "battery_charge", "charge"),
            "discharge":   _f(r, "battery_discharge", "discharge"),
        }

    # Compute hourly DELTAS from cumulative values (Sungrow returns
    # cumulative-day values at each interval point).
    prev = {m: 0.0 for m in ("pv", "consumption", "import", "export", "charge", "discharge")}
    for hr in sorted(by_hour):
        e = by_hour[hr]
        for metric in prev:
            cur = e[metric]
            delta = max(0.0, cur - prev[metric])
            out[metric].append({"time": e["time"], "value": round(delta, 3)})
            prev[metric] = cur
    return out


def build_data_json(config: dict, realtime: dict, today: dict,
                     month: dict, year: dict, lifetime: dict) -> dict:

    pv_w = _f(realtime, "curr_power", "p13002", "currentPower")
    current_pv_kw = round(pv_w / 1000, 3) if pv_w > 100 else round(pv_w, 3)
    today_pv = _f(realtime, "today_energy", "p13003", "p83025")

    sg_status = realtime.get("ps_status")
    now_hr = datetime.now(tz=SAST).hour
    is_daylight = 6 <= now_hr <= 18
    if sg_status == 2:
        status = "offline"; severity = "warning"
        reason = "iSolarCloud reports station offline"
    elif sg_status == 3:
        status = "fault"; severity = "error"
        reason = "iSolarCloud reports fault state"
    elif is_daylight and current_pv_kw < 0.1 and today_pv < 0.5:
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
        "platform":  "sungrow",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":    current_pv_kw,
            "today_kwh":   today_pv,
            "status":      status,
            "status_severity": severity,
            "status_reason":   reason,
        },
        "energy": {
            "today":    _bucket({"today_energy": today_pv, **(today or {})}),
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
    items = month.get("data_list") or month.get("dataList") or month.get("list") or []
    for r in items:
        d = r.get("time_str") or r.get("date_id") or r.get("date")
        if not d: continue
        d = str(d)
        if len(d) == 8 and d.isdigit():
            d = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        elif len(d) < 10: continue
        else: d = d[:10]
        entry = by_date.get(d, {"date": d})
        entry["pv_kwh"]          = _f(r, "p83025", "today_energy", "generation")
        entry["consumption_kwh"] = _f(r, "consumption", "use_energy")
        entry["import_kwh"]      = _f(r, "grid_import", "buy_energy")
        entry["export_kwh"]      = _f(r, "grid_export", "sell_energy")
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
                realtime: dict, today: dict, month: dict,
                year: dict, lifetime: dict) -> None:
    data = build_data_json(config, realtime, today, month, year, lifetime)
    hourly_path = site_dir / "hourly_history.json"
    hourly_history = merge_hourly_history(config, _read_json(hourly_path), data)
    _write_json(hourly_path, hourly_history)
    data["performance"] = build_performance(data, config, hourly_history)
    history_path = site_dir / "history.json"
    _write_json(history_path, merge_history(config, _read_json(history_path), month))
    _write_json(site_dir / "data.json", data)
