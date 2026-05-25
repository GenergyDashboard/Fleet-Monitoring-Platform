# Sungrow iSolarCloud platform

Pulls data from the Sungrow iSolarCloud OpenAPI. Most complex setup of
all the platforms — Sungrow requires a developer application and the
approval process can take several days.

## Setup steps

### 1. Apply for developer access

This is the slow step:

1. Go to https://developer.isolarcloud.com.hk
2. Sign in with your iSolarCloud account credentials
3. Click **Applications → Create**
4. Fill out the application form:
   - Application name: "Genergy Fleet Dashboard"
   - Description: "Internal monitoring dashboard for managed Sungrow sites"
   - Choose **V1 API (no OAuth2)** when prompted — simpler auth model
   - Approval usually takes 2–7 business days
5. Wait for approval email from Sungrow

The portal URL above is the Asia/SA gateway. Other regional gateways
exist (`.com` and `.eu` variants); for South African installations,
`.com.hk` is the right one.

### 2. Note your credentials

Once approved, the developer portal shows your application's:

- **AppKey** — public-ish identifier
- **SecretKey** — secret, used for signing some calls
- **AccessKey** — third value, used in the `x-access-key` HTTP header

You also need:
- **Username** — your iSolarCloud account login
- **Password** — your iSolarCloud account password

All five values are required.

### 3. Add five GitHub secrets

| Secret | Value |
|---|---|
| `SUNGROW_APPKEY`     | AppKey from step 2 |
| `SUNGROW_SECRETKEY`  | SecretKey from step 2 |
| `SUNGROW_ACCESS_KEY` | AccessKey (the `x-access-key` header value) |
| `SUNGROW_USERNAME`   | Your iSolarCloud account user |
| `SUNGROW_PASSWORD`   | Your iSolarCloud password (raw) |

### 4. Discover stations

```cmd
cd platforms\sungrow
set SUNGROW_APPKEY=<...>
set SUNGROW_SECRETKEY=<...>
set SUNGROW_ACCESS_KEY=<...>
set SUNGROW_USERNAME=<your-user>
set SUNGROW_PASSWORD=<your-password>
pip install requests
python fetch.py --discover
```

### 5. Create site configs

For each station: `platforms/sungrow/sites/<slug>/config.json` with
`ps_id` (the Sungrow station ID from the discovery output).

### 6. Push and run

Workflow cron: `8-58/5` (offset from all other platforms).

## How auth works (V1 flow)

```
POST /openapi/login
Headers: x-access-key=<AccessKey>, sys_code=901
Body:    {"appkey": <AppKey>, "user_account": <user>,
          "user_password": <password>, "login_type": "1"}
Response: {"result_code": "1", "result_data": {"token": "...", "user_id": "..."}}
```

Subsequent calls include the token in the body of each POST. Token expiry
is not always documented; assumed 4 hours, refreshed automatically when
a 401 is returned.

## Honest call-outs

1. **This is the highest-uncertainty platform.** I built it from
   community examples (jsanchezdelvillar/Sungrow-API for Home Assistant,
   MickMake/GoSungrow). Field names like `p83025` (generation),
   `p13002` (current power) come from those projects. After the first
   run, compare against your iSolarCloud portal — adjust field codes in
   `processor.py` if anything is `0` when it shouldn't be.

2. **Regional endpoint may need adjustment.** Default is
   `gateway.isolarcloud.com.hk`. If that gives DNS errors or returns
   garbage, try `.com` or `.eu` variants — set `SUNGROW_BASE_URL` as
   a secret.

3. **OAuth2 vs V1.** Sungrow now also offers an OAuth2 flow for new
   applications. This code uses the older V1 flow (no OAuth2) because
   it's simpler and what existing integrations use. If you accidentally
   apply for OAuth2 in step 1, the auth flow won't match — re-apply or
   ask Sungrow support to switch your app to V1.

4. **The slowest platform to get running.** Sunsynk takes minutes,
   SolisCloud takes 1–3 days for the service ticket, Sungrow takes
   2–7 days for the developer app. Plan accordingly — don't expect
   this one to work on day one.
