# FusionSolar workflow — setup

The `.github/workflows/fusionsolar.yml` workflow fetches every FusionSolar site
on a schedule and commits the refreshed `data.json` / `history.json` /
`hourly_history.json` back to the repo. It runs on **GitHub's free
`ubuntu-latest` cloud runner** — no self-hosted runner is required.

## How it works without a self-hosted runner

`intl.fusionsolar.huawei.com` does not resolve through GitHub's cloud DNS, but
the host *itself* is reachable from cloud-runner IPs once you know the IP. So
`fetch.py` starts with a `fix_dns_resolution()` step that:

1. Tries a normal DNS lookup (will fail on cloud runners — that's expected)
2. Queries Google DNS (`8.8.8.8`) directly via `dig` to get the current IP
3. Falls back to a stable known-good IP if even that fails
4. Writes the resolved IP into `/etc/hosts`

After that, every HTTPS call from `requests` reaches the host normally for the
rest of the run. Same trick the proven Nautica/FusionSolar scraper repo uses —
identical host, identical situation.

If Huawei ever moves the host to a different IP range that *is* blocked at the
firewall level (not just DNS), this approach would stop working and you would
need to fall back to a self-hosted runner. The fallback IP can also be edited
in `fetch.py`'s `FALLBACK_IP` constant if Google DNS becomes unreliable.

## One-time setup

### 1. Repository secrets

**Settings → Secrets and variables → Actions → New repository secret:**

| Secret | Value |
|---|---|
| `FUSIONSOLAR_USERNAME` | Northbound API username (e.g. `Ross@genergy.co.za`) |
| `FUSIONSOLAR_PASSWORD` | Northbound API password / system code |

That's it — just two secrets. Don't create a `FUSIONSOLAR_HOST` secret; the
host is hardcoded in `fetch.py` (it's public information, not sensitive).
If you previously created an empty `FUSIONSOLAR_HOST` secret from an older
SETUP guide, **delete it** — an empty secret would override the hardcoded
default and the workflow would fail with "No host supplied".

### 2. Workflow write permissions

The commit step pushes refreshed data back. Enable write access once:

**Settings → Actions → General → Workflow permissions → Read and write permissions → Save**

That's it. No runner to install, no machine to keep on.

## Trigger it

- **Manually:** Actions tab → "FusionSolar fetch" → "Run workflow"
- **On schedule:** runs automatically every 30 min from 06:00–18:00 SAST
  (04:00–16:00 UTC), the daylight window for South African PV

## What gets committed

The workflow commits to the repo on each successful run. Files updated:

- `platforms/fusionsolar/sites/*/data.json`
- `platforms/fusionsolar/sites/*/history.json`
- `platforms/fusionsolar/sites/*/hourly_history.json`

`hourly_history.json` grows by 24 entries per site per day — about 8 MB/year
per site at unlimited retention. Comfortable for years.

## What if a run fails

Open the run in the Actions tab and look at the "Fetch all FusionSolar sites"
step. Common failure modes:

| Symptom in log | Cause | Fix |
|---|---|---|
| "DNS still failing after patching /etc/hosts" | Both the live IP from Google DNS *and* the fallback IP are unreachable | Try a fresh fallback IP — `nslookup intl.fusionsolar.huawei.com 8.8.8.8` from any machine, update `FALLBACK_IP` in `fetch.py` |
| "Could not patch /etc/hosts: sudo: command not found" | Runner image changed — sudo is normally available on ubuntu-latest | File an issue and use a self-hosted runner as fallback |
| "Login failed (rate limited)" | Two runs overlapped — only one Northbound session allowed per account | The `concurrency` block in the workflow prevents this, but if you triggered a manual run while a scheduled one was active, wait and retry |
| "SKIP `<site-id>`: no data returned" | One site's station_code is wrong, or that site genuinely has no data right now | Confirm the station code in `config.json` matches what `--discover` shows |
