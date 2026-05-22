"""
Shared weather (Open-Meteo) module
==================================
Pulls Global Horizontal Irradiance (GHI) for every site in the repo, writes
it into each site's data.json (today's hourly + 24h forecast) and merges
into hourly_history.json (long-term irradiation history).

How it works:
  1. Globs every sites/<id>/config.json across all platforms
  2. Groups sites by (lat, lon) rounded to 3dp - sites at the same coords
     (e.g. the 5+ Gqeberha North End sites) share one API hit
  3. Calls Open-Meteo's free forecast endpoint with past_days=30 to get
     long history + today + next-day forecast in one shot
  4. Writes the result back per site

Open-Meteo specifics:
  - Field name: `shortwave_radiation` is W/m² GHI at the surface
  - Endpoint: https://api.open-meteo.com/v1/forecast
  - No API key needed, no rate limit at our volume
  - Timezone param returns timestamps already in SAST when set to
    Africa/Johannesburg, so no UTC->SAST conversion needed

Run on its own:
    python weather/refresh_irradiation.py

Bolted into fetch.py: imported as a module and `refresh_all()` called at the
end of run_fetch().
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
SAST = timezone(timedelta(hours=2))

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT = 30          # seconds
PAST_DAYS = 30                # history pulled in one call
FORECAST_DAYS = 2             # today + tomorrow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _round_coords(lat: float, lon: float) -> tuple[float, float]:
    """Round to 3dp - ~110m precision, enough for the GHI grid.

    Sites within 100m of each other share an API hit. Useful for Genergy's
    Eastern Cape cluster - Embassy, GM-Hasty Tasty, BMI Paterson all sit
    within 200m in North End."""
    return (round(lat, 3), round(lon, 3))


def _today_sast() -> str:
    return datetime.now(tz=SAST).strftime("%Y-%m-%d")


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


# ---------------------------------------------------------------------------
# Open-Meteo API client
# ---------------------------------------------------------------------------

class OpenMeteoError(RuntimeError):
    pass


def fetch_ghi(lat: float, lon: float) -> dict:
    """Pull GHI hourly history + forecast for one coordinate.

    Returns a dict shaped like:
        {
          "fetched_at": "2026-05-21T14:30:00+02:00",
          "hourly": [
            {"time": "2026-04-21 00:00:00", "ghi_w_m2": 0.0},
            ...
          ]
        }
    Times are SAST 'YYYY-MM-DD HH:MM:SS' to match our schema convention.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "shortwave_radiation",
        "past_days": PAST_DAYS,
        "forecast_days": FORECAST_DAYS,
        "timezone": "Africa/Johannesburg",
    }
    url = f"{OPEN_METEO_URL}?{urlencode(params)}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    body = r.json()

    h = body.get("hourly", {})
    times = h.get("time", [])
    vals = h.get("shortwave_radiation", [])
    if len(times) != len(vals):
        raise OpenMeteoError(f"time/value length mismatch: {len(times)} vs {len(vals)}")

    hourly = []
    for t, v in zip(times, vals):
        # Open-Meteo returns 'YYYY-MM-DDTHH:MM' - convert to our schema format
        ts = t.replace("T", " ") + ":00"
        hourly.append({"time": ts, "ghi_w_m2": float(v) if v is not None else 0.0})

    return {
        "fetched_at": datetime.now(tz=SAST).strftime("%Y-%m-%dT%H:%M:%S+02:00"),
        "hourly": hourly,
    }


# ---------------------------------------------------------------------------
# Per-site write
# ---------------------------------------------------------------------------

def _split_hourly_by_day(hourly: list[dict]) -> dict[str, list[dict]]:
    """Group hourly entries by 'YYYY-MM-DD'."""
    by_date: dict[str, list[dict]] = {}
    for h in hourly:
        d = h["time"].split(" ")[0]
        by_date.setdefault(d, []).append(h)
    return by_date


def _hourly_total_kwh(entries: list[dict]) -> float:
    """Sum hourly W/m^2 -> kWh/m^2 for a day (divide by 1000)."""
    return round(sum(e["ghi_w_m2"] for e in entries) / 1000, 3)


def _today_block(by_date: dict[str, list[dict]], today: str) -> dict:
    """Today's hourly array + total, in the data.json schema shape."""
    entries = by_date.get(today, [])
    return {
        "source": "open-meteo",
        "today_total": _hourly_total_kwh(entries),
        "today_hourly": [{"time": e["time"], "value": e["ghi_w_m2"]}
                          for e in entries],
    }


def _forecast_block(by_date: dict[str, list[dict]], today: str) -> dict:
    """Tomorrow's forecast block - same shape as today, separately keyed."""
    d = datetime.strptime(today, "%Y-%m-%d") + timedelta(days=1)
    tomorrow = d.strftime("%Y-%m-%d")
    entries = by_date.get(tomorrow, [])
    return {
        "date": tomorrow,
        "total_forecast": _hourly_total_kwh(entries),
        "hourly": [{"time": e["time"], "value": e["ghi_w_m2"]}
                    for e in entries],
    }


