"""
MyPower24 (SolarMD) API fetch
=============================
Pulls energy data from the SolarMD V3 REST API for every configured
logger. Clean bearer-token API - no scraping, no IP whitelist, no DNS games.

Auth flow:
  POST /api/v3/auth  (username + password as form-urlencoded)
    -> { "data": { "token": "<JWT>" } }
  The JWT payload contains the list of loggers the account can access
  AND an 'exp' expiry, so we cache it and re-auth only when expired.

Endpoints used:
  POST /api/v3/auth                                   - login
  GET  /api/v3/loggers/{serial}/metrics/energy        - 15-min load/grid/pv kWh
  GET  /api/v3/loggers/{serial}/variables             - live values incl. SOC

Run normally:
    python fetch.py

Discover loggers (decodes the JWT - no extra API call):
    python fetch.py --discover

Required env vars (GitHub Secrets):
    MYPOWER24_USERNAME   myPower24 account username
    MYPOWER24_PASSWORD   myPower24 account password
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

# processor imported lazily inside run_fetch() so --discover runs standalone.

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

BASE_URL = (os.environ.get("MYPOWER24_BASE_URL")
             or "https://login.mypower24.co.za/SolarMDApi")
USERNAME = os.environ.get("MYPOWER24_USERNAME")
PASSWORD = os.environ.get("MYPOWER24_PASSWORD")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))
TOKEN_CACHE = PLATFORM_DIR / ".mp24_token.json"   # gitignored


class MyPower24Error(RuntimeError):
    pass


def _decode_jwt_payload(token: str) -> dict:
    """Decode a JWT's payload section (middle part) without verifying the
    signature - we only need to read the loggers list and expiry, which are
    not secret. The server still validates the signature on every call."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)      # pad to multiple of 4
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError) as e:
        raise MyPower24Error(f"Could not decode JWT payload: {e}")


