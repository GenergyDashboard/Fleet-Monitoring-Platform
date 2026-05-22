# Normalized data schema

This is the **contract** between every platform's ingestion layer and the shared
dashboard engine. FusionSolar, GoodWe, SolisCloud, Sunsynk and Solarman all look
different at the API level. After each platform's `processor.py` runs, they must
all produce **identical** `data.json` and `history.json` shapes.

Get this right once and the dashboard engine never needs to know which inverter
platform a site uses.

Repo layout this schema assumes:

```
GenergyDashboard-API/
  index.html                  root fleet dashboard
  map.html                    Leaflet SA map
  shared/
    dashboard-engine.js       one renderer for all sites
    schema.md                 <-- this file
  platforms/
    fusionsolar/
      fetch.py                API client + orchestrator
      processor.py            raw response -> normalized schema
      sites/
        nautica-mall/
          config.json         per-site, hand-written
          data.json           per-site, written by processor
          history.json        per-site, written by processor
          predictions.min.json
  weather/
    refresh_irradiation.py    shared Open-Meteo fetch, writes into each data.json
```

## Conventions

- **All timestamps are SAST (UTC+2).** No daylight saving in South Africa, so a
  fixed +2h offset is always correct. `updated_at` is full ISO 8601 with the
  `+02:00` offset. Timestamps inside arrays use `YYYY-MM-DD HH:MM:SS` (sortable,
  SAST local, no suffix).
- **Energy is kWh, power is kW, irradiation is kWh/m2.** Never mix.
- A processor must never invent fallback numbers. Missing data is `null`, and the
  dashboard engine decides how to render a gap.

## `config.json` (per site, hand-written)

This is the only per-site file you edit by hand. Everything else is generated.

```json
{
  "site_id": "nautica-mall",
  "name": "Nautica Shopping Centre",
  "platform": "fusionsolar",
  "station_code": "NE=33xxxxxx",
  "capacity_kwp": 150.0,
  "commissioned": "2024-03-01",
  "location": { "lat": -33.9608, "lon": 25.6022, "town": "Gqeberha",
                "address": "116 Algoa Road, Kariega" },
  "modules": {
    "tou": false,
    "environmental": true,
    "warranty": true
  },
  "system": {
    "panel_groups": [
      {
        "panel_count": 1645,
        "panel_model": "Canadian Solar CS6W-560MS",
        "panel_rated_w": 560,
        "panel_area_m2": 2.5833,
        "panel_efficiency": 0.2168,
        "group_area_m2": 4249.53
      }
    ],
    "area_total_m2": 4249.53,
    "effective_area_m2": 921.30
  }
}
```

| Field | Meaning |
|---|---|
| `system.panel_groups` | One entry per distinct panel model on site. A mixed array (two models) has two entries. Empty `[]` when panel data is unknown. |
| `panel_area_m2` | Physical area of one panel (datasheet L x W). |
| `panel_efficiency` | Datasheet module efficiency as a fraction. |
| `area_total_m2` | Sum of all panel areas — total collector area. |
| `effective_area_m2` | `sum(group_area_m2 x panel_efficiency)`. **This is the target-PV multiplier.** |

### Target / predicted PV

The performance baseline uses the physical-area method:

```
target_PV_kWh = sum(hourly_irradiation_kWh_per_m2 / 1000) x effective_area_m2
```

The dashboard engine takes the irradiation series already in `data.json`,
divides each value by 1000, sums it, and multiplies by `effective_area_m2`.
For a single-model site `effective_area_m2` equals `area_total_m2 x efficiency`;
storing the rolled-up figure means the engine never needs the panel breakdown
at render time. Actual PV over the same period divided by target PV gives the
performance ratio.

### Tariff config (per-site, hand-written)

The `tariff` block declares how to put a money value on the energy flow. Every
site has one. Four shapes, picked by `type`:

```json
"tariff": {
  "type": "tou",                          // flat | tou | ppa
  "vat_included": true,                   // numbers are R/kWh including 15% VAT
  "export": {
    "mode": "feed-in",                    // net-metering | feed-in | none
    "rate_periods": [
      { "from": "2024-06-01", "to": "2025-05-31", "rate": 0.55 },
      { "from": "2025-06-01", "to": null,         "rate": 0.62 }
    ]
  },
  "rate_periods": [
    { "from": "2024-06-01", "to": "2024-06-30",
      "tou": { "peak": 5.501,  "standard": 1.375,  "off_peak": 0.917  } },
    { "from": "2024-07-01", "to": "2024-08-31",
      "tou": { "peak": 6.5287, "standard": 1.6321, "off_peak": 1.0882 } },
    { "from": "2024-09-01", "to": "2025-05-31",
      "tou": { "peak": 2.7094, "standard": 1.5233, "off_peak": 1.0882 } }
  ]
}
```

