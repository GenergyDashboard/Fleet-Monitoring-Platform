# SolisCloud (Ginlong) platform

Pulls energy data from the SolisCloud OpenAPI at
`www.soliscloud.com:13333` (yes, the port is part of the URL). Uses
HMAC-SHA1 request signing — every request is signed with your KeySecret
before sending. No bearer tokens, no expiry.

## Setup steps

### 1. Submit a service ticket to enable API access

This step is required — API access is off by default for new accounts.

1. Log in to https://www.soliscloud.com
2. Open a support ticket via the Help menu
3. Request "API access for fleet monitoring (third-party integration)"
4. Provide your company name (Genergy) and approximate site count
5. Wait for Ginlong support to enable API access on your account
   (typically 1–3 business days)

Once enabled, you'll be able to see an "API Management" section in your
account settings.

### 2. Generate your API credentials

After API access is enabled:

1. Log in to https://www.soliscloud.com
2. Click your account icon (top-right) → **Basic Settings**
3. Click **API Management** in the left sidebar
4. Click **Activate now**, scroll down, click **Agree and Activate**
5. Click **View Key** → "I have read and agree to disclaim"
6. Click **Verification code** — a code is sent to your account email
7. Enter the code (within 60 seconds) → click **Confirm**
8. The page displays your **API ID (KeyID)**, **API Secret (KeySecret)**,
   and **API URL**. Save them immediately — they're shown only once.

### 3. Add two GitHub secrets

| Secret | Value |
|---|---|
| `SOLIS_KEY_ID`     | API ID from step 2 |
| `SOLIS_KEY_SECRET` | API Secret from step 2 |

The default base URL is `https://www.soliscloud.com:13333`. If the
"API URL" shown in step 2 differs (region-specific endpoints exist),
also set `SOLIS_BASE_URL` as a secret with that value.

### 4. Discover stations

```cmd
cd platforms\soliscloud
set SOLIS_KEY_ID=<your-key-id>
set SOLIS_KEY_SECRET=<your-key-secret>
pip install requests
python fetch.py --discover
```

The HMAC signing happens inside `fetch.py` — you don't need to do anything
manually. Output saves to `soliscloud_discovered.json`.

### 5. Create site configs

For each station, create a `platforms/soliscloud/sites/<slug>/config.json`
matching the `_example/config.json` template. Note: `station_id` is stored
as a **string**, not an integer, because SolisCloud's IDs can exceed the
JS safe-integer range (e.g. `1298491919449674519`).

You already use SolisCloud for 1st Ave Spar — that site can move into
this platform structure when you're ready, replacing the older standalone
repo.

### 6. Push and run

Workflow cron: `6-59/5` (offset from other platforms). Trigger from
`Actions → SolisCloud fetch → Run workflow`.

## How HMAC signing works

For every request:

```
StringToSign = VERB + "\n" + Content-MD5 + "\n" + Content-Type + "\n" + Date + "\n" + Resource
Signature    = base64(HMAC-SHA1(StringToSign, KeySecret))
Authorization: API <KeyId>:<Signature>
```

If your KeySecret leaks, anyone can sign requests as you. Treat it like
a password — rotate via API Management if compromised.

## Schema notes

| SolisCloud field | Our schema |
|---|---|
| `dayEnergy/monthEnergy/yearEnergy/allEnergy` | `energy.*.pv` |
| `dayConsumeEnergy` | `energy.today.consumption` |
| `dayBuyEnergy` | `energy.today.import` |
| `daySellEnergy` | `energy.today.export` |
| `dayChargeEnergy` | `energy.today.charge` |
| `dayDisChargeEnergy` | `energy.today.discharge` |
| `state` (1=online, 2=offline, 3=alarm) | `current.status` |
