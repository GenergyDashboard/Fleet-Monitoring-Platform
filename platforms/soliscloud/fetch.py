"""
SolisCloud (Ginlong) API fetch
==============================
Pulls energy data for every configured SolisCloud site via the OpenAPI at
www.soliscloud.com:13333. Different from the other platforms: SolisCloud
uses HMAC-SHA1 request signing instead of bearer tokens.

Run normally:
    python fetch.py

Discover sites:
    python fetch.py --discover

Required env vars (set as GitHub Secrets):
    SOLIS_KEY_ID       API Key ID    from soliscloud.com/#/apiManage
    SOLIS_KEY_SECRET   API Key Secret from soliscloud.com/#/apiManage

API access requires a support ticket first to enable API for your account.
See README.md for full setup steps.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlparse

import requests

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

# SolisCloud uses a non-standard port :13333 - this IS part of the URL.
BASE_URL = os.environ.get("SOLIS_BASE_URL") or "https://www.soliscloud.com:13333"
KEY_ID = os.environ.get("SOLIS_KEY_ID")
KEY_SECRET = os.environ.get("SOLIS_KEY_SECRET")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))


class SolisError(RuntimeError):
    pass


class SolisCloudClient:
    """Each request must be signed with HMAC-SHA1.

    StringToSign format:
        VERB + "\n" + Content-MD5 + "\n" + Content-Type + "\n" + Date + "\n" + Resource

    Where Resource is the path including any query string, and the
    Authorization header is "API <KeyId>:<base64(HMAC-SHA1(StringToSign, KeySecret))>".
    """

    def __init__(self, key_id: str, key_secret: str):
        if not key_id or not key_secret:
            raise SolisError("SOLIS_KEY_ID / SOLIS_KEY_SECRET not set")
        self.key_id = key_id
        self.key_secret = key_secret.encode("utf-8")
        self.session = requests.Session()

    def _sign(self, verb: str, path: str, body_bytes: bytes,
                content_type: str) -> dict[str, str]:
        """Build the headers for one signed request."""
        body_md5 = base64.b64encode(hashlib.md5(body_bytes).digest()).decode("ascii")
        date_header = formatdate(timeval=time.time(), localtime=False, usegmt=True)
        # Resource = path + query string (none in our calls)
        resource = urlparse(path).path
        string_to_sign = "\n".join([verb, body_md5, content_type, date_header, resource])
        signature = base64.b64encode(
            hmac.new(self.key_secret, string_to_sign.encode("utf-8"), hashlib.sha1).digest()
        ).decode("ascii")
        return {
            "Content-MD5":   body_md5,
            "Content-Type":  content_type,
            "Date":          date_header,
            "Authorization": f"API {self.key_id}:{signature}",
        }

    def _post(self, path: str, body: dict) -> dict:
        body_bytes = json.dumps(body).encode("utf-8")
        content_type = "application/json;charset=UTF-8"
        headers = self._sign("POST", path, body_bytes, content_type)
        url = f"{BASE_URL}{path}"
        r = self.session.post(url, data=body_bytes, headers=headers,
                                timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        out = r.json()
        # SolisCloud returns {"success":true, "data":..., "code":"0", "msg":"success"}
        if out.get("success") is False or str(out.get("code")) not in ("0", "1"):
            raise SolisError(f"{path} failed (code {out.get('code')}): {out.get('msg')}")
        return out

    def list_stations(self) -> list[dict]:
        data = self._post("/v1/api/userStationList", {"pageNo": 1, "pageSize": 100})
        body = data.get("data") or {}
        return body.get("page", {}).get("records") or body.get("records") or []

    def station_detail(self, station_id) -> dict:
        data = self._post("/v1/api/stationDetail", {"id": str(station_id)})
        return data.get("data") or {}

    def station_day_energy(self, station_id, date_str: str) -> dict:
        """date_str format: 'YYYY-MM-DD'. Returns hourly energy for the day."""
        data = self._post("/v1/api/stationDayEnergyList",
                           {"id": str(station_id), "money": "ZAR",
                            "time": date_str, "timeZone": 2})
        return data.get("data") or {}

    def station_month_energy(self, station_id, month_str: str) -> dict:
        """month_str format: 'YYYY-MM'. Daily totals for the month."""
        data = self._post("/v1/api/stationMonthEnergyList",
                           {"id": str(station_id), "money": "ZAR",
                            "month": month_str})
        return data.get("data") or {}

    def station_year_energy(self, station_id, year_str: str) -> dict:
        """year_str format: 'YYYY'. Monthly totals for the year."""
        data = self._post("/v1/api/stationYearEnergyList",
                           {"id": str(station_id), "money": "ZAR",
                            "year": year_str})
        return data.get("data") or {}


def load_site_configs() -> list[tuple[Path, dict]]:
    out = []
    if not SITES_DIR.exists(): return out
    for d in sorted(SITES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"): continue
        cfg_path = d / "config.json"
        if not cfg_path.exists(): continue
        try: cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  SKIP {d.name}: bad JSON - {e}"); continue
        if not cfg.get("station_id"):
            print(f"  SKIP {d.name}: no station_id"); continue
        out.append((d, cfg))
    return out


def run_discover() -> None:
    client = SolisCloudClient(KEY_ID, KEY_SECRET)
    stations = client.list_stations()
    print(f"\nFound {len(stations)} station(s):\n")
    print(f"  {'NAME':<40} {'STATION ID':<20} CAPACITY")
    print(f"  {'-' * 40} {'-' * 20} {'-' * 10}")
    for s in stations:
        name = (s.get("stationName") or "(unnamed)")[:39]
        sid = s.get("id", "?")
        cap = f"{s.get('capacity', 0):.1f} kWp"
        print(f"  {name:<40} {str(sid):<20} {cap}")
    out_path = PLATFORM_DIR / "soliscloud_discovered.json"
    out_path.write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved to {out_path.name}")


def run_fetch() -> None:
    import processor

    sites = load_site_configs()
    if not sites:
        print("No SolisCloud sites configured. Create config.json under "
              "platforms/soliscloud/sites/<slug>/ first.")
        return

    print(f"Fetching {len(sites)} SolisCloud site(s)...")
    client = SolisCloudClient(KEY_ID, KEY_SECRET)
    now = datetime.now(tz=SAST)
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    year_str  = now.strftime("%Y")

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        station_id = config["station_id"]
        try:
            detail = client.station_detail(station_id)
            today  = client.station_day_energy(station_id, today_str)
            month  = client.station_month_energy(station_id, month_str)
            year   = client.station_year_energy(station_id, year_str)
        except SolisError as exc:
            print(f"  FAIL {sid}: {exc}"); skipped += 1; continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}"); skipped += 1; continue

        try:
            processor.write_site(site_dir, config,
                                  detail=detail, today=today,
                                  month=month, year=year)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1
        time.sleep(0.5)

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    try:
        if "--discover" in sys.argv: run_discover()
        else: run_fetch()
    except SolisError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