| Field | Meaning |
|---|---|
| `type` | `flat` = single import rate; `tou` = peak/standard/off-peak by Eskom schedule; `ppa` = customer pays a single rate per kWh of PV generated (self-consumed PV). |
| `vat_included` | Always `true` for our configs — the stored numbers are R/kWh inclusive of 15% VAT. |
| `export.mode` | `net-metering` = export credited at the import rate; `feed-in` = export paid at `export.rate_periods`; `none` = export not compensated. |
| `rate_periods[]` | A list of dated rate rows. The engine finds the row whose `from`/`to` bracket the kWh's date. `to: null` means "current, no end". Use `flat: 2.85` for `type=flat`, `ppa: 1.65` for `type=ppa`, `tou: {peak, standard, off_peak}` for `type=tou`. |

**Rate timing convention** (verified against the Eskom tariff sheet):
- A new tariff comes in on **1 June** each year. Jun is the transition month
  where last year's high-season rate may still apply; Jul–Aug are the new
  high-season rate; Sept onward is the new low-season rate. **Each row is taken
  literally** — the engine does not infer June from July. Put whatever the
  utility published for June into the June row.
- The Eskom TOU period schedule (which hours are peak/standard/off-peak by
  day-of-week and high/low season) is the **default** in `shared/financial.py`.
  Most sites buy from Eskom and inherit this schedule automatically.
- A site on a non-Eskom tariff (e.g. municipal) can **override the schedule**
  per-site by adding a `schedule` block to its `tariff` config:

```json
"tariff": {
  "type": "tou",
  "vat_included": true,
  "schedule": {
    "high_demand_months": [6, 7, 8],
    "weekday":  { "high": {"peak": [...], "standard": [...]},
                  "low":  {"peak": [...], "standard": [...]} },
    "saturday": { "high": {"standard": [...]}, "low": {"standard": [...]} },
    "sunday":   { "high": {"standard": [...]}, "low": {"standard": [...]} }
  },
  "rate_periods": [ ... ]
}
```

Hours are 24-hour clock numbers; any hour not listed defaults to `off_peak`.
If the `schedule` field is absent, the Eskom default applies.

### Energy block (in `data.json`)

The processor computes period totals plus an hourly array, and the financial
module then uses them to compute money. Period keys: `today`, `month`, `year`,
`lifetime`. Each period dict carries the **six metered fields** *and* the
**seven powerflow split fields** plus a convenience `self_consumed` rollup.

```json
"energy": {
  "today":  { "pv": 612.4, "import": 320.1, "export": 18.7,
              "charge": 145.2, "discharge": 130.8, "consumption": 906.0,
              "pv_to_load": 480.2, "pv_to_batt": 113.5, "pv_to_grid": 18.7,
              "batt_to_load": 130.8, "batt_to_grid": 0.0,
              "grid_to_load": 295.0, "grid_to_batt": 25.1,
              "self_consumed": 611.0 },
  "month":  { ...same fields... },
  "year":   { ... },
  "lifetime": { ... },
  "hourly": {
    "pv":         [{"time": "...", "value": 0.0}, ...],
    "import":     [{"time": "...", "value": 0.0}, ...],
    "export":     [{"time": "...", "value": 0.0}, ...],
    "charge":     [{"time": "...", "value": 0.0}, ...],
    "discharge":  [{"time": "...", "value": 0.0}, ...]
  },
  "powerflow_hourly": [
    {"time": "...", "pv_to_load": ..., "pv_to_batt": ..., "pv_to_grid": ...,
                    "batt_to_load": ..., "batt_to_grid": ...,
                    "grid_to_load": ..., "grid_to_batt": ...},
    ...
  ]
}
```

| Field | Meaning |
|---|---|
| `pv` | PV energy generated (kWh). FusionSolar field: `PVYield` / `inverter_power`. |
| `import` | Energy bought from grid (kWh). FusionSolar field: `buyPower`. |
| `export` | Energy fed to grid (kWh) — includes battery-to-grid, not pure PV export. FusionSolar field: `ongrid_power`. |
| `charge` | Energy charged into the battery (kWh). FusionSolar field: `chargeCap`. |
| `discharge` | Energy discharged from the battery (kWh). FusionSolar field: `dischargeCap`. |
| `consumption` | Energy consumed by the site (kWh). FusionSolar field: `use_power`. |
| `pv_to_*`, `batt_to_*`, `grid_to_*` | Seven directional flows from the **dual-anchor powerflow split** (`shared/powerflow.py`). Reconcile all four metered values (import, export, charge, discharge) to within rounding. |
| `self_consumed` | `pv_to_load + batt_to_load` — PV + battery energy actually used on-site. |

