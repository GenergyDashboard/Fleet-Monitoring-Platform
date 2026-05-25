"""
Sunsynk API response -> shared schema
=====================================
Maps Sunsynk Connect API responses into our common data.json /
history.json / hourly_history.json shape.

Sunsynk field mapping (mostly from /realtime + /energy):
  pac          - current PV power, W
  etoday       - today's PV generation, kWh
  emonth       - this month PV, kWh
  eyear        - this year PV, kWh
  etotal       - lifetime PV, kWh
  load         - current load, W
  toGrid       - export to grid (cumulative period), kWh
  fromGrid     - import from grid (cumulative period), kWh
  toBat        - charge into battery, kWh
  fromBat      - discharge from battery, kWh
  records[]    - daily records inside /day or /month responses
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
HOURLY_HISTORY_DAYS = None
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
    for k in keys:
        v = d.get(k) if isinstance(d, dict) else None
        if v is not None:
            try: return float(v)
            except (TypeError, ValueError): continue
    return default


def _bucket(d: dict) -> dict:
    """Map Sunsynk's varied field names to our standard bucket."""
    return {
        "pv":          _f(d, "etoday", "emonth", "eyear", "etotal", "pv",
                            "generationValue", "energy", "generation"),
        "consumption": _f(d, "load", "loadEnergy", "useEnergy", "totalLoad",
                            "consumption"),
        "import":      _f(d, "fromGrid", "gridImport", "buy", "buyValue"),
        "export":      _f(d, "toGrid", "gridExport", "sell", "sellValue"),
        "charge":      _f(d, "toBat", "charge", "chargeValue", "battCharge"),
        "discharge":   _f(d, "fromBat", "discharge", "dischargeValue",
                            "battDischarge"),
    }


def _records_to_series(records: list[dict], metric_keys: tuple[str, ...]) -> list[dict]:
    """Pull a metric series from /day or /month records. Each record has a
    'time' field (HH:MM or YYYY-MM-DD)."""
    out = []
    for r in records or []:
        t = r.get("time") or r.get("date") or r.get("hour") or r.get("collectTime")
        if not t: continue
        v = None
        for k in metric_keys:
            if k in r and r[k] is not None:
                try: v = float(r[k]); break
                except (TypeError, ValueError): continue
        if v is None:
            continue
        out.append({"time": str(t), "value": v})
    return out


def _normalise_today_hourly(today_resp: dict) -> dict:
    """Today's /day response usually returns records[] with hourly entries.
    Their time field is often 'HH:00'; we expand to full SAST timestamps."""
    today_str = datetime.now(tz=SAST).strftime("%Y-%m-%d")
    records = today_resp.get("records") or today_resp.get("infos") or []
    out = {k: [] for k in ("pv", "consumption", "import", "export",
                             "charge", "discharge")}
    for r in records:
        t = r.get("time") or r.get("hour")
        if t is None: continue
        # 'HH:MM' or integer hour -> 'YYYY-MM-DD HH:00:00'
        if isinstance(t, int):
            time_str = f"{today_str} {t:02d}:00:00"
        elif ":" in str(t):
            hr = str(t).split(":")[0].zfill(2)
            time_str = f"{today_str} {hr}:00:00"
        else:
            time_str = f"{today_str} {str(t)[:2]}:00:00"
        out["pv"].append({"time": time_str,
                          "value": _f(r, "pv", "generation", "etoday",
                                       "energy", "value")})
        out["consumption"].append({"time": time_str,
                                    "value": _f(r, "load", "consumption")})
        out["import"].append({"time": time_str,
                              "value": _f(r, "fromGrid", "buy")})
        out["export"].append({"time": time_str,
                              "value": _f(r, "toGrid", "sell")})
        out["charge"].append({"time": time_str,
                              "value": _f(r, "toBat", "charge")})
        out["discharge"].append({"time": time_str,
                                  "value": _f(r, "fromBat", "discharge")})
    # Strip empty series
    return {k: v for k, v in out.items() if any(e["value"] != 0 for e in v) or v}


def build_data_json(config: dict, realtime: dict, today: dict,
                     month: dict, year: dict, total: dict) -> dict:

    current_pv_kw = _f(realtime, "pac") / 1000 if realtime.get("pac") else 0
    current_pv_kw = round(current_pv_kw, 3)
    today_pv = _f(today, "etoday") or _bucket(today)["pv"]

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

    hourly_series = _normalise_today_hourly(today)

    data = {
        "site_id":   config["site_id"],
        "platform":  "sunsynk",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":    current_pv_kw,
            "today_kwh":   today_pv,
            "status":      status,
            "status_severity": severity,
            "status_reason":   reason,
        },
        "energy": {
            "today":    _bucket(today)    or _bucket(realtime),
            "month":    _bucket(month),
            "year":     _bucket(year),
            "lifetime": _bucket(total),
            "hourly":   hourly_series,
        },
        "energy_powerflow_hourly": _build_powerflow_hourly(hourly_series),
        "irradiation": None,
        "irradiation_forecast": None,
        "financial": None,
    }
    tariff = config.get("tariff")
    if tariff:
        data["financial"] = compute_financials(data, tariff)
    return data


def _build_powerflow_hourly(hourly: dict) -> list[dict]:
    by_time: dict[str, dict] = {}
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
    for r in (month.get("records") or month.get("infos") or []):
        d = r.get("time") or r.get("date")
        if not d or len(str(d)) < 10: continue
        d = str(d)[:10]
        entry = by_date.get(d, {"date": d})
        entry["pv_kwh"]          = _f(r, "pv", "generation", "etoday", "energy")
        entry["consumption_kwh"] = _f(r, "load", "consumption")
        entry["import_kwh"]      = _f(r, "fromGrid", "buy")
        entry["export_kwh"]      = _f(r, "toGrid", "sell")
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
                year: dict, total: dict) -> None:
    data = build_data_json(config, realtime, today, month, year, total)
    hourly_path = site_dir / "hourly_history.json"
    hourly_history = merge_hourly_history(config, _read_json(hourly_path), data)
    _write_json(hourly_path, hourly_history)
    data["performance"] = build_performance(data, config, hourly_history)
    history_path = site_dir / "history.json"
    history = merge_history(config, _read_json(history_path), month)
    _write_json(history_path, history)
    _write_json(site_dir / "data.json", data)
