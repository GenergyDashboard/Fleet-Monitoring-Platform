"""
SolarmanPV response -> shared schema processor
==============================================
Maps SolarmanPV API responses into the standard data.json + history.json +
hourly_history.json schema every other platform produces, so dashboards
consume SolarmanPV sites identically.

SolarmanPV field mapping (stationDataItems / real-time):
  generationPower   - current PV power, W
  generationValue   - cumulative kWh for the period
  usePower          - current load, W
  useValue          - cumulative load kWh
  gridPower         - current grid power, W (signed: + = export, - = import)
  buyValue          - cumulative grid IMPORT kWh
  sellValue         - cumulative grid EXPORT kWh
  chargePower       - current battery charging power, W (if battery present)
  chargeValue       - cumulative kWh charged into battery
  dischargePower    - current battery discharging power, W
  dischargeValue    - cumulative kWh discharged from battery
  batterySoc        - %
  lastUpdateTime    - epoch seconds of last sample
  status            - 1 = online, 2 = alarm, 3 = offline, 0 = unknown
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make shared/ importable
_SHARED_DIR = Path(__file__).resolve().parent.parent.parent / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

import powerflow                                  # noqa: E402
from financial import compute_financials          # noqa: E402
from performance import build_performance         # noqa: E402

SAST = timezone(timedelta(hours=2))
HOURLY_HISTORY_DAYS = None
HISTORY_DAYS = 400


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")


def _sast_now_iso() -> str:
    return datetime.now(tz=SAST).strftime("%Y-%m-%dT%H:%M:%S+02:00")


def _ts_to_sast_str(ts: int) -> str:
    """Epoch seconds -> 'YYYY-MM-DD HH:MM:SS' SAST."""
    return datetime.fromtimestamp(ts, tz=SAST).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Field extractors
# ---------------------------------------------------------------------------

def _f(item: dict, *keys, default: float = 0.0) -> float:
    """Try several field names, return the first non-None as float."""
    for k in keys:
        v = item.get(k)
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def _bucket_from_period_items(items: list[dict]) -> dict:
    """Sum each metric across a list of period items.

    Used for daily/monthly/yearly windows where each item is one
    interval (e.g. one day's totals) and the period total is the sum.
    """
    return {
        "pv":          sum(_f(i, "generationValue") for i in items),
        "consumption": sum(_f(i, "useValue") for i in items),
        "import":      sum(_f(i, "buyValue", "gridImportValue") for i in items),
        "export":      sum(_f(i, "sellValue", "gridExportValue") for i in items),
        "charge":      sum(_f(i, "chargeValue") for i in items),
        "discharge":   sum(_f(i, "dischargeValue") for i in items),
    }


def _bucket_from_realtime(rt: dict) -> dict:
    """Today's totals from real-time response. Values are cumulative for the day."""
    return {
        "pv":          _f(rt, "generationValue"),
        "consumption": _f(rt, "useValue"),
        "import":      _f(rt, "buyValue", "gridImportValue"),
        "export":      _f(rt, "sellValue", "gridExportValue"),
        "charge":      _f(rt, "chargeValue"),
        "discharge":   _f(rt, "dischargeValue"),
    }


# ---------------------------------------------------------------------------
# Aggregate 5-min raw samples to hourly buckets
# ---------------------------------------------------------------------------

def _aggregate_5min_to_hourly(items: list[dict]) -> dict:
    """SolarmanPV's 5-min interval points are CUMULATIVE day totals at that
    moment. To get per-hour energy we take the value at the end of each hour
    and subtract the value at the start.

    Returns a dict keyed by metric name, value = list[{time, value}].
    """
    metrics = {
        "pv":          ["generationValue"],
        "consumption": ["useValue"],
        "import":      ["buyValue", "gridImportValue"],
        "export":      ["sellValue", "gridExportValue"],
        "charge":      ["chargeValue"],
        "discharge":   ["dischargeValue"],
    }

    # Sort items by timestamp
    points = []
    for item in items:
        ts = item.get("dateTime") or item.get("timestamp") or item.get("collectTime")
        if ts is None:
            continue
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        # Solarman epochs are seconds; some payloads use ms - normalise.
        if ts > 10_000_000_000:        # > year ~2286 in seconds => must be ms
            ts //= 1000
        points.append((ts, item))
    points.sort(key=lambda p: p[0])

    if not points:
        return {k: [] for k in metrics}

    # For each hour-of-day, find the last sample in that hour and use its
    # cumulative value. Then take diffs hour-to-hour.
    by_hour: dict[int, dict] = {}      # hour-of-day (0-23) -> last cumulative sample
    for ts, item in points:
        dt = datetime.fromtimestamp(ts, tz=SAST)
        hr = dt.hour
        # Keep the latest sample within each hour
        prev = by_hour.get(hr)
        if not prev or ts > prev["_ts"]:
            entry = {"_ts": ts, "_time": dt.strftime("%Y-%m-%d %H:00:00")}
            for metric, keys in metrics.items():
                entry[metric] = _f(item, *keys)
            by_hour[hr] = entry

    # Now compute hourly DELTAS from cumulative values.
    # Cumulative values reset at midnight, so we start at 0 at hour-0.
    out: dict[str, list[dict]] = {m: [] for m in metrics}
    prev_cum = {m: 0.0 for m in metrics}
    for hr in range(24):
        if hr not in by_hour:
            continue
        entry = by_hour[hr]
        time_str = entry["_time"]
        for metric in metrics:
            cur = entry[metric]
            delta = max(0.0, cur - prev_cum[metric])
            out[metric].append({"time": time_str, "value": round(delta, 3)})
            prev_cum[metric] = cur
    return out


# ---------------------------------------------------------------------------
# Build data.json (without performance - added by write_site)
# ---------------------------------------------------------------------------

def build_data_json(config: dict, realtime: dict,
                     hourly_raw: list[dict],
                     daily: list[dict],
                     monthly: list[dict],
                     yearly: list[dict]) -> dict:

    # Current state ---------------------------------------------------------
    pv_w = _f(realtime, "generationPower")
    current_pv_kw = round(pv_w / 1000, 3)

    # Status: SolarmanPV uses 1=online, 2=alarm, 3=offline, 0=unknown
    sm_status = realtime.get("status") or realtime.get("stationStatus")
    now_hr = datetime.now(tz=SAST).hour
    is_daylight = 6 <= now_hr <= 18
    today_pv = _f(realtime, "generationValue")
    if sm_status == 3:
        status = "offline"
        status_severity = "warning"
        status_reason = "SolarmanPV reports station offline"
    elif sm_status == 2:
        status = "fault"
        status_severity = "error"
        status_reason = "SolarmanPV reports an alarm condition"
    elif is_daylight and current_pv_kw < 0.1 and today_pv < 0.5:
        status = "underperforming"
        status_severity = "warning"
        status_reason = "Daylight hours but no PV generation today"
    elif current_pv_kw > 0.1:
        status = "online"
        status_severity = "ok"
        status_reason = "Generating normally"
    elif not is_daylight:
        status = "offline"
        status_severity = "info"
        status_reason = "Outside daylight hours"
    else:
        status = "unknown"
        status_severity = "info"
        status_reason = "No clear status from API"

    # Energy buckets --------------------------------------------------------
    today_b   = _bucket_from_realtime(realtime)
    month_b   = _bucket_from_period_items(daily)
    year_b    = _bucket_from_period_items(monthly)
    life_b    = _bucket_from_period_items(yearly)

    # Hourly series from 5-min raw ------------------------------------------
    hourly_series = _aggregate_5min_to_hourly(hourly_raw)

    data = {
        "site_id":   config["site_id"],
        "platform":  "solarmanpv",
        "updated_at": _sast_now_iso(),
        "current": {
            "power_kw":    current_pv_kw,
            "today_kwh":   today_b["pv"],
            "status":      status,
            "status_severity": status_severity,
            "status_reason":   status_reason,
        },
        "energy": {
            "today":    today_b,
            "month":    month_b,
            "year":     year_b,
            "lifetime": life_b,
            "hourly": hourly_series,
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


def _build_powerflow_hourly(hourly_series: dict) -> list[dict]:
    """For each hour with metered values, run the V5 powerflow split."""
    by_time: dict[str, dict] = {}
    for metric in ("pv", "consumption", "import", "export", "charge", "discharge"):
        for entry in hourly_series.get(metric, []):
            by_time.setdefault(entry["time"], {})[metric] = entry["value"]
    out = []
    for t in sorted(by_time):
        m = by_time[t]
        flows = powerflow.split(
            pv=m.get("pv", 0),
            grid_import=m.get("import", 0),
            export=m.get("export", 0),
            charge=m.get("charge", 0),
            discharge=m.get("discharge", 0),
            load=m.get("consumption", 0),
        )
        out.append({"time": t, **flows})
    return out


# ---------------------------------------------------------------------------
# History merging - mirrors FusionSolar processor
# ---------------------------------------------------------------------------

def merge_history(config: dict, existing: dict | None,
                   daily_items: list[dict]) -> dict:
    """Rolling daily totals across HISTORY_DAYS days."""
    days = (existing or {}).get("days", [])
    by_date = {d.get("date"): d for d in days if d.get("date")}

    for item in daily_items:
        ts = item.get("dateTime") or item.get("timestamp") or item.get("collectTime")
        if ts is None:
            continue
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if ts > 10_000_000_000:
            ts //= 1000
        d = datetime.fromtimestamp(ts, tz=SAST).strftime("%Y-%m-%d")
        entry = by_date.get(d, {"date": d})
        entry["pv_kwh"]          = _f(item, "generationValue")
        entry["consumption_kwh"] = _f(item, "useValue")
        entry["import_kwh"]      = _f(item, "buyValue", "gridImportValue")
        entry["export_kwh"]      = _f(item, "sellValue", "gridExportValue")
        by_date[d] = entry

    merged = sorted(by_date.values(), key=lambda x: x.get("date", ""))
    if HISTORY_DAYS:
        merged = merged[-HISTORY_DAYS:]
    return {"site_id": config["site_id"], "days": merged}


def merge_hourly_history(config: dict, existing: dict | None,
                           data: dict) -> dict:
    """Per-hour series with metered + powerflow + irradiation fields."""
    hours = (existing or {}).get("hours", [])
    by_time = {h.get("time"): h for h in hours if h.get("time")}

    hourly = data["energy"]["hourly"]
    def upsert(entries, key):
        for e in entries:
            row = by_time.get(e["time"]) or {"time": e["time"]}
            row[key] = e["value"]
            by_time[e["time"]] = row
    for metric in ("pv", "consumption", "import", "export", "charge", "discharge"):
        upsert(hourly.get(metric, []), metric)

    for flows in data.get("energy_powerflow_hourly", []):
        row = by_time.get(flows["time"]) or {"time": flows["time"]}
        for k, v in flows.items():
            if k != "time":
                row[k] = v
        by_time[flows["time"]] = row

    merged = sorted(by_time.values(), key=lambda x: x.get("time", ""))
    if HOURLY_HISTORY_DAYS:
        cutoff = (datetime.now(tz=SAST)
                   - timedelta(days=HOURLY_HISTORY_DAYS)).strftime("%Y-%m-%d")
        merged = [h for h in merged if h.get("time", "")[:10] >= cutoff]
    return {"site_id": config["site_id"], "hours": merged}


# ---------------------------------------------------------------------------
# Top-level writer
# ---------------------------------------------------------------------------

def write_site(site_dir: Path, config: dict, *,
                realtime: dict, hourly_raw: list[dict],
                daily: list[dict], monthly: list[dict],
                yearly: list[dict]) -> None:
    """Mirror of the FusionSolar processor's write order so empirical
    performance has hourly_history to read from."""
    data = build_data_json(config, realtime, hourly_raw, daily, monthly, yearly)

    hourly_path = site_dir / "hourly_history.json"
    existing_hourly = _read_json(hourly_path)
    hourly_history = merge_hourly_history(config, existing_hourly, data)
    _write_json(hourly_path, hourly_history)

    data["performance"] = build_performance(data, config, hourly_history)

    history_path = site_dir / "history.json"
    existing_history = _read_json(history_path)
    history = merge_history(config, existing_history, daily)
    _write_json(history_path, history)

    _write_json(site_dir / "data.json", data)
