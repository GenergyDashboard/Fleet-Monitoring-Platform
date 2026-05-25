"""
Sungrow iSolarCloud API fetch
=============================
Pulls energy data from the Sungrow iSolarCloud OpenAPI. Several regional
gateways exist - the SA installations typically use the Asia gateway at
gateway.isolarcloud.com.hk.

Setup requires a Sungrow developer application (approval takes days). See
README.md for the full flow.

Run normally:
    python fetch.py

Discover stations:
    python fetch.py --discover

Required env vars (set as GitHub Secrets):
    SUNGROW_APPKEY        AppKey from developer.isolarcloud.com.hk
    SUNGROW_SECRETKEY     SecretKey from same
    SUNGROW_ACCESS_KEY    AccessKey (third value) from same
    SUNGROW_USERNAME      Your iSolarCloud account user
    SUNGROW_PASSWORD      Your iSolarCloud password (raw)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

BASE_URL = (os.environ.get("SUNGROW_BASE_URL")
             or "https://gateway.isolarcloud.com.hk")
APPKEY = os.environ.get("SUNGROW_APPKEY")
SECRETKEY = os.environ.get("SUNGROW_SECRETKEY")
ACCESS_KEY = os.environ.get("SUNGROW_ACCESS_KEY")
USERNAME = os.environ.get("SUNGROW_USERNAME")
PASSWORD = os.environ.get("SUNGROW_PASSWORD")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))
TOKEN_CACHE = PLATFORM_DIR / ".sg2_token.json"   # gitignored


class SungrowError(RuntimeError):
    pass


class SungrowClient:
    """iSolarCloud OpenAPI client.

    Auth: POST /openapi/login with credentials in body and headers
    containing x-access-key + sys_code. Returns a token used for
    subsequent calls.

    The exact endpoint set varies by region; the V1 (no OAuth2)
    flow used here is what jsanchezdelvillar/Sungrow-API documents
    for Home Assistant integration. If your account has been
    configured for OAuth2, swap the auth path.
    """

    def __init__(self):
        for arg, name in [(APPKEY, "SUNGROW_APPKEY"),
                          (SECRETKEY, "SUNGROW_SECRETKEY"),
                          (ACCESS_KEY, "SUNGROW_ACCESS_KEY"),
                          (USERNAME, "SUNGROW_USERNAME"),
                          (PASSWORD, "SUNGROW_PASSWORD")]:
            if not arg:
                raise SungrowError(f"{name} environment variable not set")
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
            "x-access-key": ACCESS_KEY,
            "sys_code": "901",
        })
        self.token: str | None = None
        self.user_id: str | None = None
        self.token_expires_at = 0.0

    def login(self) -> str:
        if self.token_expires_at == 0.0 and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                if (cached.get("expires_at", 0) - time.time()) > 600:
                    self.token = cached["token"]
                    self.user_id = cached.get("user_id")
                    self.token_expires_at = cached["expires_at"]
                    print("  Using cached Sungrow token")
                    return self.token
            except (json.JSONDecodeError, KeyError):
                pass

        url = f"{BASE_URL}/openapi/login"
        body = {
            "appkey": APPKEY,
            "user_account": USERNAME,
            "user_password": PASSWORD,
            "login_type": "1",
        }
        r = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise SungrowError(f"Login HTTP {r.status_code}: {r.text[:200]}")
        body = r.json()
        if body.get("result_code") not in ("1", 1, "0", 0):
            raise SungrowError(f"Login failed: {body.get('result_msg', body)}")
        result = body.get("result_data") or {}
        self.token = result.get("token") or result.get("access_token")
        self.user_id = result.get("user_id") or result.get("userId")
        if not self.token:
            raise SungrowError(f"Login response missing token: {body}")
        # Token doesn't always include expiry; assume 4 hours
        self.token_expires_at = time.time() + 4 * 3600
        try:
            TOKEN_CACHE.write_text(json.dumps({"token": self.token,
                                                "user_id": self.user_id,
                                                "expires_at": self.token_expires_at}))
        except OSError:
            pass
        print(f"  Logged in to Sungrow iSolarCloud")
        return self.token

    def _post(self, path: str, body: dict, _retried: bool = False) -> dict:
        if not self.token: self.login()
        url = f"{BASE_URL}{path}"
        body = {**body, "token": self.token, "appkey": APPKEY}
        r = self.session.post(url, json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code == 401 and not _retried:
            print("  Token rejected, re-authenticating...")
            self.token = None; self.token_expires_at = 0
            if TOKEN_CACHE.exists(): TOKEN_CACHE.unlink()
            self.login()
            return self._post(path, body, _retried=True)
        r.raise_for_status()
        out = r.json()
        if out.get("result_code") not in ("1", 1, "0", 0):
            raise SungrowError(f"{path} failed: {out.get('result_msg', out)}")
        return out.get("result_data") or {}

    def list_stations(self) -> list[dict]:
        body = {"curPage": 1, "size": 100}
        data = self._post("/openapi/getPowerStationList", body)
        return data.get("pageList") or data.get("list") or []

    def station_realtime(self, ps_id) -> dict:
        return self._post("/openapi/getPowerStationDetail", {"ps_id": ps_id})

    def station_energy(self, ps_id, date_type: str, date_str: str) -> dict:
        """date_type: 1=day(5min), 2=month(daily), 3=year(monthly), 4=lifetime(yearly)"""
        return self._post("/openapi/queryPowerStationData", {
            "ps_id": ps_id,
            "date_type": date_type,
            "date_id": date_str,
        })


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
        if not cfg.get("ps_id"):
            print(f"  SKIP {d.name}: no ps_id"); continue
        out.append((d, cfg))
    return out


def run_discover() -> None:
    client = SungrowClient()
    stations = client.list_stations()
    print(f"\nFound {len(stations)} station(s):\n")
    for s in stations:
        name = (s.get("ps_name") or "(unnamed)")[:39]
        ps_id = s.get("ps_id", "?")
        print(f"  {name:<40} ps_id={ps_id}")
    out_path = PLATFORM_DIR / "sungrow_discovered.json"
    out_path.write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved to {out_path.name}")


def run_fetch() -> None:
    import processor

    sites = load_site_configs()
    if not sites:
        print("No Sungrow sites configured."); return

    print(f"Fetching {len(sites)} Sungrow site(s)...")
    client = SungrowClient()
    client.login()
    now = datetime.now(tz=SAST)
    today_str = now.strftime("%Y%m%d")
    month_str = now.strftime("%Y%m")
    year_str  = now.strftime("%Y")

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        ps_id = config["ps_id"]
        try:
            realtime = client.station_realtime(ps_id)
            today    = client.station_energy(ps_id, "1", today_str)
            month    = client.station_energy(ps_id, "2", month_str)
            year     = client.station_energy(ps_id, "3", year_str)
            lifetime = client.station_energy(ps_id, "4", year_str)
        except SungrowError as exc:
            print(f"  FAIL {sid}: {exc}"); skipped += 1; continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}"); skipped += 1; continue

        try:
            processor.write_site(site_dir, config,
                                  realtime=realtime, today=today,
                                  month=month, year=year, lifetime=lifetime)
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
    except SungrowError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
