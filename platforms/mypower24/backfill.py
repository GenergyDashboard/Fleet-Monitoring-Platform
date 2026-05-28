"""
MyPower24 lifetime backfill
===========================
One-time (or occasional) script that walks BACKWARDS through time pulling
historical energy data for a logger, filling history.json and
hourly_history.json with everything the API has.

It stops when it hits a month that returns no data (i.e. before the logger
was commissioned), and records that boundary as the commissioning date in
the site's config.json (used as degradation year 1 for predictions).

Usage:
    cd platforms/mypower24
    set MYPOWER24_USERNAME=...
    set MYPOWER24_PASSWORD=...
    python backfill.py                  # all sites, everything available
    python backfill.py --site beyond-buds-mypower24
    python backfill.py --months 24      # cap how far back to walk

This is separate from fetch.py (which only does 'today') so the slow,
many-call historical pull runs on demand, not every 5 minutes.
"""

from __future__ import annotations

import argparse
import calendar
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Reuse the client + processor from this platform
sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch as mp24_fetch                       # noqa: E402
import processor as mp24_processor                # noqa: E402

SAST = timezone(timedelta(hours=2))
PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"


def _month_range_utc(year: int, month: int) -> tuple[str, str]:
    """First and last instant of a calendar month, as UTC ISO strings."""
    last_day = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, tzinfo=SAST).astimezone(timezone.utc)
    end = datetime(year, month, last_day, 23, 59, tzinfo=SAST).astimezone(timezone.utc)
    return start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")


def _prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def backfill_site(client, site_dir: Path, config: dict,
                   max_months: int | None) -> None:
    sid = config["site_id"]
    serial = config["serial"]
    print(f"\n=== Backfilling {sid} ({serial}) ===")

    # Accumulators across all months
    all_daily: dict[str, dict] = {}
    all_hourly: dict[str, dict] = {}

    now = datetime.now(tz=SAST)
    year, month = now.year, now.month
    empty_streak = 0
    months_done = 0
    earliest_data_date: str | None = None

    while True:
        start_iso, end_iso = _month_range_utc(year, month)
        try:
            energy = client.energy_metrics(serial, start_iso, end_iso, "PT15M")
        except Exception as exc:
            print(f"  {year}-{month:02d}: error ({exc}); stopping")
            break

        nonzero = [r for r in energy if _has_any_value(r)]
        if not nonzero:
            empty_streak += 1
            print(f"  {year}-{month:02d}: no data (empty streak {empty_streak})")
            # Two consecutive empty months = we've gone past commissioning
            if empty_streak >= 2:
                print("  Two empty months in a row - reached start of data.")
                break
        else:
            empty_streak = 0
            _accumulate(energy, all_daily, all_hourly)
            # Track earliest date seen
            for r in energy:
                d = _interval_date(r)
                if d and (earliest_data_date is None or d < earliest_data_date):
                    earliest_data_date = d
            print(f"  {year}-{month:02d}: {len(nonzero)} intervals")

        months_done += 1
        if max_months and months_done >= max_months:
            print(f"  Reached --months cap ({max_months}).")
            break
        year, month = _prev_month(year, month)
        # Don't walk back before solar existed / sane floor
        if year < 2015:
            break
        time.sleep(0.4)        # polite

    # Write merged history files
    _write_history(site_dir, config, all_daily)
    _write_hourly(site_dir, config, all_hourly)

    # Record commissioning date in config (degradation year 1)
    if earliest_data_date:
        config["commissioning_date"] = earliest_data_date
        (site_dir / "config.json").write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        print(f"  Commissioning date (earliest data): {earliest_data_date}")
    print(f"  Wrote {len(all_daily)} days, {len(all_hourly)} hours.")


def _has_any_value(rec: dict) -> bool:
    for node in ("load", "grid", "pv"):
        n = rec.get(node, {})
        for direction in ("export", "import"):
            try:
                if n[direction]["active"]["value"]:
                    return True
            except (KeyError, TypeError):
                continue
    return False


def _interval_date(rec: dict) -> str | None:
    dt = mp24_processor._parse_interval_time(rec)
    return dt.strftime("%Y-%m-%d") if dt else None


def _accumulate(energy: list[dict], all_daily: dict, all_hourly: dict) -> None:
    """Fold one month of intervals into the running daily + hourly maps."""
    for rec in energy:
        dt = mp24_processor._parse_interval_time(rec)
        if dt is None:
            continue
        day = dt.strftime("%Y-%m-%d")
        hour = dt.strftime("%Y-%m-%d %H:00:00")

        cons = mp24_processor._active(rec.get("load", {}), "export")
        imp = mp24_processor._active(rec.get("grid", {}), "import")
        exp = mp24_processor._active(rec.get("grid", {}), "export")
        pv = mp24_processor._active(rec.get("pv", {}), "export")

        d = all_daily.setdefault(day, {"date": day, "pv_kwh": 0.0,
                                         "consumption_kwh": 0.0,
                                         "import_kwh": 0.0, "export_kwh": 0.0})
        d["pv_kwh"] += pv
        d["consumption_kwh"] += cons
        d["import_kwh"] += imp
        d["export_kwh"] += exp

        h = all_hourly.setdefault(hour, {"time": hour, "pv": 0.0,
                                          "consumption": 0.0, "import": 0.0,
                                          "export": 0.0, "charge": 0.0,
                                          "discharge": 0.0})
        h["pv"] += pv
        h["consumption"] += cons
        h["import"] += imp
        h["export"] += exp


def _write_history(site_dir: Path, config: dict, all_daily: dict) -> None:
    # Merge with any existing history (don't clobber today's live entry)
    path = site_dir / "history.json"
    existing = {}
    if path.exists():
        try:
            for d in json.loads(path.read_text()).get("days", []):
                existing[d.get("date")] = d
        except json.JSONDecodeError:
            pass
    for day, rec in all_daily.items():
        rec = {k: round(v, 3) if isinstance(v, float) else v
               for k, v in rec.items()}
        existing[day] = rec
    days = sorted(existing.values(), key=lambda x: x.get("date", ""))
    path.write_text(json.dumps({"site_id": config["site_id"], "days": days},
                                indent=2) + "\n", encoding="utf-8")


def _write_hourly(site_dir: Path, config: dict, all_hourly: dict) -> None:
    path = site_dir / "hourly_history.json"
    existing = {}
    if path.exists():
        try:
            for h in json.loads(path.read_text()).get("hours", []):
                existing[h.get("time")] = h
        except json.JSONDecodeError:
            pass
    for hour, rec in all_hourly.items():
        rec = {k: round(v, 3) if isinstance(v, float) else v
               for k, v in rec.items()}
        existing[hour] = rec
    hours = sorted(existing.values(), key=lambda x: x.get("time", ""))
    path.write_text(json.dumps({"site_id": config["site_id"], "hours": hours},
                                indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", help="limit to one site slug")
    parser.add_argument("--months", type=int, help="cap how far back to walk")
    args = parser.parse_args()

    client = mp24_fetch.MyPower24Client(mp24_fetch.USERNAME, mp24_fetch.PASSWORD)
    client.login()

    sites = mp24_fetch.load_site_configs()
    if args.site:
        sites = [(d, c) for d, c in sites if c["site_id"] == args.site]
    if not sites:
        print("No matching sites.")
        return 1

    for site_dir, config in sites:
        backfill_site(client, site_dir, config, args.months)

    print("\nBackfill complete. Commit the updated history files and push.")
    print("Then run build_sites_index.py if commissioning dates changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