**Powerflow split is the authoritative source for derived flows.** The
algorithm (`shared/powerflow.py`) is verified against Valeo's
Combined_Plant_Report and produces conservation-respecting splits. Old
approximations like `consumption - import` are not used — they fail on
battery sites where import goes to charge the battery rather than the load.

### Status detection (in `data.json` `current`)

The `current` block carries three status fields:

```json
"current": {
  "power_kw": 87.3,
  "today_kwh": 612.4,
  "month_kwh": 14203.0,
  "total_kwh": 287104.0,
  "status": "online",
  "status_severity": "ok",
  "status_reason": null
}
```

| Status | Severity | Meaning |
|---|---|---|
| `online` | `ok` | Reporting and producing as expected. |
| `underperforming` | `warn` | Reporting but zero PV through 09:00–15:00 SAST daylight window. Catches "quiet failures" where the inverter is online but dead. |
| `fault` | `critical` | API reports `real_health_state = 2`. |
| `offline` | `critical` | API reports `real_health_state = 1`, or no API response. |
| `no_data` | `warn` | API returned nothing for this site. |
| `unknown` | n/a | Could not determine. |

`status_reason` is human-readable text used for alerts and dashboard tooltips.
The dashboard engine uses `status_severity` to colour the site card.

### Financial block (in `data.json`)

Same period keys as `energy`. Populated by `shared/financial.py` using the
site's `tariff` block and the energy series.

```json
"financial": {
  "today":    { "cost_import": 950.20, "revenue_export": 11.50,
                "ppa_cost": 0.0, "savings": 1240.80, "net": 302.10 },
  "month":    { ... },
  "year":     { ... },
  "lifetime": { ... }
}
```

| Field | Meaning |
|---|---|
| `cost_import` | What the site paid the utility for `import` kWh. |
| `revenue_export` | What the site is paid for `export` kWh (zero if `export.mode = none`). |
| `ppa_cost` | What the site pays the PPA owner for self-consumed PV kWh (zero unless `type = ppa`). |
| `savings` | What the site *would have* paid if it had no PV — `self_consumed × import rate`. The headline benefit number. |
| `net` | `savings + revenue_export - ppa_cost`. The all-in financial benefit of the system for the period. |

| Field | Meaning |
|---|---|
| `site_id` | Folder name. Lowercase, hyphenated. Stable forever. |
| `platform` | One of `fusionsolar`, `goodwe`, `soliscloud`, `sunsynk`, `solarman`. |
| `station_code` | The platform's own plant identifier. For FusionSolar this is the `stationCode` (e.g. `NE=33800007`). |
| `capacity_kwp` | Installed DC capacity. Used for performance ratio. |
| `location` | Drives the shared weather module and the map pin. `lat`/`lon` are suburb-centre approximations — fine for the regional irradiation lookup, refine for exact map pins. `address` is the raw street address from the platform. |
| `capacity_kwp` | Installed DC capacity in kWp. May be `null` when the platform never recorded it — fill from the install spec. Only used for the performance-ratio figure, so `null` does not block ingestion. |
| `modules` | Feature toggles. The dashboard engine shows/hides sections from this — no forked code per site. |

## `data.json` (per site, generated)

The current snapshot plus today's hourly curve. Overwritten on every run.

```json
{
  "site_id": "nautica-mall",
  "name": "Nautica Shopping Centre",
  "platform": "fusionsolar",
  "capacity_kwp": 150.0,
  "updated_at": "2026-05-18T14:30:00+02:00",
  "current": {
    "power_kw": 87.3,
    "today_kwh": 612.4,
    "month_kwh": 14203.0,
    "total_kwh": 287104.0,
    "status": "online"
  },
  "today_hourly": [
    { "time": "2026-05-18 06:00:00", "pv_kwh": 0.0 },
    { "time": "2026-05-18 07:00:00", "pv_kwh": 8.2 }
  ],
  "irradiation": {
    "source": "fusionsolar",
    "today_total": 5.81,
    "today_hourly": [
      { "time": "2026-05-18 06:00:00", "value": 0.0 },
      { "time": "2026-05-18 07:00:00", "value": 0.12 }
    ]
  }
}
```

| Field | Notes |
|---|---|
| `current.power_kw` | Instantaneous AC power. `null` if the platform doesn't expose it. |
| `current.status` | `online`, `offline`, `fault`, or `unknown`. |
| `today_hourly` | One entry per hour of the current day. `pv_kwh` is energy generated *in that hour*, not a running total. |
| `irradiation.source` | `open-meteo` once the weather module fills it, or `fusionsolar` for the rare site with a native EMI sensor. `null` before the weather module has run. |

## `history.json` (per site, generated)

