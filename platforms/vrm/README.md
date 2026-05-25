# VRM (Victron Remote Monitoring) platform

Pulls energy data from the Victron VRM API and writes it into the shared
schema that the fleet dashboard consumes.

## Why this is the cleanest of our platforms

- **Public REST API.** `vrmapi.victronenergy.com` resolves and accepts
  connections from anywhere, so the workflow runs on a free GitHub
  cloud runner. No DNS patching, no self-hosted runner.
- **Personal Access Token auth.** No login flow, no token expiry, no
  session limits. Generate once in the portal, use forever.
- **JSON-native API.** Documented response shapes, standard REST patterns.

## One-time setup

### 1. Generate a Personal Access Token

1. Sign in to https://vrm.victronenergy.com
2. Click your profile (top-right) → **Preferences** → **Integrations**
3. Under **Access tokens**, click **Add**
4. Give it a descriptive name (e.g. `genergy-fleet-dashboard`)
5. Copy the token immediately - it's shown ONCE and never again

### 2. Add the token as a GitHub secret

Repo `Settings → Secrets and variables → Actions → New repository secret`:

| Secret | Value |
|---|---|
| `VRM_TOKEN` | The token you just generated |

### 3. Discover your sites

Run locally to list every installation the token can see:

```cmd
cd platforms\vrm
set VRM_TOKEN=<your-token>
set VRM_USER_ID=<your-user-id>
python fetch.py --discover
```

`VRM_USER_ID` is the multi-digit number in your VRM profile URL. Open
https://vrm.victronenergy.com → Profile → look at the URL.

The discover run prints every site you can monitor and saves the full
list to `vrm_discovered.json`. Use those `idSite` numbers in step 4.

### 4. Create site config files

For each site you want to include, create a folder under
`platforms/vrm/sites/<slug>/` with a `config.json` matching the
`_example/config.json` template. Fields to set:

| Field | Source |
|---|---|
| `site_id` | Folder slug, e.g. `coega-vrm-1` |
| `name` | Display name for the dashboard |
| `id_site` | The `idSite` number from `--discover` |
| `location.lat` / `.lon` | Site coordinates - used by weather workflow |
| `location.town` | Town name - used by location-based groups |
| `capacity_kwp` | Total panel capacity in kW |
| `system.panel_groups[]` | Panel info - drives the naive performance method |
| `tariff` | Tariff config (see schema.md) - leave as `null` initially |

### 5. Push and let it run

Once at least one site has a `config.json`, the next scheduled run of
the workflow (or a manual trigger from the Actions tab) will pull data
for it.

## Schema notes - field mappings

The processor maps VRM attribute codes to our shared schema:

| VRM code | Our schema field | Verified? |
|---|---|---|
| `Pb` | `energy.pv` (solar yield) | yes |
| `Pc` | `energy.consumption` | yes |
| `Gc` | `energy.import` (grid consumed) | yes |
| `Gb` | `energy.export` (grid generated) | yes |
| `Bc` | `energy.discharge` (battery to consumers) | **NEEDS REAL-DATA CHECK** |
| `Bg` | `energy.charge` (battery to grid?) | **NEEDS REAL-DATA CHECK** |

The battery codes (Bc, Bg) are based on community documentation, not
official Victron schema docs. Once a real battery site lands data,
compare against the Energy Flow view in the VRM portal to confirm the
mapping is correct. Adjust `processor.py` if needed - this is a 2-line
fix in `bucket()` and the hourly series extraction.

## Discover command

```cmd
python fetch.py --discover
```

Prints every installation visible to the token and saves the raw list to
`vrm_discovered.json`. Re-run whenever your portal access changes.