class MyPower24Client:
    def __init__(self, username: str, password: str):
        if not username or not password:
            raise MyPower24Error(
                "MYPOWER24_USERNAME / MYPOWER24_PASSWORD not set")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.token: str | None = None
        self.token_exp = 0.0
        self.loggers: list[str] = []

    def login(self) -> str:
        # Use cached token if still valid (with 5 min safety margin)
        if self.token_exp == 0.0 and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                if cached.get("exp", 0) - time.time() > 300:
                    self.token = cached["token"]
                    self.token_exp = cached["exp"]
                    self.loggers = cached.get("loggers", [])
                    mins = int((self.token_exp - time.time()) / 60)
                    print(f"  Using cached MyPower24 token (expires in {mins}m)")
                    return self.token
            except (json.JSONDecodeError, KeyError):
                pass

        url = f"{BASE_URL}/api/v3/auth"
        # Credentials are form-urlencoded, NOT JSON
        resp = self.session.post(
            url,
            data={"username": self.username, "password": self.password},
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code != 200:
            raise MyPower24Error(f"Auth HTTP {resp.status_code}: {resp.text[:200]}")
        body = resp.json()
        if not body.get("success") or not body.get("data", {}).get("token"):
            raise MyPower24Error(f"Auth failed: {body.get('message', body)}")
        self.token = body["data"]["token"]

        # Decode the JWT to get loggers + expiry
        payload = _decode_jwt_payload(self.token)
        self.loggers = payload.get("loggers", [])
        self.token_exp = float(payload.get("exp", time.time() + 3600))

        try:
            TOKEN_CACHE.write_text(json.dumps({
                "token": self.token,
                "exp": self.token_exp,
                "loggers": self.loggers,
            }))
        except OSError:
            pass
        exp_dt = datetime.fromtimestamp(self.token_exp, tz=SAST)
        print(f"  Logged in to MyPower24 - {len(self.loggers)} logger(s), "
              f"token valid until {exp_dt.strftime('%Y-%m-%d %H:%M')}")
        return self.token

    def _headers(self) -> dict:
        if not self.token:
            self.login()
        return {"Authorization": f"Bearer {self.token}"}

    def _get(self, path: str, params: dict | None = None,
              _retried: bool = False) -> dict:
        url = f"{BASE_URL}{path}"
        r = self.session.get(url, headers=self._headers(), params=params,
                              timeout=REQUEST_TIMEOUT)
        if r.status_code in (401, 403) and not _retried:
            print("  Token rejected, re-authenticating...")
            self.token = None
            self.token_exp = 0
            if TOKEN_CACHE.exists():
                TOKEN_CACHE.unlink()
            self.login()
            return self._get(path, params, _retried=True)
        r.raise_for_status()
        body = r.json()
        if not body.get("success", True):
            raise MyPower24Error(f"{path} failed: {body.get('message', body)}")
        return body

    def energy_metrics(self, serial: str, start_iso: str, end_iso: str,
                        period: str = "PT15M") -> list[dict]:
        """15-min load/grid/pv energy over a time range."""
        body = self._get(
            f"/api/v3/loggers/{serial}/metrics/energy",
            params={"start": start_iso, "end": end_iso, "period": period},
        )
        return body.get("data") or []

    def variables(self, serial: str) -> list[dict]:
        """Live device + user variables (SOC, grid power, controls, etc.)."""
        body = self._get(f"/api/v3/loggers/{serial}/variables")
        return body.get("data") or []


# ---------------------------------------------------------------------------
# Site config loading
# ---------------------------------------------------------------------------

def load_site_configs() -> list[tuple[Path, dict]]:
    out = []
    if not SITES_DIR.exists():
        return out
    for d in sorted(SITES_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        cfg_path = d / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  SKIP {d.name}: bad JSON - {e}")
            continue
        if not cfg.get("serial"):
            print(f"  SKIP {d.name}: no serial")
            continue
        out.append((d, cfg))
    return out


# ---------------------------------------------------------------------------
# Date windows (UTC, since the API recommends UTC timestamps)
# ---------------------------------------------------------------------------

def _today_range_utc() -> tuple[str, str]:
    """Today in SAST, expressed as UTC ISO strings the API expects."""
    now_sast = datetime.now(tz=SAST)
    start_sast = now_sast.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_sast.astimezone(timezone.utc)
    end_utc = now_sast.astimezone(timezone.utc)
    return (start_utc.strftime("%Y-%m-%dT%H:%M"),
            end_utc.strftime("%Y-%m-%dT%H:%M"))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_discover() -> None:
    """Decode the JWT to list loggers - no dedicated list endpoint needed."""
    client = MyPower24Client(USERNAME, PASSWORD)
    client.login()
    print(f"\nFound {len(client.loggers)} logger(s) on this account:\n")
    for serial in client.loggers:
        print(f"  {serial}")
    out_path = PLATFORM_DIR / "mypower24_discovered.json"
    out_path.write_text(json.dumps(client.loggers, indent=2) + "\n",
                          encoding="utf-8")
    print(f"\nSaved to {out_path.name}")
    print("Create a sites/<slug>/config.json for each logger you want to monitor.")


def run_fetch() -> None:
    import processor

    sites = load_site_configs()
    if not sites:
        print("No MyPower24 sites configured. Create config.json files under "
              "platforms/mypower24/sites/<slug>/ and run again. Use `python "
              "fetch.py --discover` to list available loggers first.")
        return

    print(f"Fetching {len(sites)} MyPower24 site(s)...")
    client = MyPower24Client(USERNAME, PASSWORD)
    client.login()

    start_iso, end_iso = _today_range_utc()

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        serial = config["serial"]
        try:
            energy = client.energy_metrics(serial, start_iso, end_iso, "PT15M")
            try:
                variables = client.variables(serial)
            except Exception:
                variables = []        # variables are a bonus, not required
        except MyPower24Error as exc:
            print(f"  FAIL {sid}: {exc}"); skipped += 1; continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}"); skipped += 1; continue

        try:
            processor.write_site(site_dir, config,
                                  energy=energy, variables=variables)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1
        time.sleep(0.3)

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    try:
        if "--discover" in sys.argv:
            run_discover()
        else:
            run_fetch()
    except MyPower24Error as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
