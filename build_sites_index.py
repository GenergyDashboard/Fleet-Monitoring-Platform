"""
Build a sites_index.json manifest at the repo root.

The dashboard discovers all sites by fetching this single file rather than
hardcoding URLs. Re-run whenever sites are added, removed, or renamed.
The fetch workflow can also run this as a final step so the index stays
fresh automatically.

Output shape:
{
  "generated_at": "2026-05-20T14:30:00+02:00",
  "sites": [
    {
      "site_id": "bel-essex-valeo",
      "name": "Bel Essex (Valeo)",
      "platform": "fusionsolar",
      "location": {"lat": ..., "lon": ..., "town": "..."},
      "capacity_kwp": 921.2,
      "data_path":    "platforms/fusionsolar/sites/bel-essex-valeo/data.json",
      "history_path": "platforms/fusionsolar/sites/bel-essex-valeo/history.json",
      "hourly_path":  "platforms/fusionsolar/sites/bel-essex-valeo/hourly_history.json",
      "config_path":  "platforms/fusionsolar/sites/bel-essex-valeo/config.json",
      "site_url":     "site.html?site=bel-essex-valeo"
    }
  ]
}
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent
OUT = REPO / "sites_index.json"
SAST = timezone(timedelta(hours=2))


def main() -> None:
    sites = []
    for config_path in sorted(REPO.glob("platforms/*/sites/*/config.json")):
        # Skip example/template folders (prefixed with underscore)
        if config_path.parent.name.startswith("_"):
            continue
        config = json.loads(config_path.read_text(encoding="utf-8"))
        site_dir = config_path.parent
        rel = site_dir.relative_to(REPO).as_posix()
        sites.append({
            "site_id": config["site_id"],
            "name": config["name"],
            "platform": config["platform"],
            "hidden": bool(config.get("hidden", False)),
            "location": config.get("location"),
            "capacity_kwp": config.get("capacity_kwp"),
            "data_path":    f"{rel}/data.json",
            "history_path": f"{rel}/history.json",
            "hourly_path":  f"{rel}/hourly_history.json",
            "config_path":  f"{rel}/config.json",
            "site_url":     f"site.html?site={config['site_id']}",
            "modules":      config.get("modules", {}),
        })

    manifest = {
        "generated_at": datetime.now(tz=SAST).strftime("%Y-%m-%dT%H:%M:%S+02:00"),
        "sites": sites,
    }
    OUT.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"Wrote {OUT.relative_to(REPO)} with {len(sites)} site(s).")


if __name__ == "__main__":
    main()