def _merge_into_hourly_history(site_dir: Path, hourly: list[dict]) -> None:
    """Add the irradiation series to each hour entry in hourly_history.json.

    Each entry in hourly_history.json already has metered + powerflow fields
    keyed by time; we add `ghi_w_m2` alongside, so the dashboard can render
    irradiation against PV without a second fetch.
    """
    path = site_dir / "hourly_history.json"
    existing = _read_json(path)
    if not existing or "hours" not in existing:
        # No hourly history yet (first run or non-FusionSolar site without
        # generation data) - write a weather-only file
        existing = {"site_id": site_dir.name, "hours": []}

    # Build a {time: entry} index for fast merge
    by_time = {h.get("time"): h for h in existing["hours"] if h.get("time")}
    for h in hourly:
        t = h["time"]
        if t in by_time:
            by_time[t]["ghi_w_m2"] = h["ghi_w_m2"]
        else:
            by_time[t] = {"time": t, "ghi_w_m2": h["ghi_w_m2"]}

    existing["hours"] = sorted(by_time.values(), key=lambda h: h["time"])
    _write_json(path, existing)


def write_site_weather(site_dir: Path, ghi_data: dict) -> None:
    """Write the fetched GHI series into a site's data.json + hourly_history.json."""
    today = _today_sast()
    by_date = _split_hourly_by_day(ghi_data["hourly"])

    # 1. Update data.json - irradiation block + new forecast block
    data_path = site_dir / "data.json"
    data = _read_json(data_path)
    if data is None:
        # No generation data yet for this site - write a weather-only data.json
        data = {"site_id": site_dir.name, "updated_at": ghi_data["fetched_at"]}

    # Only overwrite irradiation with Open-Meteo when there is NO native
    # sensor reading already. A site with a physical EMI sensor (FusionSolar
    # radiation_intensity) keeps its real data - Open-Meteo is the fallback.
    existing_irr = data.get("irradiation") or {}
    if existing_irr.get("source") != "fusionsolar":
        data["irradiation"] = _today_block(by_date, today)
    data["irradiation_forecast"] = _forecast_block(by_date, today)

    _write_json(data_path, data)

    # 2. Merge into hourly_history.json (full 30-day series)
    _merge_into_hourly_history(site_dir, ghi_data["hourly"])


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def _load_sites_with_coords() -> list[tuple[Path, dict]]:
    """Return [(site_dir, config)] for every site that has lat+lon set."""
    out = []
    for config_path in sorted(REPO_ROOT.glob("platforms/*/sites/*/config.json")):
        # Skip example/template folders (prefixed with underscore)
        if config_path.parent.name.startswith("_"):
            continue
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"  SKIP {config_path.parent.name}: bad JSON")
            continue
        loc = cfg.get("location") or {}
        lat, lon = loc.get("lat"), loc.get("lon")
        if lat is None or lon is None:
            print(f"  SKIP {cfg.get('site_id')}: no lat/lon")
            continue
        out.append((config_path.parent, cfg))
    return out


def refresh_all() -> tuple[int, int]:
    """Fetch GHI for every site and write the per-site files.

    Returns (sites_updated, api_calls). Sites at the same coords (within
    100m) share one API call - on a fleet of 25 sites clustered in 5-6
    metros, this typically means ~6 API calls total.
    """
    sites = _load_sites_with_coords()
    if not sites:
        print("No sites with coordinates to refresh.")
        return (0, 0)

    print(f"Refreshing irradiation for {len(sites)} site(s)...")

    # Group by rounded coords - one API hit per cluster
    by_coords: dict[tuple[float, float], list[tuple[Path, dict]]] = {}
    for site_dir, cfg in sites:
        key = _round_coords(cfg["location"]["lat"], cfg["location"]["lon"])
        by_coords.setdefault(key, []).append((site_dir, cfg))

    print(f"  -> {len(by_coords)} unique coordinate group(s)")

    updated, calls = 0, 0
    for (lat, lon), members in by_coords.items():
        try:
            ghi = fetch_ghi(lat, lon)
            calls += 1
        except Exception as e:
            print(f"  FAIL at ({lat}, {lon}): {e}")
            continue
        for site_dir, cfg in members:
            try:
                write_site_weather(site_dir, ghi)
                print(f"  OK   {cfg['site_id']}")
                updated += 1
            except Exception as e:
                print(f"  FAIL {cfg['site_id']}: {e}")

    print(f"\nDone. {updated} site(s) updated, {calls} API call(s) made.")
    return (updated, calls)


def main() -> int:
    try:
        refresh_all()
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