Rolling daily history. The processor merges new days into whatever already exists,
so the file fills out over time and survives a missed run.

```json
{
  "site_id": "nautica-mall",
  "days": [
    { "date": "2026-04-18", "pv_kwh": 580.2, "irradiation": 5.41 },
    { "date": "2026-04-19", "pv_kwh": 612.8, "irradiation": 5.66 }
  ]
}
```

- `days` is sorted ascending by `date`.
- Retention is set by `HISTORY_DAYS` in `processor.py` (default 400, so a full
  year-plus is kept for actual-vs-predicted comparison against `predictions.min.json`).
- `irradiation` may be `null` until the weather module backfills it.

### Irradiation forecast (in `data.json`)

`irradiation_forecast` is set by the weather module alongside today's
irradiation. Holds **next-day** hourly GHI for performance-alert use:

```json
"irradiation_forecast": {
  "date": "2026-05-22",
  "total_forecast": 3.131,
  "hourly": [{"time": "2026-05-22 00:00:00", "value": 0.0}, ...]
}
```

Same hourly shape as `irradiation.today_hourly`, separately keyed so it
can't be mixed up with realised data.

### Performance block (in `data.json`)

`performance` is set by `shared/performance.py` on every fetch. Compares
actual PV against expected PV for elapsed daylight hours.

```json
"performance": {
  "method": "naive",
  "hourly_expected": [
    {"time": "2026-05-21 11:00:00", "ghi_w_m2": 250.0, "expected_kwh": 109.5},
    ...
  ],
  "today_expected_total": 384.2,
  "today_actual_total":   348.7,
  "performance_pct":      90.8,
  "elapsed_hours_compared": 7,
  "reason": "Expected = irradiation x panel area x efficiency"
}
```

**Method picked automatically per site:**

**Currently every site uses the empirical method.** The naive method
(irradiation × area × efficiency) is implemented but paused — see the
top of `shared/performance.py` for the one-line revert when re-enabling.

| Method | Status | Formula |
|---|---|---|
| `empirical` | **Active for all sites** | Mean of this site's PV at hour-of-day, last 30 days. Matches the existing PV alert dashboard exactly — irradiation is not used |
| `naive` | Paused | `(ghi/1000) × effective_area_m2 × efficiency` — kept in code for future per-site re-enable |

`performance_pct` is `today_actual_total ÷ today_expected_total × 100`,
computed only across hours that have *both* actual and irradiation data.
Returns `null` when there's no daylight yet or insufficient calibration
data — the dashboard renders that as `--`.

The dashboard surfaces this in two places:
- **Site dashboard** — "Performance Today" stat card with the percentage,
  plus an expected-PV overlay line on the hourly chart
- **Fleet dashboard** — replaces the old "% of avg" badge with the
  authoritative percentage. Falls back to the old JS-computed % when
  the performance block isn't yet populated.

## `hourly_history.json` (per site, generated)

Rolling per-hour energy series across days, used by the dashboard's hourly
historical views and by tariff-aware TOU billing. The processor appends today's
hours to it on every run and trims to `HOURLY_HISTORY_DAYS` (default 90 days).

```json
{
  "site_id": "bel-essex-valeo",
  "hours": [
    {
      "time": "2026-05-18 07:00:00",
      "pv": 2.58, "import": 0.0, "export": 724.8,
      "charge": 0.0, "discharge": 815.91,
      "pv_to_load": 2.58, "pv_to_batt": 0.0, "pv_to_grid": 0.0,
      "batt_to_load": 91.11, "batt_to_grid": 724.8,
      "grid_to_load": 0.0, "grid_to_batt": 0.0
    }
  ]
}
```

Each hour entry carries the five metered energy values *and* the seven
powerflow split values for that hour. File sits at a few hundred KB per
site at 90 days. Merge is dedup-by-timestamp, so refetching today's hours
mid-day correctly overwrites earlier provisional values with fresher ones.

## Platform notes

- **FusionSolar** only returns `radiation_intensity` for sites that have a
  physical environmental monitoring instrument (EMI / pyranometer) wired into
  the SmartLogger. Most rooftop sites have none. The processor uses native
  irradiation *only* when the API actually returns a real reading; otherwise it
  leaves the `irradiation` block empty and the shared weather module fills it
  from Open-Meteo — exactly like every other platform.
- **GoodWe / SolisCloud / Sunsynk / Solarman** do not expose irradiation at all,
  so for those sites `irradiation` is always left empty by the processor and the
  shared `weather/refresh_irradiation.py` fills it from Open-Meteo using
  `location`.
- Net effect: Open-Meteo is the default irradiation source fleet-wide. Native
  FusionSolar irradiation is treated as a bonus for the few sites that have a
  sensor, never assumed.
