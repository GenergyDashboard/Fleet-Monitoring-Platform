"""
VRM (Victron Remote Monitoring) fetch
=====================================
Pulls energy data for every Victron VRM installation listed in
platforms/vrm/sites/*/config.json and hands each one to processor.py.

Why VRM is the easiest platform we have:
  - Public REST API at vrmapi.victronenergy.com - no DNS games, resolves
    fine from cloud runners (unlike Huawei FusionSolar)
  - Personal Access Token auth - no token expiry, no session juggling
  - Documented response shapes, JSON-native
  - Built-in batching via the stats endpoint with custom date ranges

Run normally:
    python fetch.py

Discover sites visible to your token (writes nothing, just prints):
    python fetch.py --discover

Required environment variables (GitHub Secrets):
    VRM_TOKEN          Personal Access Token from VRM portal
                       (Generate at: https://vrm.victronenergy.com -> Profile
                        -> Integrations -> Access tokens -> Add)
    VRM_USER_ID        User ID for the account (the multi-digit number after
                       /users/ in your profile URL). Only needed for --discover.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# processor is imported lazily inside run_fetch() so that --discover can
# run from anywhere (including a copied platforms/vrm/ folder without the
# rest of the repo around it). processor needs shared/powerflow.py which
# only exists in the full repo layout.

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

BASE_URL = os.environ.get("VRM_BASE_URL") or "https://vrmapi.victronenergy.com/v2"
TOKEN = os.environ.get("VRM_TOKEN")
USER_ID = os.environ.get("VRM_USER_ID")    # only needed for --discover

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))


class VRMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------

class VRMClient:
    """Thin client wrapping the VRM v2 REST API.

    Uses Personal Access Token auth - the cleanest option Victron offers,
    no expiry, no refresh dance. Generate one in the VRM portal under
    Profile -> Integrations -> Access tokens.
    """

    def __init__(self, token: str):
        if not token:
            raise VRMError("VRM_TOKEN environment variable is not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            # NOTE the prefix is 'Token', not 'Bearer'. The community has
            # had repeated issues with this - 'Bearer' is for username/password
            # login flow tokens; 'Token' is for PATs. They are NOT
            # interchangeable.
            "X-Authorization": f"Token {token}",
        })

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{BASE_URL}{path}"
        r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 401:
            raise VRMError("VRM auth failed (401). Token invalid or expired.")
        if r.status_code == 403:
            raise VRMError(f"VRM auth forbidden (403). Token lacks access "
                            f"to {path}. Generate a token with full read access.")
        r.raise_for_status()
        body = r.json()
        if isinstance(body, dict) and body.get("success") is False:
            raise VRMError(f"VRM API error: {body.get('errors') or body}")
        return body

    # --------------------------------------------------------------
    # User / discovery
    # --------------------------------------------------------------

    def get_me(self) -> dict:
        """Return the authenticated user's profile.

        VRM exposes /users/me which returns the user record for whoever's
        token is making the call. This means we don't need to ask the user
        to dig their user ID out of a URL - we just look it up.
        """
        return self._get("/users/me")

    def list_user_installations(self, user_id: str) -> list[dict]:
        """All installations visible to the given user ID."""
        body = self._get(f"/users/{user_id}/installations")
        return body.get("records", [])

    # --------------------------------------------------------------
    # Per-installation data
    # --------------------------------------------------------------

    def get_system_overview(self, id_site: int) -> dict:
        """Current state of the installation - power, alarms, gateway info."""
        return self._get(f"/installations/{id_site}/system-overview")

    def get_kwh_stats(self, id_site: int, interval: str,
                       start_ts: int, end_ts: int) -> dict:
        """Energy stats over a time range.

        Args:
            interval: 'hours' / 'days' / 'months' / 'years'
            start_ts, end_ts: Unix epoch seconds

        Returns the raw response. Records dict keys are VRM attribute codes:
            Pb = Solar yield (PV production), kWh
            Pc = Consumption, kWh
            Gc = Grid consumed (import), kWh
            Gb = Grid generated (export), kWh
            Bc = Battery to consumers, kWh
            Bg = Battery to grid, kWh
            Other codes appear for specific system topologies.

        Field semantics for battery flows are not 100% documented; verify
        against your actual VRM portal kWh display once data lands.
        """
        return self._get(
            f"/installations/{id_site}/stats",
            params={
                "type": "kwh",
                "interval": interval,
                "start": start_ts,
                "end": end_ts,
            },
        )


# --------------------------------------------------------------------------
# Config loading
# --------------------------------------------------------------------------

def load_site_configs() -> list[tuple[Path, dict]]:
    """Returns [(site_dir, config_dict)] for every site folder with a config."""
    out = []
    if not SITES_DIR.exists():
        return out
    for site_dir in sorted(SITES_DIR.iterdir()):
        if not site_dir.is_dir():
            continue
        cfg_path = site_dir / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  SKIP {site_dir.name}: bad JSON in config.json - {e}")
            continue
        id_site = cfg.get("id_site") or cfg.get("station_code")
        if not id_site:
            print(f"  SKIP {site_dir.name}: no id_site in config.json")
            continue
        out.append((site_dir, cfg))
    return out


# --------------------------------------------------------------------------
# Date range helpers
# --------------------------------------------------------------------------

def _today_window_sast() -> tuple[int, int]:
    """Unix-seconds range covering today in SAST (00:00 -> now)."""
    now = datetime.now(tz=SAST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _month_window_sast() -> tuple[int, int]:
    """Unix-seconds range from start-of-month SAST to now."""
    now = datetime.now(tz=SAST)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _year_window_sast() -> tuple[int, int]:
    """Unix-seconds range from start-of-year SAST to now."""
    now = datetime.now(tz=SAST)
    start = now.replace(month=1, day=1, hour=0, minute=0,
                         second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


# --------------------------------------------------------------------------
# Orchestrator
# --------------------------------------------------------------------------

def run_discover() -> None:
    """Print every VRM installation visible to the token.

    Saves the full list to vrm_discovered.json. Use this to find idSite
    values for new sites. User ID is auto-discovered from the token via
    /users/me - no need to dig it out of a URL.
    """
    client = VRMClient(TOKEN)

    # Auto-resolve the user ID from the token. Fall back to env var only
    # if /users/me is unavailable for some reason.
    user_id = USER_ID
    if not user_id:
        try:
            me = client.get_me()
            # /users/me returns {"success": true, "user": {"id": 12345, ...}}
            user_data = me.get("user") or me.get("data") or {}
            user_id = (user_data.get("id") or user_data.get("idUser")
                       or user_data.get("userId"))
            if user_id:
                print(f"Auto-discovered user ID: {user_id}")
        except Exception as exc:
            raise VRMError(
                f"Could not auto-discover user ID via /users/me: {exc}. "
                f"Set VRM_USER_ID env var manually - find it at "
                f"vrm.victronenergy.com -> Preferences (check URL)."
            )

    if not user_id:
        raise VRMError(
            "VRM_USER_ID could not be determined. Set it manually as an "
            "environment variable."
        )

    installs = client.list_user_installations(str(user_id))
    print(f"\nFound {len(installs)} installation(s) visible to this token:\n")
    print(f"  {'NAME':<40} {'ID':<10} ACCESS")
    print(f"  {'-' * 40} {'-' * 10} {'-' * 10}")
    for inst in installs:
        name = (inst.get("name") or "(unnamed)")[:39]
        sid = inst.get("idSite", "?")
        access = inst.get("access_level", inst.get("role", ""))
        print(f"  {name:<40} {sid:<10} {access}")

    # Persist the raw discovery for inspection / config generation
    out_path = PLATFORM_DIR / "vrm_discovered.json"
    out_path.write_text(json.dumps(installs, indent=2) + "\n",
                          encoding="utf-8")
    print(f"\nRaw discovery saved to {out_path.name}")
    print("Create a sites/<slug>/config.json for each site you want to monitor.")


def run_fetch() -> None:
    """Normal run: fetch every configured VRM site."""
    # Lazy import: processor depends on shared/, which is only available
    # when running from inside the full repo. Keep --discover usable from
    # anywhere by deferring this until we actually need it.
    import processor

    sites = load_site_configs()
    if not sites:
        print("No VRM sites configured yet. Create config.json files under "
              "platforms/vrm/sites/<slug>/ and run again. Use `python fetch.py "
              "--discover` to list available sites first.")
        return

    print(f"Fetching {len(sites)} VRM site(s)...")
    client = VRMClient(TOKEN)

    today_start, today_end = _today_window_sast()
    month_start, month_end = _month_window_sast()
    year_start, year_end   = _year_window_sast()

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        id_site = int(config.get("id_site") or config.get("station_code"))
        try:
            overview = client.get_system_overview(id_site)
            hourly = client.get_kwh_stats(id_site, "hours",
                                            today_start, today_end)
            daily = client.get_kwh_stats(id_site, "days",
                                           month_start, month_end)
            monthly = client.get_kwh_stats(id_site, "months",
                                             year_start, year_end)
            yearly = client.get_kwh_stats(id_site, "years",
                                            year_start - (10 * 365 * 86400),
                                            year_end)
        except VRMError as exc:
            print(f"  FAIL {sid}: {exc}")
            skipped += 1
            continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}")
            skipped += 1
            continue

        try:
            processor.write_site(site_dir, config,
                                  overview=overview,
                                  hourly=hourly,
                                  daily=daily,
                                  monthly=monthly,
                                  yearly=yearly)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1

        # Be polite to the API - small spacing between calls
        time.sleep(0.5)

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    if not TOKEN:
        print("ERROR: VRM_TOKEN environment variable not set.", file=sys.stderr)
        print("Generate a Personal Access Token at:", file=sys.stderr)
        print("  https://vrm.victronenergy.com -> Profile -> Integrations -> "
              "Access tokens -> Add", file=sys.stderr)
        return 1
    try:
        if "--discover" in sys.argv:
            run_discover()
        else:
            run_fetch()
    except VRMError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
