"""
SolarmanPV OpenAPI fetch
========================
Pulls energy data for every SolarmanPV site listed under
platforms/solarmanpv/sites/<slug>/config.json. Replaces the older
Playwright scraper at pro.solarmanpv.com with proper REST API calls.

Why this is reliable:
  - Public OpenAPI at globalapi.solarmanpv.com - resolves and accepts
    connections from anywhere (no DNS games like FusionSolar)
  - Token-based auth with 2-month token lifetime
  - Documented endpoints, JSON responses, ~50 req/min rate limit
  - Same architecture as fetch.py for FusionSolar/VRM

Run normally:
    python fetch.py

Discover sites visible to your account (writes nothing, prints + saves
discovery JSON):
    python fetch.py --discover

Credentials (set as GitHub Secrets):
    SOLARMAN_APPID         App ID assigned by Solarman support
    SOLARMAN_APPSECRET     App Secret assigned by Solarman support
    SOLARMAN_EMAIL         Your pro.solarmanpv.com account email
    SOLARMAN_PASSWORD      Your pro.solarmanpv.com account password (raw -
                            this script hashes it with SHA-256 before sending)

To request AppID/AppSecret: email customerservice@solarmanpv.com explaining
you need OpenAPI access for fleet monitoring. They reply with the credentials
within a few business days. The email/password is your normal Solarman
business account login.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# processor is imported lazily inside run_fetch() so --discover works
# standalone without shared/ on the path.

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

BASE_URL = os.environ.get("SOLARMAN_BASE_URL") or "https://globalapi.solarmanpv.com"
APP_ID = os.environ.get("SOLARMAN_APPID")
APP_SECRET = os.environ.get("SOLARMAN_APPSECRET")
EMAIL = os.environ.get("SOLARMAN_EMAIL")
PASSWORD = os.environ.get("SOLARMAN_PASSWORD")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))
TOKEN_CACHE = PLATFORM_DIR / ".sm_token.json"   # gitignored


class SolarmanError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SolarmanClient:
    """SolarmanPV OpenAPI client.

    Auth flow:
      POST /account/v1.0/token?appId=<app_id>
      Body: {"appSecret": "<secret>", "email": "...", "password": "<sha256-hex>"}
      Returns: {"access_token": "...", "expires_in": 63072000}  # 2 months

    Token is cached to disk because login is rate-limited too. We re-auth
    when the cache is missing, older than 50 days, or rejected with 401.
    """

    def __init__(self, app_id: str, app_secret: str,
                  email: str, password: str):
        for arg, name in [(app_id, "SOLARMAN_APPID"),
                          (app_secret, "SOLARMAN_APPSECRET"),
                          (email, "SOLARMAN_EMAIL"),
                          (password, "SOLARMAN_PASSWORD")]:
            if not arg:
                raise SolarmanError(f"{name} environment variable not set")
        self.app_id = app_id
        self.app_secret = app_secret
        self.email = email
        # Solarman expects the password as a SHA-256 hex digest of the
        # plain-text password. Hash here so the raw password never crosses
        # the network or appears in logs.
        self.password_sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.token: str | None = None
        self.token_expires_at = 0.0

    def login(self) -> str:
        """Authenticate and cache the access token to disk."""
        # Try cached token first
        if self.token_expires_at == 0.0 and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                if (cached.get("expires_at", 0) - time.time()) > 86400:
                    self.token = cached["token"]
                    self.token_expires_at = cached["expires_at"]
                    print(f"  Using cached SolarmanPV token (expires in "
                          f"{int((self.token_expires_at - time.time()) / 86400)}d)")
                    return self.token
            except (json.JSONDecodeError, KeyError):
                pass

        # Fresh login
        url = f"{BASE_URL}/account/v1.0/token?appId={self.app_id}&language=en"
        body = {"appSecret": self.app_secret,
                "email": self.email,
                "password": self.password_sha}
        r = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise SolarmanError(f"Login HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        if not data.get("success") or not data.get("access_token"):
            # SolarmanPV uses code="0" for success and code-strings for errors
            code = data.get("code", "?")
            msg = data.get("msg") or data.get("errorMsg") or "unknown error"
            raise SolarmanError(f"Login failed (code {code}): {msg}")
        self.token = data["access_token"]
        expires_in = data.get("expires_in", 60 * 86400)
        self.token_expires_at = time.time() + expires_in

        try:
            TOKEN_CACHE.write_text(json.dumps({
                "token": self.token,
                "expires_at": self.token_expires_at,
            }))
        except OSError:
            pass        # cache is best-effort; if we can't write, just re-login
        print(f"  Logged in to SolarmanPV - token valid {int(expires_in / 86400)}d")
        return self.token

    def _post(self, path: str, body: dict, _retried: bool = False) -> dict:
        if not self.token:
            self.login()
        url = f"{BASE_URL}{path}?language=en"
        headers = {"Authorization": f"bearer {self.token}",
                   "Content-Type": "application/json"}
        r = self.session.post(url, headers=headers, json=body,
                                timeout=REQUEST_TIMEOUT)
        if r.status_code == 401 and not _retried:
            # Token rejected - re-login and retry once
            print("  Token expired/rejected, re-authenticating...")
            self.token = None
            self.token_expires_at = 0
            if TOKEN_CACHE.exists():
                TOKEN_CACHE.unlink()
            self.login()
            return self._post(path, body, _retried=True)
        r.raise_for_status()
        data = r.json()
        if not data.get("success", True):
            code = data.get("code", "?")
            msg = data.get("msg") or data.get("errorMsg") or "unknown"
            raise SolarmanError(f"{path} failed (code {code}): {msg}")
        return data

    # --------------------------------------------------------------
    # Endpoints used
    # --------------------------------------------------------------

    def list_stations(self, page: int = 1, size: int = 50) -> list[dict]:
        """All stations visible to the account."""
        data = self._post("/station/v1.0/list", {"page": page, "size": size})
        return data.get("stationList", [])

    def station_realtime(self, station_id: int) -> dict:
        """Current real-time snapshot of one station."""
        data = self._post("/station/v1.0/realTime", {"stationId": station_id})
        items = data.get("stationDataItems", []) or [data]
        return items[0] if items else {}

    def station_history(self, station_id: int, start_ts: int, end_ts: int,
                          time_type: int) -> list[dict]:
        """Historical data series.

        time_type:
          1 = 5-minute granularity (one day max)
          2 = daily (one month max)
          3 = monthly (one year max)
          4 = yearly (lifetime)
        """
        data = self._post("/station/v1.0/history", {
            "stationId": station_id,
            "startTime": start_ts,
            "endTime": end_ts,
            "timeType": time_type,
        })
        return data.get("stationDataItems", [])


# ---------------------------------------------------------------------------
# Site config loading
# ---------------------------------------------------------------------------

def load_site_configs() -> list[tuple[Path, dict]]:
    out = []
    if not SITES_DIR.exists():
        return out
    for site_dir in sorted(SITES_DIR.iterdir()):
        if not site_dir.is_dir() or site_dir.name.startswith("_"):
            continue
        cfg_path = site_dir / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  SKIP {site_dir.name}: bad JSON - {e}")
            continue
        if not cfg.get("station_id") and not cfg.get("id_site"):
            print(f"  SKIP {site_dir.name}: no station_id in config.json")
            continue
        out.append((site_dir, cfg))
    return out


# ---------------------------------------------------------------------------
# Date window helpers (SAST)
# ---------------------------------------------------------------------------

def _today_window_sast() -> tuple[int, int]:
    now = datetime.now(tz=SAST)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _month_window_sast() -> tuple[int, int]:
    now = datetime.now(tz=SAST)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _year_window_sast() -> tuple[int, int]:
    now = datetime.now(tz=SAST)
    start = now.replace(month=1, day=1, hour=0, minute=0, second=0,
                          microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_discover() -> None:
    """List every station visible to the OpenAPI account."""
    client = SolarmanClient(APP_ID, APP_SECRET, EMAIL, PASSWORD)
    client.login()
    stations = client.list_stations()
    print(f"\nFound {len(stations)} station(s) visible to this account:\n")
    print(f"  {'NAME':<40} {'STATION ID':<12} {'CAPACITY':<12} LOCATION")
    print(f"  {'-' * 40} {'-' * 12} {'-' * 12} {'-' * 20}")
    for s in stations:
        name = (s.get("name") or "(unnamed)")[:39]
        sid = s.get("id", "?")
        cap = f"{s.get('installedCapacity', 0):.1f} kWp"
        loc = (s.get("locationAddress") or
               f"{s.get('locationLat', '?')},{s.get('locationLng', '?')}")[:30]
        print(f"  {name:<40} {sid:<12} {cap:<12} {loc}")

    out_path = PLATFORM_DIR / "solarmanpv_discovered.json"
    out_path.write_text(json.dumps(stations, indent=2) + "\n",
                          encoding="utf-8")
    print(f"\nRaw discovery saved to {out_path.name}")


def run_fetch() -> None:
    """Fetch all configured SolarmanPV sites."""
    import processor      # lazy - needs shared/, only available in full repo

    sites = load_site_configs()
    if not sites:
        print("No SolarmanPV sites configured yet. Create config.json files "
              "under platforms/solarmanpv/sites/<slug>/ and run again. Use "
              "`python fetch.py --discover` to list available sites first.")
        return

    print(f"Fetching {len(sites)} SolarmanPV site(s)...")
    client = SolarmanClient(APP_ID, APP_SECRET, EMAIL, PASSWORD)
    client.login()

    today_start, today_end = _today_window_sast()
    month_start, month_end = _month_window_sast()
    year_start, year_end = _year_window_sast()
    lifetime_start = year_start - (15 * 365 * 86400)

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        station_id = config.get("station_id") or config.get("id_site")
        try:
            realtime = client.station_realtime(station_id)
            # 5-min granularity for today's hourly aggregation
            hourly_raw = client.station_history(station_id, today_start,
                                                  today_end, time_type=1)
            daily = client.station_history(station_id, month_start,
                                             month_end, time_type=2)
            monthly = client.station_history(station_id, year_start,
                                               year_end, time_type=3)
            yearly = client.station_history(station_id, lifetime_start,
                                              year_end, time_type=4)
        except SolarmanError as exc:
            print(f"  FAIL {sid}: {exc}")
            skipped += 1
            continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}")
            skipped += 1
            continue

        try:
            processor.write_site(site_dir, config,
                                  realtime=realtime,
                                  hourly_raw=hourly_raw,
                                  daily=daily,
                                  monthly=monthly,
                                  yearly=yearly)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1

        time.sleep(0.5)        # be polite to the API

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    try:
        if "--discover" in sys.argv:
            run_discover()
        else:
            run_fetch()
    except SolarmanError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
