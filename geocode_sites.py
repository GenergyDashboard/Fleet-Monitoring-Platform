"""
Geocode missing coordinates in site configs
===========================================
For every config.json that has an address but no lat/lon, look up the
address via Open-Meteo's free geocoding API and fill the lat/lon in.

Usage:
    python geocode_sites.py              # all platforms, only sites missing coords
    python geocode_sites.py --platform sunsynk
    python geocode_sites.py --dry-run    # show what would change, don't write

Open-Meteo Geocoding is free, no API key, ~10 req/sec polite limit.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent
PLATFORMS = REPO / "platforms"

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Conservative bounding box for "South Africa-ish" results. If a vague
# address like "Eastern Cape" geocodes to a location outside this box,
# we'd rather fall back to no-coords than place a marker in another country.
SA_BBOX = {"lat_min": -35.0, "lat_max": -22.0,
            "lon_min": 16.0,  "lon_max": 33.0}


def _clean_address(address: str) -> str:
    """Clean up vague addresses that confuse Open-Meteo.

    'Eastern Cape Port Elizabeth' -> 'Port Elizabeth, South Africa'
    'Eastern Cape' alone -> '' (too vague to geocode usefully)
    Names with plus codes like 'HPCW+XC Bathurst' -> 'Bathurst, South Africa'
    """
    if not address:
        return ""
    addr = address.strip()
    lower = addr.lower()

    # Drop the province prefix when a city follows
    for city in ("port elizabeth", "gqeberha", "walmer", "summerstrand",
                  "humewood", "lorraine", "framesby", "kabeljouws",
                  "jeffreys bay", "j-bay", "st francis bay", "saint francis bay",
                  "plettenberg bay", "bathurst", "cape st francis", "cannon rocks",
                  "south end", "sunridge park", "fernglen", "modimolle",
                  "malalane", "mill park"):
        if city in lower and "eastern cape" in lower:
            return f"{city.title()}, South Africa"

    # 'Eastern Cape' on its own is too vague
    if lower in ("eastern cape", "eastern cape "):
        return ""

    # Plus codes like 'HPCW+XC Bathurst, South Africa' or 'RRM8+MP Saint Francis Bay'
    # - drop the plus code, keep the place name
    if "+" in addr.split(" ")[0] and len(addr.split(" ")) > 1:
        return " ".join(addr.split(" ")[1:])

    return addr


def geocode(address: str) -> tuple[float, float, str] | None:
    """Return (lat, lon, town) for the address, or None if no good match."""
    cleaned = _clean_address(address)
    if not cleaned:
        return None
    # Open-Meteo's search returns top matches; we take the first that's
    # inside the SA bbox.
    params = {"name": cleaned, "count": 5, "language": "en", "format": "json"}
    try:
        r = requests.get(GEOCODE_URL, params=params, timeout=15)
        r.raise_for_status()
    except requests.RequestException as exc:
        print(f"    geocode network error: {exc}")
        return None
    body = r.json()
    results = body.get("results") or []
    for result in results:
        lat = result.get("latitude")
        lon = result.get("longitude")
        if lat is None or lon is None:
            continue
        if not (SA_BBOX["lat_min"] <= lat <= SA_BBOX["lat_max"]
                 and SA_BBOX["lon_min"] <= lon <= SA_BBOX["lon_max"]):
            continue
        town = (result.get("admin2") or result.get("admin3")
                or result.get("name") or "")
        return float(lat), float(lon), town
    return None


def process_config(cfg_path: Path, dry_run: bool) -> bool:
    """Returns True if the config was updated."""
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    loc = cfg.get("location") or {}
    if loc.get("lat") is not None and loc.get("lon") is not None:
        return False     # already has coords
    addr = loc.get("address")
    if not addr:
        return False

    print(f"  {cfg['site_id']:<35} addr='{addr[:50]}'")
    result = geocode(addr)
    if not result:
        print(f"    -> no usable match")
        return False
    lat, lon, town = result
    print(f"    -> {lat:.5f}, {lon:.5f}  ({town})")
    if dry_run:
        return True
    loc["lat"] = lat
    loc["lon"] = lon
    if not loc.get("town"):
        loc["town"] = town
    cfg["location"] = loc
    cfg_path.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", help="limit to one platform (e.g. sunsynk)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    pattern = (f"{args.platform}/sites/*/config.json"
                if args.platform else "*/sites/*/config.json")
    configs = sorted(PLATFORMS.glob(pattern))
    configs = [c for c in configs if not c.parent.name.startswith("_")]
    if not configs:
        print(f"No configs found matching {pattern}")
        return 0

    print(f"Processing {len(configs)} config(s)...")
    updated = 0
    for cfg_path in configs:
        if process_config(cfg_path, args.dry_run):
            updated += 1
        time.sleep(0.15)        # polite rate limiting

    verb = "Would update" if args.dry_run else "Updated"
    print(f"\n{verb} {updated} of {len(configs)} configs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
