"""
Sigenergy (SigenCloud) API fetch
================================
Pulls energy data from the Sigenergy openapi at api-eu.sigencloud.com.
Auth: base64(AppKey:AppSecret) → bearer token (~12hr expiry).

Run normally:
    python fetch.py

Discover stations:
    python fetch.py --discover

Required env vars (set as GitHub Secrets):
    SIGEN_APPKEY      AppKey    from developer.sigencloud.com
    SIGEN_APPSECRET   AppSecret from developer.sigencloud.com

Rate limit: ~10 requests/minute for third-party accounts. Energy flow data
updates once per 5 min per station - so polling more often than that is
wasted effort.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

# Default to EU endpoint; SA installations also use this region.
BASE_URL = os.environ.get("SIGEN_BASE_URL") or "https://api-eu.sigencloud.com"
APPKEY = os.environ.get("SIGEN_APPKEY")
APPSECRET = os.environ.get("SIGEN_APPSECRET")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))
TOKEN_CACHE = PLATFORM_DIR / ".sg_token.json"   # gitignored


class SigenError(RuntimeError):
    pass


class SigenClient:
    """SigenCloud openapi client.

    Auth flow:
      POST /openapi/auth/login/key
      Body: {"key": "<base64(AppKey:AppSecret)>"}
      Response: {"code":0,"data":"<JSON-encoded-string>"}
      Inside data (parsed as JSON):
        {"tokenType":"Bearer","accessToken":"...","expiresIn":43199}
    """

    def __init__(self, appkey: str, appsecret: str):
        if not appkey or not appsecret:
            raise SigenError("SIGEN_APPKEY / SIGEN_APPSECRET not set")
        self.appkey = appkey
        self.appsecret = appsecret
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.token: str | None = None
        self.token_expires_at = 0.0
        self._last_call = 0.0       # for rate limiting

    def login(self) -> str:
        if self.token_expires_at == 0.0 and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                if (cached.get("expires_at", 0) - time.time()) > 600:
                    self.token = cached["token"]
                    self.token_expires_at = cached["expires_at"]
                    mins = int((self.token_expires_at - time.time()) / 60)
                    print(f"  Using cached Sigenergy token (expires in {mins}m)")
                    return self.token
            except (json.JSONDecodeError, KeyError):
                pass

        key_b64 = base64.b64encode(
            f"{self.appkey}:{self.appsecret}".encode("utf-8")
        ).decode("ascii")
        url = f"{BASE_URL}/openapi/auth/login/key"
        r = self.session.post(url, json={"key": key_b64},
                                timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise SigenError(f"Login HTTP {r.status_code}: {r.text[:200]}")
        outer = r.json()
        if outer.get("code") != 0:
            raise SigenError(f"Login failed: {outer.get('msg', outer)}")
        # data is a JSON STRING that must be parsed
        raw = outer.get("data")
        if isinstance(raw, str):
            inner = json.loads(raw)
        else:
            inner = raw or {}
        self.token = inner.get("accessToken")
        if not self.token:
            raise SigenError(f"Login response missing accessToken: {outer}")
        self.token_expires_at = time.time() + (inner.get("expiresIn") or 43199)
        try:
            TOKEN_CACHE.write_text(json.dumps({"token": self.token,
                                                "expires_at": self.token_expires_at}))
        except OSError:
            pass
        print(f"  Logged in to Sigenergy - token valid "
              f"{int((self.token_expires_at - time.time()) / 3600)}h")
        return self.token

    def _throttle(self) -> None:
        """Respect 10 req/min limit - 6s between calls."""
        elapsed = time.time() - self._last_call
        if elapsed < 6:
            time.sleep(6 - elapsed)
        self._last_call = time.time()

    def _get(self, path: str, params: dict | None = None,
              _retried: bool = False) -> dict:
        if not self.token: self.login()
        self._throttle()
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        r = self.session.get(url, headers=headers, params=params,
                              timeout=REQUEST_TIMEOUT)
        if r.status_code == 401 and not _retried:
            print("  Token rejected, re-authenticating...")
            self.token = None; self.token_expires_at = 0
            if TOKEN_CACHE.exists(): TOKEN_CACHE.unlink()
            self.login()
            return self._get(path, params, _retried=True)
        r.raise_for_status()
        body = r.json()
        if body.get("code") not in (0, "0"):
            raise SigenError(f"{path} failed: {body.get('msg', body)}")
        return body.get("data") or body

    # Endpoint paths follow Sigenergy's documented openapi naming.
    # If any path 404s, check developer.sigencloud.com for the latest.
    def list_stations(self) -> list[dict]:
        body = self._get("/openapi/station/list", params={"page": 1, "size": 100})
        return body.get("records") or body.get("list") or body or []

    def station_overview(self, station_id) -> dict:
        return self._get(f"/openapi/station/{station_id}/overview")

    def station_statistics(self, station_id, period: str, date_str: str) -> dict:
        """period: 'day'|'month'|'year'|'lifetime'"""
        return self._get(f"/openapi/station/{station_id}/statistics",
                          params={"period": period, "date": date_str})


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
    client = SigenClient(APPKEY, APPSECRET)
    client.login()
    stations = client.list_stations()
    print(f"\nFound {len(stations)} station(s):\n")
    for s in stations:
        name = (s.get("name") or s.get("stationName") or "(unnamed)")[:39]
        sid = s.get("id") or s.get("stationId", "?")
        print(f"  {name:<40} {sid}")
    out_path = PLATFORM_DIR / "sigenergy_discovered.json"
    out_path.write_text(json.dumps(stations, indent=2) + "\n", encoding="utf-8")
    print(f"\nSaved to {out_path.name}")


def run_fetch() -> None:
    import processor

    sites = load_site_configs()
    if not sites:
        print("No Sigenergy sites configured."); return

    print(f"Fetching {len(sites)} Sigenergy site(s)...")
    client = SigenClient(APPKEY, APPSECRET)
    client.login()
    now = datetime.now(tz=SAST)
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    year_str  = now.strftime("%Y")

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        station_id = config["station_id"]
        try:
            overview = client.station_overview(station_id)
            today    = client.station_statistics(station_id, "day", today_str)
            month    = client.station_statistics(station_id, "month", month_str)
            year     = client.station_statistics(station_id, "year", year_str)
            lifetime = client.station_statistics(station_id, "lifetime", year_str)
        except SigenError as exc:
            print(f"  FAIL {sid}: {exc}"); skipped += 1; continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}"); skipped += 1; continue

        try:
            processor.write_site(site_dir, config,
                                  overview=overview, today=today,
                                  month=month, year=year, lifetime=lifetime)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    try:
        if "--discover" in sys.argv: run_discover()
        else: run_fetch()
    except SigenError as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
