"""
Sunsynk API fetch
=================
Pulls energy data for every configured Sunsynk site from the Sunsynk Connect
API (api.sunsynk.net).

Auth flow (more complex than other platforms):
  1. GET  /anonymous/publicKey  -- fetch an RSA public key, signed with
                                   a nonce + MD5 signature
  2. POST /oauth/token/new      -- send username + RSA-encrypted password
                                   + nonce + MD5 sign, get bearer token

This matches what the jamesridgway/sunsynk-api-client library does for
Sunsynk's post-Nov-2025 API. The old /oauth/token endpoint returns 404
and /api/v1/oauth/token returns 403 - the new endpoint is the only path.

Run normally:
    python fetch.py

Discover sites visible to your account:
    python fetch.py --discover

Required env vars (set as GitHub Secrets):
    SUNSYNK_USERNAME   sunsynk.net account email
    SUNSYNK_PASSWORD   sunsynk.net account password (raw)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except ImportError:
    print("ERROR: The 'cryptography' package is required for Sunsynk auth.",
          file=sys.stderr)
    print("Install it with: pip install cryptography", file=sys.stderr)
    sys.exit(1)

# processor imported lazily inside run_fetch() so --discover runs standalone.

PLATFORM_DIR = Path(__file__).resolve().parent
SITES_DIR = PLATFORM_DIR / "sites"

BASE_URL = os.environ.get("SUNSYNK_BASE_URL") or "https://api.sunsynk.net"
USERNAME = os.environ.get("SUNSYNK_USERNAME")
PASSWORD = os.environ.get("SUNSYNK_PASSWORD")

REQUEST_TIMEOUT = 30
SAST = timezone(timedelta(hours=2))
TOKEN_CACHE = PLATFORM_DIR / ".ss_token.json"   # gitignored


class SunsynkError(RuntimeError):
    pass


class SunsynkClient:
    """Sunsynk Connect REST client.

    Auth: POST /oauth/token with username/password, returns a bearer token
    valid for ~24 hours. Cached to disk to survive workflow runs.
    """

    def __init__(self, username: str, password: str):
        if not username or not password:
            raise SunsynkError("SUNSYNK_USERNAME / SUNSYNK_PASSWORD not set")
        self.username = username
        self.password = password
        self.session = requests.Session()
        # The Sunsynk API rejects bare python-requests calls with 403.
        # These headers match what sunsynk.net's web client sends.
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://www.sunsynk.net",
            "Referer": "https://www.sunsynk.net/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        })
        self.token: str | None = None
        self.token_expires_at = 0.0

    _SOURCE = "sunsynk"
    _CLIENT_ID = "csp-web"

    def login(self) -> str:
        # Cached?
        if self.token_expires_at == 0.0 and TOKEN_CACHE.exists():
            try:
                cached = json.loads(TOKEN_CACHE.read_text())
                if (cached.get("expires_at", 0) - time.time()) > 600:
                    self.token = cached["token"]
                    self.token_expires_at = cached["expires_at"]
                    mins = int((self.token_expires_at - time.time()) / 60)
                    print(f"  Using cached Sunsynk token (expires in {mins}m)")
                    return self.token
            except (json.JSONDecodeError, KeyError):
                pass

        # Step 1: fetch the RSA public key.
        # This endpoint is itself signed with a nonce + MD5 hash to discourage
        # casual scraping. The signature scheme is documented in the
        # jamesridgway/sunsynk-api-client library.
        raw_key = self._fetch_public_key()

        # Step 2: RSA-encrypt the password with the fetched public key,
        # using PKCS1v15 padding (NOT OAEP).
        encrypted_password = self._rsa_encrypt_pkcs1v15(raw_key, self.password)

        # Step 3: POST credentials to /oauth/token/new with another
        # nonce + MD5 signature.
        login_nonce = self._make_nonce()
        login_sign = self._md5_hex(
            f"nonce={login_nonce}&source={self._SOURCE}{raw_key[:10]}"
        )
        payload = {
            "username": self.username,
            "password": encrypted_password,
            "grant_type": "password",
            "client_id": self._CLIENT_ID,
            "source": self._SOURCE,
            "nonce": login_nonce,
            "sign": login_sign,
        }
        url = f"{BASE_URL}/oauth/token/new"
        r = self.session.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise SunsynkError(f"Login HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if not body.get("success"):
            raise SunsynkError(f"Login failed: {body.get('msg', body)}")
        data = body.get("data") or {}
        self.token = data.get("access_token")
        if not self.token:
            raise SunsynkError(f"Login response missing access_token: {body}")
        # Sunsynk tokens don't always include expires_in - assume 24h
        self.token_expires_at = time.time() + (data.get("expires_in") or 86400)
        try:
            TOKEN_CACHE.write_text(json.dumps({
                "token": self.token,
                "expires_at": self.token_expires_at,
            }))
        except OSError:
            pass
        print(f"  Logged in to Sunsynk - token valid "
              f"{int((self.token_expires_at - time.time()) / 3600)}h")
        return self.token

    def _fetch_public_key(self) -> str:
        """Get the RSA public key Sunsynk uses to encrypt the password.

        Sunsynk signs this anonymous call with nonce + MD5 of
        'nonce={n}&source=sunsynkPOWER_VIEW'. The fixed 'POWER_VIEW' suffix
        seems to be the magic string the server expects for the publicKey
        endpoint specifically (different from login's signing).
        """
        nonce = self._make_nonce()
        sign = self._md5_hex(f"nonce={nonce}&source={self._SOURCE}POWER_VIEW")
        url = (f"{BASE_URL}/anonymous/publicKey"
               f"?nonce={nonce}&source={self._SOURCE}&sign={sign}")
        r = self.session.get(url, timeout=REQUEST_TIMEOUT)
        if r.status_code != 200:
            raise SunsynkError(f"publicKey HTTP {r.status_code}: {r.text[:300]}")
        body = r.json()
        if not body.get("success") or not body.get("data"):
            raise SunsynkError(f"publicKey response invalid: {body}")
        return body["data"]

    @staticmethod
    def _make_nonce() -> int:
        """Milliseconds since epoch."""
        return int(time.time() * 1000)

    @staticmethod
    def _md5_hex(value: str) -> str:
        return hashlib.md5(value.encode()).hexdigest()

    @staticmethod
    def _rsa_encrypt_pkcs1v15(raw_key: str, plaintext: str) -> str:
        """RSA-PKCS1v15 encrypt the password and return base64."""
        pem = (f"-----BEGIN PUBLIC KEY-----\n"
                f"{raw_key}\n"
                f"-----END PUBLIC KEY-----").encode()
        public_key = serialization.load_pem_public_key(pem)
        ciphertext = public_key.encrypt(plaintext.encode(),
                                          padding.PKCS1v15())
        return base64.b64encode(ciphertext).decode()

    def _get(self, path: str, params: dict | None = None,
              _retried: bool = False) -> dict:
        if not self.token:
            self.login()
        url = f"{BASE_URL}{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        r = self.session.get(url, headers=headers, params=params,
                              timeout=REQUEST_TIMEOUT)
        if r.status_code == 401 and not _retried:
            print("  Token rejected, re-authenticating...")
            self.token = None
            self.token_expires_at = 0
            if TOKEN_CACHE.exists():
                TOKEN_CACHE.unlink()
            self.login()
            return self._get(path, params, _retried=True)
        r.raise_for_status()
        return r.json()

    def list_plants(self) -> list[dict]:
        """List plants visible to the account.

        Sunsynk has two different relevant collections:
          /api/v1/plants    - "plants" the user owns/manages
          /api/v1/inverters - individual inverter devices, possibly without
                               a plant grouping

        For installer-level accounts that monitor sites for end-customers,
        sometimes only the inverters endpoint returns data. So if plants is
        empty, fall back to inverters and synthesize plant-like records
        from them.

        Empty `status=` (not `status=-1`) matches what the upstream library
        uses, and what the sunsynk.net web app sends.
        """
        # Paginate through plants - some accounts have many
        plants = []
        page = 1
        while True:
            body = self._get("/api/v1/plants",
                              params={"page": page, "limit": 50,
                                      "name": "", "status": ""})
            data = body.get("data") or {}
            page_plants = data.get("infos") or data.get("list") or []
            if not page_plants:
                break
            plants.extend(page_plants)
            total = data.get("total", 0)
            if len(plants) >= total:
                break
            page += 1
            if page > 20:        # safety cap
                break

        if plants:
            return plants

        # Plants is empty - try inverters and synthesize plant-like entries
        print("  /api/v1/plants returned 0 - trying /api/v1/inverters fallback")
        body = self._get("/api/v1/inverters",
                          params={"page": 1, "limit": 50, "total": 0,
                                  "status": -1, "sn": "", "plantId": "",
                                  "type": -2, "softVer": "", "hmiVer": "",
                                  "agentCompanyId": -1, "gsn": ""})
        data = body.get("data") or {}
        inverters = data.get("infos") or data.get("list") or []
        # Map inverter records to plant-shaped objects so the rest of the
        # code can treat them uniformly. Each inverter becomes a "plant"
        # keyed by its serial number.
        synthesized = []
        for inv in inverters:
            synthesized.append({
                "id":     inv.get("plantId") or inv.get("pid") or inv.get("sn"),
                "name":   inv.get("plantName") or inv.get("alias") or inv.get("sn"),
                "status": inv.get("status"),
                "sn":     inv.get("sn"),
                "_from":  "inverters_endpoint",
                **inv,
            })
        return synthesized

    def plant_realtime(self, plant_id: int) -> dict:
        body = self._get(f"/api/v1/plant/{plant_id}/realtime", params={"id": plant_id})
        return body.get("data") or {}

    def plant_energy(self, plant_id: int, period: str,
                       date_str: str) -> dict:
        """period: 'day' | 'month' | 'year' | 'total'."""
        params = {"lan": "en"}
        if period != "total":
            params["date"] = date_str
        body = self._get(f"/api/v1/plant/energy/{plant_id}/{period}",
                          params=params)
        return body.get("data") or {}

    def plant_inverters(self, plant_id: int) -> list:
        """Get inverter serial numbers for a plant."""
        try:
            body = self._get(f"/api/v1/plant/{plant_id}/inverters",
                              params={"page": 1, "limit": 10})
            return body.get("data", {}).get("infos", [])
        except Exception:
            return []

    def inverter_output(self, sn: str, date_str: str) -> dict:
        """Get inverter grid output for a date — hourly records."""
        try:
            body = self._get(f"/api/v1/inverter/grid/{sn}/output",
                              params={"date": date_str})
            return body.get("data") or {}
        except Exception:
            return {}

    def inverter_input(self, sn: str, date_str: str) -> dict:
        """Get inverter input (PV) for a date — hourly records."""
        try:
            body = self._get(f"/api/v1/inverter/{sn}/input",
                              params={"date": date_str})
            return body.get("data") or {}
        except Exception:
            return {}


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
        if not cfg.get("plant_id"):
            print(f"  SKIP {site_dir.name}: no plant_id in config.json")
            continue
        out.append((site_dir, cfg))
    return out


def run_discover() -> None:
    client = SunsynkClient(USERNAME, PASSWORD)
    plants = client.list_plants()
    print(f"\nFound {len(plants)} plant(s):\n")
    print(f"  {'NAME':<35} {'PLANT ID':<12} {'SN':<16} STATUS")
    print(f"  {'-' * 35} {'-' * 12} {'-' * 16} {'-' * 10}")
    for p in plants:
        name = (p.get("name") or p.get("plantName") or "(unnamed)")[:34]
        pid = p.get("id") or p.get("plantId") or "?"
        sn = p.get("sn", "")[:15] if p.get("sn") else ""
        status = p.get("status", "?")
        print(f"  {name:<35} {str(pid):<12} {sn:<16} {status}")
    out_path = PLATFORM_DIR / "sunsynk_discovered.json"
    out_path.write_text(json.dumps(plants, indent=2) + "\n", encoding="utf-8")
    print(f"\nRaw discovery saved to {out_path.name}")
    if plants and plants[0].get("_from") == "inverters_endpoint":
        print("\nNOTE: Plants endpoint returned empty - results came from "
              "inverters fallback. Each 'plant' here is actually one inverter "
              "device; the 'id' field is the inverter's serial number.")


def run_fetch() -> None:
    import processor

    sites = load_site_configs()
    if not sites:
        print("No Sunsynk sites configured. Create config.json under "
              "platforms/sunsynk/sites/<slug>/ first. Use `python fetch.py "
              "--discover` to list available plants.")
        return

    print(f"Fetching {len(sites)} Sunsynk site(s)...")
    client = SunsynkClient(USERNAME, PASSWORD)
    client.login()

    now = datetime.now(tz=SAST)
    today_str = now.strftime("%Y-%m-%d")
    month_str = now.strftime("%Y-%m")
    year_str  = now.strftime("%Y")

    written, skipped = 0, 0
    for site_dir, config in sites:
        sid = config["site_id"]
        pid = int(config["plant_id"])
        try:
            realtime = client.plant_realtime(pid)
            today    = client.plant_energy(pid, "day",   today_str)
            month    = client.plant_energy(pid, "month", month_str)
            year     = client.plant_energy(pid, "year",  year_str)
            total    = client.plant_energy(pid, "total", "")
        except SunsynkError as exc:
            print(f"  FAIL {sid}: {exc}"); skipped += 1; continue
        except requests.RequestException as exc:
            print(f"  FAIL {sid} (network): {exc}"); skipped += 1; continue

        # ── Enrich: inject realtime etoday into today if missing ──
        # The /day endpoint often returns empty records, but /realtime has etoday
        if realtime.get("etoday") and not today.get("etoday"):
            today["etoday"] = realtime["etoday"]
        # Also inject month/year/total from realtime if missing
        if realtime.get("etotal") and not total.get("etotal"):
            total["etotal"] = realtime["etotal"]

        # ── Enrich: try inverter endpoint for hourly data if /day returned nothing ──
        has_records = bool(today.get("records") or today.get("infos"))
        if not has_records:
            try:
                inverters = client.plant_inverters(pid)
                if inverters:
                    sn = inverters[0].get("sn", "")
                    if sn:
                        inv_out = client.inverter_output(sn, today_str)
                        inv_records = inv_out.get("records") or inv_out.get("infos") or []
                        if inv_records:
                            today["records"] = inv_records
                            print(f"  ℹ️  {sid}: using inverter {sn} for hourly data ({len(inv_records)} records)")
                        else:
                            # Try input endpoint
                            inv_in = client.inverter_input(sn, today_str)
                            inv_in_records = inv_in.get("records") or inv_in.get("infos") or []
                            if inv_in_records:
                                today["records"] = inv_in_records
                                print(f"  ℹ️  {sid}: using inverter input {sn} ({len(inv_in_records)} records)")
            except Exception as e:
                print(f"  ⚠️  {sid}: inverter fallback failed: {e}")

        try:
            processor.write_site(site_dir, config,
                                  realtime=realtime,
                                  today=today, month=month,
                                  year=year, total=total)
            print(f"  OK   {sid}")
            written += 1
        except Exception as exc:
            print(f"  FAIL {sid} (processor): {exc}")
            skipped += 1
        time.sleep(0.5)

    print(f"\nDone. {written} written, {skipped} skipped.")


def main() -> int:
    try:
        if "--discover" in sys.argv:
            run_discover()
        else:
            run_fetch()
    except SunsynkError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"NETWORK ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
