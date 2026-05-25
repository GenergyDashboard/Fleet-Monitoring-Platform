"""
FusionSolar fetch
=================
Northbound API client + orchestrator for the GenergyDashboard-API repo.

What it does, in order:
  1. Loads every sites/<id>/config.json under this platform folder.
  2. Logs in once to the Huawei Northbound API (token cached to disk and reused
     across runs - login is heavily rate limited).
  3. Fetches ALL sites in 5 batched calls (realKpi, hourly KPI, daily, monthly,
     yearly) by passing comma-separated station codes - keeps well inside
     Huawei's strict rate limits even with 28+ sites.
  4. Splits the batched responses per station and hands each site to
     processor.py, which writes data.json + history.json + hourly_history.json.

Run normally:
    python fetch.py

Discover station codes for new sites (prints the plant list, writes nothing):
    python fetch.py --discover

NETWORK NOTE: `intl.fusionsolar.huawei.com` does not resolve via standard DNS
from GitHub's cloud datacenter IPs. The `fix_dns_resolution()` helper below
mirrors the same trick used in the Nautica/FusionSolar scraper repo: query
Google DNS (8.8.8.8) explicitly, then write the resolved IP into /etc/hosts
so the rest of the script (and `requests`) can reach the host normally. With
this in place, the workflow runs fine on `ubuntu-latest` and no self-hosted
runner is needed.

Credentials come from environment variables (GitHub Secrets):
    FUSIONSOLAR_USERNAME   Northbound API username  (e.g. Ross@genergy.co.za)
    FUSIONSOLAR_PASSWORD   Northbound API password / system code
    FUSIONSOLAR_HOST       Portal host (default intl.fusionsolar.huawei.com)
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests

import processor

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"
TOKEN_CACHE = PLATFORM_DIR / ".fs_token.json"   # gitignored

HOST = os.environ.get("FUSIONSOLAR_HOST") or "intl.fusionsolar.huawei.com"

# Last-resort IP for intl.fusionsolar.huawei.com when even Google DNS can't
# reach Huawei's nameservers from the runner. Sourced from the proven
# Nautica/FusionSolar scraper repo - same host, same situation, this IP has
# been stable for months. If Huawei ever rotates it, the Google DNS lookup
# above this fallback should still find the new one.
FALLBACK_IP = "119.8.160.213"
# The Northbound "system code" is the password set when the API account was
# created in the FusionSolar UI. Kept under the FUSIONSOLAR_PASSWORD name to
# match the convention in Genergy's other repos - same string, the API just
# calls it systemCode.
USER = os.environ.get("FUSIONSOLAR_USERNAME", "")
SYSTEM_CODE = os.environ.get("FUSIONSOLAR_PASSWORD", "")

BASE_URL = f"https://{HOST}/thirdData"
REQUEST_TIMEOUT = 30        # seconds
RETRY_PAUSE = 60            # seconds to wait after a rate-limit hit
MAX_STATIONS_PER_CALL = 100 # Huawei caps batched stationCodes per request


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------

class FusionSolarError(RuntimeError):
    pass


class FusionSolarClient:
    """Thin Northbound API client. Handles login, token caching and one
    automatic re-login when the session expires mid-run."""

    def __init__(self, user: str, system_code: str):
        if not user or not system_code:
            raise FusionSolarError(
                "Missing credentials. Set FUSIONSOLAR_USERNAME and "
                "FUSIONSOLAR_PASSWORD in the environment."
            )
        self.user = user
        self.system_code = system_code
        self.session = requests.Session()
        self.token: str | None = None

    # -- authentication ----------------------------------------------------

    def _load_cached_token(self) -> bool:
        """Reuse a token from a previous run if it is recent enough.
        Northbound sessions last ~30 min; we treat anything under 25 as fresh."""
        if not TOKEN_CACHE.exists():
            return False
        try:
            cached = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False
        if time.time() - cached.get("issued_at", 0) > 25 * 60:
            return False
        self.token = cached.get("token")
        if self.token:
            self.session.cookies.set("XSRF-TOKEN", self.token)
            return True
        return False

    def _save_cached_token(self) -> None:
        TOKEN_CACHE.write_text(
            json.dumps({"token": self.token, "issued_at": time.time()}),
            encoding="utf-8",
        )

    def login(self, force: bool = False) -> None:
        """Authenticate and store the XSRF token. Uses the cache unless forced."""
        if not force and self._load_cached_token():
            return
        resp = self.session.post(
            f"{BASE_URL}/login",
            json={"userName": self.user, "systemCode": self.system_code},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        token = resp.headers.get("xsrf-token") or resp.cookies.get("XSRF-TOKEN")
        if not token:
            body = resp.json() if resp.content else {}
            raise FusionSolarError(
                f"Login failed - no token returned. failCode={body.get('failCode')}"
            )
        self.token = token
        self.session.cookies.set("XSRF-TOKEN", token)
        self._save_cached_token()

    # -- requests ----------------------------------------------------------

    def _post(self, endpoint: str, payload: dict, _retried: bool = False) -> dict:
        """POST to a thirdData endpoint, with one automatic re-login on an
        expired session and one pause-and-retry on a rate-limit response."""
        resp = self.session.post(
            f"{BASE_URL}/{endpoint}",
            json=payload,
            headers={"XSRF-TOKEN": self.token or ""},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()

        if body.get("success"):
            return body

        fail = body.get("failCode")
        # 305 = not logged in / session expired. 401 = invalid token.
        if fail in (305, 401) and not _retried:
            self.login(force=True)
            return self._post(endpoint, payload, _retried=True)
        # 407 / 429 = access frequency too high.
        if fail in (407, 429) and not _retried:
            print(f"  rate limited (failCode={fail}), pausing {RETRY_PAUSE}s...")
            time.sleep(RETRY_PAUSE)
            return self._post(endpoint, payload, _retried=True)

        raise FusionSolarError(f"{endpoint} failed - failCode={fail}")

    # -- endpoints ---------------------------------------------------------

    def get_station_list(self) -> list[dict]:
        """Plant list - used by --discover to find station codes.

        Uses the classic getStationList endpoint, which is confirmed working
        for this Northbound account (returned all 25 stations on test). Falls
        back to the paginated V2 'stations' endpoint only if getStationList is
        deprecated for the account (failCode 401)."""
        try:
            body = self._post("getStationList", {})
            data = body.get("data", [])
            if isinstance(data, list):
                return data
        except FusionSolarError:
            pass  # deprecated for this account - fall through to V2

        stations, page = [], 1
        while True:
            body = self._post("stations", {"pageNo": page})
            data = body.get("data", {})
            page_list = data.get("list", []) if isinstance(data, dict) else []
            stations.extend(page_list)
            total = data.get("total", 0) if isinstance(data, dict) else 0
            if len(stations) >= total or not page_list:
                break
            page += 1
        return stations

    def get_station_real_kpi(self, station_codes: list[str]) -> dict:
        """Current snapshot for many stations. Returns {station_code: item}."""
        body = self._post("getStationRealKpi",
                           {"stationCodes": ",".join(station_codes)})
        return {it.get("stationCode"): it for it in body.get("data", [])}

    def get_kpi_station_hour(self, station_codes: list[str], when_ms: int) -> dict:
        """Hourly KPI for the day containing when_ms. Returns {code: [rows]}."""
        body = self._post("getKpiStationHour",
                           {"stationCodes": ",".join(station_codes),
                            "collectTime": when_ms})
        return _group_by_station(body.get("data", []))

    def get_kpi_station_day(self, station_codes: list[str], when_ms: int) -> dict:
        """Daily KPI for the month containing when_ms. Returns {code: [rows]}."""
        body = self._post("getKpiStationDay",
                           {"stationCodes": ",".join(station_codes),
                            "collectTime": when_ms})
        return _group_by_station(body.get("data", []))

    def get_kpi_station_month(self, station_codes: list[str], when_ms: int) -> dict:
        """Monthly KPI for the year containing when_ms. Returns {code: [rows]}."""
        body = self._post("getKpiStationMonth",
                           {"stationCodes": ",".join(station_codes),
                            "collectTime": when_ms})
        return _group_by_station(body.get("data", []))

    def get_kpi_station_year(self, station_codes: list[str], when_ms: int) -> dict:
        """Yearly KPI for station lifetime. Returns {code: [rows]}."""
        body = self._post("getKpiStationYear",
                           {"stationCodes": ",".join(station_codes),
                            "collectTime": when_ms})
        return _group_by_station(body.get("data", []))


def _group_by_station(rows: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row.get("stationCode"), []).append(row)
    return grouped


# --------------------------------------------------------------------------
# Site config loading
# --------------------------------------------------------------------------

def load_site_configs() -> list[tuple[Path, dict]]:
    """Return (site_dir, config_dict) for every valid FusionSolar site."""
    if not SITES_DIR.exists():
        raise FusionSolarError(f"No sites directory at {SITES_DIR}")
    sites = []
    for config_path in sorted(SITES_DIR.glob("*/config.json")):
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"  SKIP {config_path.parent.name}: bad JSON ({exc})")
            continue
        code = config.get("station_code", "")
        if not code or code.startswith("NE=REPLACE"):
            print(f"  SKIP {config_path.parent.name}: station_code not set")
            continue
        sites.append((config_path.parent, config))
    return sites


def _chunk(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


# --------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------

DISCOVERY_FILE = PLATFORM_DIR / "stations_discovered.json"


# --------------------------------------------------------------------------
# DNS resolution fix (port of the Nautica/FusionSolar scraper's trick)
# --------------------------------------------------------------------------
# `intl.fusionsolar.huawei.com` does not resolve through GitHub's cloud
# nameservers. We work around this by querying Google DNS (8.8.8.8) for the
# IP, then writing it into /etc/hosts so `requests` (and any other library)
# can reach the host normally for the rest of the run.
#
# On a self-hosted runner with normal corporate DNS this is a no-op: the
# initial `socket.gethostbyname()` succeeds and we return immediately.

def fix_dns_resolution() -> None:
    """Ensure HOST resolves; patch /etc/hosts if standard DNS fails."""
    if not HOST or not HOST.strip():
        raise FusionSolarError(
            "FUSIONSOLAR_HOST is empty. If you set it as a GitHub secret "
            "with no value, either remove the secret or give it the value "
            "'intl.fusionsolar.huawei.com'."
        )
    print(f"Checking DNS for {HOST}...")
    try:
        ip = socket.gethostbyname(HOST)
        print(f"  DNS OK: {HOST} -> {ip}")
        return
    except socket.gaierror:
        print(f"  DNS failed, trying Google DNS (8.8.8.8) fallback...")

    # Try `dig` against Google DNS - present on ubuntu-latest by default.
    resolved_ip = None
    try:
        result = subprocess.run(
            ["dig", "+short", HOST, "@8.8.8.8"],
            capture_output=True, text=True, timeout=10,
        )
        ips = [l.strip() for l in result.stdout.strip().split("\n")
               if l.strip() and not l.strip().endswith(".")]
        if ips:
            resolved_ip = ips[0]
            print(f"  Resolved via Google DNS: {resolved_ip}")
    except Exception as e:
        print(f"  dig lookup failed: {e}")

    if not resolved_ip:
        resolved_ip = FALLBACK_IP
        print(f"  Using fallback IP: {resolved_ip}")

    # Append HOST -> resolved_ip mapping to /etc/hosts. requests reads this
    # via the OS resolver on its next call, so no extra wiring needed.
    hosts_entry = f"{resolved_ip} {HOST}\n"
    try:
        with open("/etc/hosts", "r") as f:
            if HOST in f.read():
                print("  /etc/hosts already has an entry for this host")
                return
        try:
            # Prefer sudo - ubuntu-latest's default user has it passwordless.
            result = subprocess.run(
                ["sudo", "tee", "-a", "/etc/hosts"],
                input=hosts_entry, capture_output=True, text=True, timeout=5,
            )
            if result.returncode != 0:
                raise RuntimeError("sudo tee failed")
        except Exception:
            # Last resort: try writing directly (works only if writable).
            with open("/etc/hosts", "a") as f:
                f.write(hosts_entry)
        print(f"  Added to /etc/hosts: {hosts_entry.strip()}")
    except Exception as e:
        raise FusionSolarError(
            f"Could not patch /etc/hosts: {e}. Without this the host "
            f"{HOST} cannot be reached from this runner."
        )

    # Verify the patch worked.
    try:
        ip = socket.gethostbyname(HOST)
        print(f"  DNS now resolves: {HOST} -> {ip}")
    except socket.gaierror:
        raise FusionSolarError(
            f"DNS still failing after patching /etc/hosts. Open-Meteo "
            f"connectivity probably also affected - check runner network."
        )


def run_discover() -> None:
    """Print every plant visible to the Northbound account AND save the full
    raw station list to stations_discovered.json.

    The saved file contains code, name, capacity and address per plant - upload
    it to have all sites/<id>/config.json files generated automatically."""
    fix_dns_resolution()
    client = FusionSolarClient(USER, SYSTEM_CODE)
    client.login()
    stations = client.get_station_list()

    print(f"\n{len(stations)} stations visible to this account:\n")
    print(f"  {'PLANT NAME':<40} {'STATION CODE':<16} {'CAPACITY':<10} ADDRESS")
    print(f"  {'-' * 40} {'-' * 16} {'-' * 10} {'-' * 20}")
    for s in stations:
        name = s.get("stationName") or s.get("plantName") or "?"
        code = s.get("stationCode") or s.get("plantCode") or "?"
        cap = s.get("capacity") or s.get("aidCapacity") or "?"
        addr = s.get("stationAddr") or s.get("plantAddress") or ""
        print(f"  {name:<40} {code:<16} {str(cap):<10} {addr}")

    # Save the complete raw response - this is what gets uploaded back.
    DISCOVERY_FILE.write_text(
        json.dumps(stations, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nFull raw list saved to: {DISCOVERY_FILE}")
    print("Upload that file to have every config.json generated automatically.\n")


def run_fetch() -> None:
    """Normal run: fetch all sites in 5 batched calls and write their files."""
    fix_dns_resolution()
    sites = load_site_configs()
    if not sites:
        print("No sites ready to fetch.")
        return
    print(f"Fetching {len(sites)} FusionSolar site(s)...")

    client = FusionSolarClient(USER, SYSTEM_CODE)
    client.login()

    now_ms = int(time.time() * 1000)
    by_code = {cfg["station_code"]: (site_dir, cfg) for site_dir, cfg in sites}

    real_kpi: dict = {}
    hourly: dict = {}
    daily: dict = {}
    monthly: dict = {}
    yearly: dict = {}

    # Batch by 100 station codes per call to respect Huawei's per-request cap.
    # 5 endpoints * (sites / 100) calls total - still 5 calls for 25 sites.
    for batch in _chunk(list(by_code.keys()), MAX_STATIONS_PER_CALL):
        real_kpi.update(client.get_station_real_kpi(batch))
        hourly.update(client.get_kpi_station_hour(batch, now_ms))
        daily.update(client.get_kpi_station_day(batch, now_ms))
        monthly.update(client.get_kpi_station_month(batch, now_ms))
        yearly.update(client.get_kpi_station_year(batch, now_ms))

    written, skipped = 0, 0
    for code, (site_dir, config) in by_code.items():
        rk = real_kpi.get(code)
        hr = hourly.get(code, [])
        dy = daily.get(code, [])
        mo = monthly.get(code, [])
        yr = yearly.get(code, [])
        if rk is None and not hr and not dy:
            print(f"  SKIP {config['site_id']}: no data returned for {code}")
            skipped += 1
            continue
        processor.write_site(site_dir, config, rk, hr, dy, mo, yr)
        print(f"  OK   {config['site_id']}")
        written += 1

    print(f"\nDone. {written} written, {skipped} skipped.")
    print("Weather is refreshed by its own workflow (.github/workflows/weather.yml).")


def main() -> int:
    try:
        if "--discover" in sys.argv:
            run_discover()
        else:
            run_fetch()
    except FusionSolarError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        print("If this is a DNS or connection failure: check the DNS fix log "
              "above. If /etc/hosts was patched but requests still failed, "
              "the FALLBACK_IP in fetch.py may be stale - update it from a "
              "fresh `nslookup intl.fusionsolar.huawei.com 8.8.8.8` and retry.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
