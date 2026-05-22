"""
Tariff populate
===============
Reads tariffs.csv (next to this script) and injects a `tariff` block into
every matching site's config.json under platforms/*/sites/.

CSV format - one row per (site, rate_period). Use multiple rows per site
to declare a multi-year rate history. Same workflow as add_system_specs.py.

Required columns:
  site_id           : matches sites/<id>/ folder
  type              : flat | tou | ppa | none
  export_mode       : net-metering | feed-in | none
  rate_from         : YYYY-MM-DD start date
  rate_to           : YYYY-MM-DD end date (blank for open-ended / current)
  flat              : R/kWh (for type=flat)
  peak / standard / off_peak : R/kWh (for type=tou)
  ppa               : R/kWh (for type=ppa)
  export_rate       : R/kWh (for export_mode=feed-in)
  export_rate_from  : YYYY-MM-DD - export periods can change at different dates
  export_rate_to    : YYYY-MM-DD or blank
  notes             : free text, ignored by the script

`type=none` rows produce sites with no tariff (financial block stays null).
Useful for the battery-only RDM site and the kirkwood-spar meter.

Run:
    python populate_tariffs.py
"""
import csv
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent
SITES_GLOB = REPO / "platforms" / "*" / "sites"

CSV_PATH = REPO / "tariffs.csv"


def parse_float(s):
    s = (s or "").strip()
    if not s or s.lower() in ("none", "n/a", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(s):
    """Pass through YYYY-MM-DD strings; convert blank/none to null."""
    s = (s or "").strip()
    return s if s and s.lower() not in ("none", "n/a", "-") else None


def build_tariffs_from_csv(path: Path) -> dict[str, dict]:
    """Read the CSV and return {site_id: tariff_block}."""
    by_site: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            sid = (row.get("site_id") or "").strip()
            if not sid:
                continue
            t_type = (row.get("type") or "").strip().lower()
            if t_type == "none":
                by_site[sid] = None     # explicit "no tariff"
                continue
            if t_type not in ("flat", "tou", "ppa"):
                if t_type:  # only warn when something non-empty was tried
                    print(f"  SKIP {sid}: unknown type '{t_type}'")
                continue

            entry = by_site.setdefault(sid, {
                "type": t_type,
                "vat_included": True,
                "export": {
                    "mode": (row.get("export_mode") or "none").strip().lower(),
                    "rate_periods": [],
                },
                "rate_periods": [],
            })

            # Import rate row
            rfrom = parse_date(row.get("rate_from"))
            rto   = parse_date(row.get("rate_to"))
            rate_row = {"from": rfrom, "to": rto}
            if t_type == "flat":
                rate_row["flat"] = parse_float(row.get("flat"))
            elif t_type == "ppa":
                rate_row["ppa"] = parse_float(row.get("ppa"))
            else:  # tou
                rate_row["tou"] = {
                    "peak":     parse_float(row.get("peak")),
                    "standard": parse_float(row.get("standard")),
                    "off_peak": parse_float(row.get("off_peak")),
                }
            entry["rate_periods"].append(rate_row)

            # Export rate row (optional)
            erate = parse_float(row.get("export_rate"))
            if erate is not None:
                entry["export"]["rate_periods"].append({
                    "from": parse_date(row.get("export_rate_from")) or rfrom,
                    "to":   parse_date(row.get("export_rate_to"))   or rto,
                    "rate": erate,
                })

    # Sort each site's rate periods by date for predictable lookup.
    for sid, tariff in by_site.items():
        if not tariff:
            continue
        tariff["rate_periods"].sort(key=lambda r: r.get("from") or "")
        tariff["export"]["rate_periods"].sort(key=lambda r: r.get("from") or "")
    return by_site


def inject_tariffs(by_site: dict[str, dict]) -> tuple[int, int]:
    """Apply each site's tariff to its config.json. Returns (written, unmatched)."""
    written, unmatched = 0, 0
    seen_sites = set()
    for config_path in REPO.glob("platforms/*/sites/*/config.json"):
        sid = config_path.parent.name
        seen_sites.add(sid)
        if sid not in by_site:
            continue
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["tariff"] = by_site[sid]    # None means no tariff
        config_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        written += 1
        print(f"  OK   {sid}")

    for sid in by_site:
        if sid not in seen_sites:
            print(f"  WARN {sid}: in CSV but no matching site folder")
            unmatched += 1
    return written, unmatched


def main():
    if not CSV_PATH.exists():
        print(f"Create {CSV_PATH} first. See the header in this script's docstring.")
        return
    by_site = build_tariffs_from_csv(CSV_PATH)
    print(f"Read tariffs for {len(by_site)} site(s):\n")
    written, unmatched = inject_tariffs(by_site)
    print(f"\n{written} configs updated, {unmatched} CSV entries with no site folder.")


if __name__ == "__main__":
    main()
