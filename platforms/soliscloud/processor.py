"""
SolisCloud response -> shared schema
====================================
Maps SolisCloud API responses into our standard data.json /
history.json / hourly_history.json shape.

SolisCloud field mappings (from /stationDetail, /stationDayEnergyList,
/stationMonthEnergyList, /stationYearEnergyList):

  dayEnergy        - today's PV kWh
  monthEnergy      - this month PV kWh
  yearEnergy       - this year PV kWh
  allEnergy        - lifetime PV kWh
  dayConsumeEnergy - today's consumption kWh
  dayBuyEnergy     - today's grid import kWh
  daySellEnergy    - today's grid export kWh
  dayChargeEnergy  - battery charged today
  dayDisChargeEnergy - battery discharged today
  power            - current PV power (kW)
  capacity         - installed kWp
  state            - 1=online, 2=offline, 3=alarm
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


def _bucket_period(d: dict) -> dict:
    """Period bucket from a stationDetail or daily-summary response."""
    return {
        "pv":          _f(d, "dayEnergy", "monthEnergy", "yearEnergy", "allEnergy",
                            "energy", "pv"),
        "consumption": _f(d, "dayConsumeEnergy", "monthConsumeEnergy",
                            "yearConsumeEnergy", "consumption"),
        "import":      _f(d, "dayBuyEnergy", "monthBuyEnergy", "yearBuyEnergy",
                            "buy", "import"),
        "export":      _f(d, "daySellEnergy", "monthSellEnergy", "yearSellEnergy",
                            "sell", "export"),
        "charge":      _f(d, "dayChargeEnergy", "monthChargeEnergy",
                            "yearChargeEnergy", "charge"),
        "discharge":   _f(d, "dayDisChargeEnergy", "monthDisChargeEnergy",
                            "yearDisChargeEnergy", "discharge"),
    }


def _hourly_from_day(today: dict) -> dict:
    """SolisCloud's /stationDayEnergyList returns hourly granularity in 'data'."""
    items = today.get("data") or today.get("records") or []
    # Each item typically has 'time' as 'YYYY-MM-DD HH:MM:SS' and energy fields
    today_str = datetime.now(tz=SAST).strftime("%Y-%m-%d")
    out = {k: [] for k in ("pv", "consumption", "import", "export",
                             "charge", "discharge")}
    for r in items:
        t = r.get("time") or r.get("dataTimestamp") or r.get("collectTime")
        if not t: continue
        time_str = str(t) if " " in str(t) else f"{today_str} {str(t)[:5]}:00"
        out["pv"].append({"time": time_str,
                          "value": _f(r, "energy", "pv", "dayEnergy")})
        out["consumption"].append({"time": time_str,
                                    "value": _f(r, "consumeEnergy", "consumption")})
        out["import"].append({"time": time_str,
                              "value": _f(r, "buyEnergy", "buy")})
        out["export"].append({"time": time_str,
                              "value": _f(r, "sellEnergy", "sell")})
        out["charge"].append({"time": time_str,
                              "value": _f(r, "chargeEnergy", "charge")})
        out["discharge"].append({"time": time_str,
                                  "value": _f(r, "disChargeEnergy", "discharge")})
    return out


def build_data_json(config: dict, detail: dict, today: dict,
                     month: dict, year: dict) -> dict:

    current_pv_kw = _f(detail, "power", "currentPower", "pac") / (
        1000 if _f(detail, "power") > 100 else 1)
    current_pv_kw = round(current_pv_kw, 3)
    today_pv = _f(detail, "dayEnergy")

    state = detail.get("state")
    now_hr = datetime.now(tz=SAST).hour
    is_daylight = 6 <= now_hr <= 18
    if state == 2:
        status = "offline"; severity = "warning"; reason = "Station offline per SolisCloud"
    elif state == 3:
        status = "fault"; severity = "error"; reason = "Alarm condition per SolisCloud"
    elif is_daylight and current_pv_kw < 0.1 and today_pv < 0.5:
        status = "underperforming"; severity = "warning"
        reason = "Daylight but no PV generation today"
    elif current_pv_kw > 0.1:
        status = "online"; severity = "ok"; reason = "Generating normally"
    elif not is_daylight:
        status = "offline"; severity = "info"; reason = "Outside daylight hours"
    else:
        status = "unknown"; severity = "info"; reason = "Insufficient data"

    hourly = _hourly_from_day(today)
    data = {
        "site_id":   config["site_id"],
        "platform":  "soliscloud",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":    current_pv_kw,
            "today_kwh":   today_pv,
            "status":      status,
            "status_severity": severity,
            "status_reason":   reason,
        },
        "energy": {
            "today":    _bucket_period(detail),
            "month":    _bucket_period(month) or _bucket_period(detail),
            "year":     _bucket_period(year),
            "lifetime": {"pv": _f(detail, "allEnergy"),
                          "consumption": _f(detail, "totalConsumeEnergy"),
                          "import": 0, "export": 0, "charge": 0, "discharge": 0},
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
    items = month.get("data") or month.get("records") or []
    for r in items:
        d = r.get("time") or r.get("date")
        if not d or len(str(d)) < 10: continue
        d = str(d)[:10]
        entry = by_date.get(d, {"date": d})
        entry["pv_kwh"]          = _f(r, "energy", "pv", "dayEnergy")
        entry["consumption_kwh"] = _f(r, "consumeEnergy", "consumption")
        entry["import_kwh"]      = _f(r, "buyEnergy", "buy")
        entry["export_kwh"]      = _f(r, "sellEnergy", "sell")
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
                detail: dict, today: dict, month: dict, year: dict) -> None:
    data = build_data_json(config, detail, today, month, year)
    hourly_path = site_dir / "hourly_history.json"
    hourly_history = merge_hourly_history(config, _read_json(hourly_path), data)
    _write_json(hourly_path, hourly_history)
    data["performance"] = build_performance(data, config, hourly_history)
    history_path = site_dir / "history.json"
    _write_json(history_path, merge_history(config, _read_json(history_path), month))
    _write_json(site_dir / "data.json", data)
