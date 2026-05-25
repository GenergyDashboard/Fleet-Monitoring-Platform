# Sigenergy (SigenCloud) platform

Pulls data from the Sigenergy OpenAPI at `api-eu.sigencloud.com`. Newest
of the platforms; their developer portal is straightforward but the API
field naming is less well-documented than the others.

## Setup steps

### 1. Apply for developer portal access

1. Go to https://developer.sigencloud.com
2. Sign in with your Sigenergy account credentials (the same login as
   `app-eu.sigencloud.com`)
3. Apply for OpenAPI access — there's a form asking for company name and
   intended use. Approval is usually same-day to 1–2 business days.

### 2. Generate AppKey + AppSecret

Once approved:

1. Sign in to https://developer.sigencloud.com
2. Go to **Control Center → Settings → Applications**
3. Click **Create Application** if you don't have one
4. Note the **AppKey** and **AppSecret** values shown — save immediately

### 3. Add two GitHub secrets

| Secret | Value |
|---|---|
| `SIGEN_APPKEY`    | AppKey from step 2 |
| `SIGEN_APPSECRET` | AppSecret from step 2 |

Default endpoint is `https://api-eu.sigencloud.com`. If your account is
on a different regional gateway, also set `SIGEN_BASE_URL`.

### 4. Discover stations

```cmd
cd platforms\sigenergy
set SIGEN_APPKEY=<your-app-key>
set SIGEN_APPSECRET=<your-app-secret>
pip install requests
python fetch.py --discover
```

### 5. Push and run

Workflow cron: `7-58/5`. Rate-limited to 10 req/min, so the fetch
sleeps 6 seconds between calls. Large fleets take longer to refresh —
plan accordingly.

## How auth works

```
key_b64    = base64(AppKey + ":" + AppSecret)
POST /openapi/auth/login/key  with body {"key": key_b64}
response.data is a JSON-encoded string that must be parsed AGAIN to get:
  {"tokenType": "Bearer", "accessToken": "...", "expiresIn": 43199}
```

The double-parse is a Sigenergy quirk — the `data` field comes back as
a string containing JSON rather than as a nested object. Token is cached
to `.sg_token.json` (gitignored).

## Honest call-outs

1. **Field names are guesses.** I built the processor's `_bucket()`
   function from Sigenergy's general API conventions, not from a published
   spec. After your first run, look at the actual `data.json` produced
   and compare with the `app-eu.sigencloud.com` portal. If any field is
   `0` when the portal shows real numbers, the field name in
   `processor.py` needs adjusting — usually a 1-line fix.

2. **Endpoint paths might differ.** I used `/openapi/station/list`,
   `/openapi/station/{id}/overview`, `/openapi/station/{id}/statistics`.
   If any return 404, check `developer.sigencloud.com` documentation for
   the latest path names.

3. **Rate limit is real.** 10 req/min applies per OpenAPI account. The
   client throttles to 6 seconds between calls. For 5+ sites, the run
   takes 30+ seconds.
