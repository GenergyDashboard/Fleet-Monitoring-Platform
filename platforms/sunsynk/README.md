# Sunsynk platform

Pulls energy data from the Sunsynk Connect API (`api.sunsynk.net`).
Simplest of the new platforms — just your normal sunsynk.net login.

## Why this is simple

- Standard OAuth-password flow — no application approval needed
- Public API endpoint, works from cloud runners
- Token cached locally, refreshed when expired

## Setup steps

### 1. Confirm you have a sunsynk.net account

You already do if you manage Sunsynk sites. The username is your
sunsynk.net email, password is whatever you log into the portal with.
If you don't have one yet, sign up at https://www.sunsynk.net.

### 2. Add two GitHub secrets

`Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `SUNSYNK_USERNAME` | Your sunsynk.net account email |
| `SUNSYNK_PASSWORD` | Your sunsynk.net password (raw — sent over HTTPS, not hashed) |

### 3. Discover your sites locally

```cmd
cd platforms\sunsynk
set SUNSYNK_USERNAME=<your-email>
set SUNSYNK_PASSWORD=<your-password>
pip install requests
python fetch.py --discover
```

Output saves to `sunsynk_discovered.json`. Paste it back to me and I'll
generate site configs same as for VRM and FusionSolar.

### 4. Create site configs

For each plant you want, create a folder `platforms/sunsynk/sites/<slug>/`
containing a `config.json` matching the `_example/config.json` template.
Required fields: `site_id`, `name`, `plant_id`, `location.lat/.lon`,
`capacity_kwp`.

### 5. Push and run

The workflow runs every 5 min on cron `5-59/5` (offset from other
platforms). Trigger manually from `Actions → Sunsynk fetch → Run workflow`
to test.

## Schema notes

| Sunsynk field | Our schema |
|---|---|
| `pac` | `current.power_kw` (W → kW) |
| `etoday/emonth/eyear/etotal` | `energy.today/month/year/lifetime.pv` |
| `load` | `energy.*.consumption` |
| `toGrid` | `energy.*.export` |
| `fromGrid` | `energy.*.import` |
| `toBat` | `energy.*.charge` |
| `fromBat` | `energy.*.discharge` |

Today's hourly series comes from `records[]` in the `/plant/energy/{id}/day`
response, where each record is one hour with `time: 'HH:MM'`.

## Honest call-out

The Sunsynk API doesn't have an official public spec — community examples
are the documentation. The field mappings above are based on what most
HA integrations and the `sunsynkloggerapi` Python lib use, but Sunsynk
has changed shapes before. If the first run produces empty or partial
buckets, check the actual response JSON against `_bucket()` in
`processor.py` — usually a 1–2 line fix.
