"""
Regenerate data/SITES.md from sites_index.json
==============================================
The dashboard reads SITES.md as the primary hide list - unchecked
sites disappear from the fleet view. This script keeps SITES.md in
sync with the actual list of sites in the repo:

  - Adds entries for any NEW sites that aren't already in SITES.md
    (defaults them to checked = visible).
  - Removes entries for sites that no longer exist in sites_index.json
    (e.g. you deleted a site folder).
  - PRESERVES the checked/unchecked state of every existing line.
    So your tick/untick decisions don't get wiped when new sites
    are added.

Usage:
    python regenerate_sites_md.py

Recommended to run any time you add new site configs. The build_sites_index
script already runs this automatically (see hook below if/when added).
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent
SITES_INDEX = REPO / "sites_index.json"
SITES_MD = REPO / "data" / "SITES.md"

PLATFORM_LABELS = {
    "fusionsolar": "FusionSolar (Huawei)",
    "vrm":         "VRM (Victron)",
    "solarmanpv":  "SolarmanPV",
    "soliscloud":  "SolisCloud",
    "sunsynk":     "Sunsynk",
    "sigenergy":   "Sigenergy",
    "sungrow":     "Sungrow",
    "goodwe":      "GoodWe",
}
PLATFORM_ORDER = ["fusionsolar", "vrm", "solarmanpv", "soliscloud",
                   "sunsynk", "sigenergy", "sungrow", "goodwe"]


def parse_existing_states(md_text: str) -> dict[str, bool]:
    """Parse existing SITES.md to extract {site_id: is_checked} for each
    task list item. Lines we don't recognise are simply ignored - the
    parser is forgiving.
    """
    states: dict[str, bool] = {}
    # Matches: '- [x] `slug`' or '- [ ] slug' (with or without backticks).
    pattern = re.compile(r"^\s*-\s*\[(\s|x|X)\]\s*`?([a-z0-9_-]+)`?", re.MULTILINE)
    for match in pattern.finditer(md_text):
        check_char, slug = match.group(1), match.group(2)
        states[slug] = check_char.lower() == "x"
    return states


def build_markdown(sites: list[dict], existing_states: dict[str, bool]) -> str:
    by_platform: dict[str, list[dict]] = defaultdict(list)
    for s in sites:
        by_platform[s["platform"]].append(s)

    ordered = ([p for p in PLATFORM_ORDER if p in by_platform]
                + [p for p in sorted(by_platform) if p not in PLATFORM_ORDER])

    lines = [
        "# Sites visible in the fleet dashboard",
        "",
        "Tick a box to **show** that site. Untick to **hide** it.",
        "Changes take effect on next dashboard load - no workflow run needed.",
        "",
        "> **HOW THIS FILE WORKS**",
        "> ",
        "> This file IS the hide list. The dashboard reads it directly on every load.",
        "> Unchecked sites disappear from the fleet view (grid, map, status counts, dropdowns)",
        "> but their workflows keep running and data files keep updating - direct URLs like",
        "> `site.html?site=<id>` continue to work.",
        "> ",
        "> Two ways to edit:",
        "> 1. **On github.com**: open this file in your browser, click the checkboxes directly,",
        ">    GitHub auto-commits each click. Easiest.",
        "> 2. **Locally**: open in any text editor, change `[x]` to `[ ]` or vice versa,",
        ">    save, commit, push.",
        "> ",
        "> Each line MUST be exactly: `- [x] site-slug` or `- [ ] site-slug` (slug optionally",
        "> wrapped in backticks). Don't change the indentation or the parser breaks.",
        "> ",
        "> New sites are auto-added by `regenerate_sites_md.py` as checked (visible by default).",
        "",
    ]

    total_visible = 0
    total_hidden = 0
    for p in ordered:
        sites_p = sorted(by_platform[p], key=lambda x: x["name"].lower())
        label = PLATFORM_LABELS.get(p, p)

        # Compute counts after applying preserved + default states
        platform_visible = 0
        platform_hidden = 0
        rows = []
        for s in sites_p:
            sid = s["site_id"]
            is_checked = existing_states.get(sid, True)         # default: visible
            mark = "[x]" if is_checked else "[ ]"
            display = s["name"][:50]
            rows.append(f"- {mark} `{sid}` — {display}")
            if is_checked:
                platform_visible += 1
            else:
                platform_hidden += 1
        total_visible += platform_visible
        total_hidden += platform_hidden

        lines.append(f"## {label} — {platform_visible} of {len(sites_p)} visible")
        lines.append("")
        lines.extend(rows)
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(f"**Summary:** {total_visible} visible / {total_hidden} hidden / "
                  f"{total_visible + total_hidden} total")
    return "\n".join(lines) + "\n"


def main() -> int:
    if not SITES_INDEX.exists():
        print(f"ERROR: {SITES_INDEX} not found. Run build_sites_index.py first.",
              file=sys.stderr)
        return 1
    idx = json.loads(SITES_INDEX.read_text(encoding="utf-8"))
    sites = idx.get("sites", [])
    if not sites:
        print("WARNING: sites_index.json contains zero sites.", file=sys.stderr)

    existing_states: dict[str, bool] = {}
    if SITES_MD.exists():
        existing_states = parse_existing_states(SITES_MD.read_text(encoding="utf-8"))
        print(f"Preserved tick state for {len(existing_states)} existing site(s)")

    markdown = build_markdown(sites, existing_states)
    SITES_MD.parent.mkdir(exist_ok=True)
    SITES_MD.write_text(markdown, encoding="utf-8")
    visible = sum(1 for line in markdown.splitlines() if re.match(r"^\s*-\s*\[x\]", line, re.I))
    hidden = sum(1 for line in markdown.splitlines() if re.match(r"^\s*-\s*\[\s\]", line))
    print(f"Wrote {SITES_MD.relative_to(REPO)} with {visible} visible, "
          f"{hidden} hidden ({visible + hidden} total)")

    # Detect added/removed since last run
    in_md = set(existing_states.keys())
    in_idx = {s["site_id"] for s in sites}
    added = in_idx - in_md
    removed = in_md - in_idx
    if added:
        print(f"  New sites auto-added (default visible): {len(added)}")
        for sid in sorted(added)[:10]:
            print(f"    + {sid}")
        if len(added) > 10:
            print(f"    ... and {len(added) - 10} more")
    if removed:
        print(f"  Sites no longer in index (removed from SITES.md): {len(removed)}")
        for sid in sorted(removed)[:10]:
            print(f"    - {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
