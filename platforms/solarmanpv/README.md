# SolarmanPV platform

Pulls energy data from the SolarmanPV OpenAPI (`globalapi.solarmanpv.com`)
and writes it into the shared schema the fleet dashboard consumes.
Replaces the older Playwright scraper at `pro.solarmanpv.com` with proper
REST API calls — far more reliable, no Cloudflare Turnstile challenges,
no session-cookie management, no DNS games.

## One-time setup

### 1. Request OpenAPI access from Solarman

Email **customerservice@solarmanpv.com** asking for OpenAPI access for
fleet monitoring. Mention:

- Your company name (Genergy)
- That you already have a pro.solarmanpv.com business account
- Approximately how many sites you'll monitor

They reply within a few business days with an **AppID** and **AppSecret**
specifically for your account. Save these — they're the equivalent of
GitHub personal access tokens for Solarman.

### 2. Add four GitHub secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `SOLARMAN_APPID`     | The AppID from step 1 |
| `SOLARMAN_APPSECRET` | The AppSecret from step 1 |
| `SOLARMAN_EMAIL`     | Your `pro.solarmanpv.com` account email |
| `SOLARMAN_PASSWORD`  | Your `pro.solarmanpv.com` account password (raw — the script SHA-256 hashes it before sending) |

### 3. Discover your sites

Run locally to list stations visible to your account:

```cmd
cd platforms\solarmanpv
set SOLARMAN_APPID=<your-app-id>
set SOLARMAN_APPSECRET=<your-app-secret>
set SOLARMAN_EMAIL=<your-email>
set SOLARMAN_PASSWORD=<your-password>
pip install requests
python fetch.py --discover
```

Output prints every station and saves to `solarmanpv_discovered.json`.

### 4. Create site config files

For each station you want included, create a folder under
`platforms/solarmanpv/sites/<slug>/` with a `config.json` matching
the `_example/config.json` template. Required fields:

| Field | Source |
|---|---|
| `site_id` | Folder slug (URL-safe lowercase) |
| `name` | Display name for the dashboard |
| `station_id` | The integer station ID from `--discover` |
| `location.lat` / `.lon` | Site coordinates (drives weather workflow + map) |
| `location.town` | Town name |
| `capacity_kwp` | Total panel capacity in kW |

### 5. Push and let it run

The workflow runs every 5 min during daylight (offset slots `4-59/5`).
Trigger manually from `Actions → SolarmanPV fetch → Run workflow` to
test before waiting for the schedule.

## How it works

- **Auth:** AppID + AppSecret + email + SHA-256(password) → access token,
  valid for 2 months. Token is cached to `.sm_token.json` (gitignored)
  and refreshed only when expired or rejected.
- **Endpoints used:**
  - `/account/v1.0/token` — login
  - `/station/v1.0/list` — discovery
  - `/station/v1.0/realTime` — current state per station
  - `/station/v1.0/history` — daily/monthly/yearly/5-min historical
- **Rate limit:** ~50 requests/minute per OpenAPI account. With 5 calls
  per site per run, you can comfortably run ~10 sites per minute. For
  larger fleets, sites are processed sequentially with 0.5s spacing.

## Schema notes

Field mappings used in the processor (`processor.py`):

| SolarmanPV field | Our schema |
|---|---|
| `generationValue` | `energy.*.pv` |
| `useValue` | `energy.*.consumption` |
| `buyValue` / `gridImportValue` | `energy.*.import` |
| `sellValue` / `gridExportValue` | `energy.*.export` |
| `chargeValue` | `energy.*.charge` |
| `dischargeValue` | `energy.*.discharge` |
| `status` (1=online, 2=alarm, 3=offline) | `current.status` |

Hourly series is derived by aggregating Solarman's 5-min cumulative
samples — the last value in each hour minus the last value of the
previous hour. This handles missing samples gracefully.
