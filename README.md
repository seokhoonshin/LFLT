# RouteOpt — same-day white-glove route optimizer

RouteOpt plans same-day delivery and installation routes for a fleet of 10 straight trucks
running out of the Rialto, CA hub (5550 Mango Ave).  A load planner uploads an Excel of
shipments on D-2,
picks a preset, runs a ≤ 60-second optimization, reviews the routes on a map, hand-edits
if needed, and exports an Excel plan plus printable PDF route sheets.  Every run, version,
edit and export is saved for audit.

**CA LFLT pilot (default rules).**  Runs use the *CA LFLT* profile unless *Legacy* is
chosen on New Run (`--profile legacy` on the CLI).  It plans two trucks out of LGC2 and LGC7
(two DCs at 5550 Mango Ave, Rialto) and LGC8 (5267 Sierra Ave, Fontana); a load is picked up
at one DC only under the rules in
[`docs/CA_LFLT_RouteOpt_Conditions_EN.md`](docs/CA_LFLT_RouteOpt_Conditions_EN.md)
([Korean](docs/CA_LFLT_RouteOpt_Conditions_KR.md)): 06:00 start with 60 min loading,
first appointment 08:00, less than 800 cu ft per load (less than 400 when any shipment
needs a pallet: every product group except HA),
0.1 min per cu ft at each stop + 20 min on every stop-to-stop drive, less than 200 mi one way (150-200 mi flagged for a
carrier check), two turns with less than 300 mi in total and at least 3 h between the
first turn's last delivery and the second turn's departure, lunch 30 min between 11:00
and 14:00.  OBS-only stops can be excluded or added last, only next to planned stops.
How each rule is modelled is in [ASSUMPTIONS.md](ASSUMPTIONS.md#ca-lflt-profile).

Everything is open source and runs locally: **Google OR-Tools** (routing), **OSRM** +
**OpenStreetMap** (road travel times, via Docker), the **US Census Geocoder** (only for
rows without coordinates), **Streamlit** (UI), **SQLAlchemy/SQLite** (history),
**openpyxl** / **reportlab** / **Folium** (exports and map).  No API keys.

---

## 1. Install

### Windows 11 (PowerShell)

```powershell
# Python 3.12 from python.org (tick "Add to PATH"), then in the repo folder:
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"          # or:  pip install -e .   (no test tools)
copy .env.example .env             # optional; defaults work out of the box
```

### Linux / macOS

```bash
make dev            # creates .venv and installs the package with dev tools
source .venv/bin/activate
```

The `Makefile` targets (`make test`, `make lint`, `make typecheck`, `make run`,
`make sample`, `make osrm-setup`, `make osrm-up`) have direct PowerShell equivalents:

| Makefile      | PowerShell (inside the venv)                                   |
|---------------|----------------------------------------------------------------|
| `make test`   | `python -m pytest -q --cov=routeopt --cov-report=term-missing`  |
| `make lint`   | `ruff check routeopt app tests data`                            |
| `make format` | `ruff format routeopt app tests data`                           |
| `make typecheck` | `mypy routeopt`                                              |
| `make run`    | `streamlit run app\Home.py`                                     |
| `make sample` | `routeopt sample --shipments 120`                               |
| `make osrm-setup` | `cd docker\osrm; .\setup.ps1`                               |
| `make osrm-up`| `cd docker\osrm; docker compose up -d`                          |

## 2. OSRM (road travel times)

RouteOpt asks a local OSRM server for road travel times and route geometry.  Without it,
the app still works but uses **straight-line estimates** (distance × 1.3 at 30 mph) and
shows a persistent warning banner; every export then carries the line
*"Travel times are ESTIMATED — OSRM was unavailable"*.

One-time setup with Docker Desktop.  The server carries the road network within **250
miles of the hub** — LA, Orange County, San Diego, the Inland Empire, Bakersfield, Fresno,
Las Vegas, Yuma; northern Baja California optional (see `docker/osrm/coverage.geojson`):
the script downloads ≈ 1.6 GB of Geofabrik extracts (California, Nevada, Arizona), clips
them to that circle and processes the result.  Allow about an hour, ≈ 15 GB of disk and
roughly 4–7 GB of RAM for Docker during processing; a 16 GB laptop is fine with the WSL2
settings in `docker/osrm/README.md`:

```powershell
cd docker\osrm
.\setup.ps1                 # Linux/macOS: ./setup.sh
docker compose up -d
routeopt osrm-check         # -> "OSRM at http://localhost:5000: healthy"
```

`docker/osrm/README.md` documents disk/RAM needs, how to change the coverage radius, how
to refresh the map extract, and troubleshooting.  `OSRM_URL` in `.env` points the app at a different host/port.

### TomTom (optional): truck times with predicted traffic

OSRM gives free-flow car times, so CA LFLT multiplies them by the *traffic factor* (1.3).
For truck-specific times with traffic, set a TomTom key and pick **Travel times = TomTom**
on New Run (`--provider tomtom` on the CLI):

1. Create a free key at <https://developer.tomtom.com> (no card; the free plan is a daily
   request allowance, blocked rather than billed when used up).
2. Add `TOMTOM_API_KEY=...` to `.env` and restart the app.

The whole stop matrix is requested from TomTom Matrix Routing v2 with `travelMode=truck`
(length / height / gross weight from Advanced settings), `traffic=historical` and
`departAt` = the delivery day at *Departure time for traffic* (10:00).  A past delivery day
uses the same weekday in the coming weeks.  Requests are split into blocks of at most
`TOMTOM_MAX_CELLS` (200) origin x destination cells.  The result is cached per day, time
and truck, so re-running the same stops spends nothing.  No key, a refused key, an
exhausted quota or a network error falls back to OSRM x traffic factor with a warning.
Customer coordinates are sent to TomTom: check this is allowed before enabling it.

## 3. Run the app

**CA LFLT pilot, one command** (OSRM container `routeopt-osrm-ca` on port 5001):

```powershell
cd <this folder>
.\start_ca.ps1            # starts OSRM, stops an old app, opens http://localhost:8520
.\start_ca.ps1 -Update    # git pull first (when the folder is a git clone)
```


```powershell
streamlit run app\Home.py
```

The browser opens at <http://localhost:8501>.  Pages (left sidebar):

1. **Trucks & Drivers** — the T01…T10 table with driver names and an active flag.  Every
   run snapshots this table.
2. **New Run** — upload the workbook, review the validation report (errors block the run,
   warnings do not; rows without coordinates are geocoded through the Census batch API and
   cached), pick a preset and settings (every setting has a plain-English tooltip), press
   **Optimize** and watch the elapsed time.  The last-used settings become the next run's
   defaults.
3. **Results** — KPI cards, the map (one color per truck, markers `T03·L1·#4`, road
   polylines from OSRM or straight lines), per-truck loads with ETAs and customer windows,
   the **Unassigned** panel grouped by tier with a reason per stop and a capacity-vs-demand
   summary, and the Excel / PDF exports.
4. **Edit** — move a stop to another load (or a new load), re-sequence, unassign, assign;
   every change is re-validated immediately with the cached travel times and problems are
   highlighted inline.  *Save as new version* (v1, v2, …), *Revert to optimizer result*,
   or keep editing.  Exports are blocked while a version has hard violations unless you
   tick *Export anyway* and give a reason (logged and printed on the export).
5. **History** — every run and version: open, re-export, compare two runs' KPIs, download
   the original upload, soft-delete/restore.

### Command line

```powershell
routeopt sample --shipments 120                    # synthetic workbook in data\samples
routeopt sample --shipments 120 --all-coords       # ...with every lat/lon filled in
routeopt run data\samples\sample_shipments_<D>.xlsx --preset A --plan-date <D> --pull-forward 1,2
routeopt run file.xlsx --preset B --export exports --save     # also writes Excel + PDF and saves the run
routeopt osrm-check
routeopt clear-cache                               # travel-time cache (keeps matrices used by saved runs)
```

## 4. Sample walkthrough

See **DEMO.md** for the 5-minute scripted walkthrough (generate the sample, run Preset A
and Preset B, check the map, export, edit, history).

## 5. Input workbook

One row per shipment; header matching is case-insensitive and whitespace-trimmed, common
aliases are accepted, extra columns are ignored, and a column-mapping step appears when a
required header cannot be matched.

| Column | Required | Notes |
|---|---|---|
| `shipment_id` | yes | unique; must end with `_LMD` (delivery), `_RTN` (return pickup) or `_INC` (installation check) |
| `purchase_order` | no | grouping/reporting only |
| `delivery_date` | yes | D, D+1 or D+2 relative to the plan date; other dates are excluded with a warning |
| `order_type` | yes | `OBS` / `BUILDER` / `LTL` |
| `customer_name`, `address`, `city`, `state`, `zip` | yes | zip kept as text |
| `phone`, `notes` | no | printed on route sheets |
| `latitude`, `longitude` | no | blank → geocoded with the Census batch geocoder |
| `model_code`, `weight_lb`, `volume_cuft` | LMD/RTN | totals for the shipment |
| `pieces` | yes | ≥ 1 for LMD/RTN, 0 allowed for INC |
| `service_minutes` | yes | install / pickup / check time |

CA LFLT uploads may also carry (and need none of `model_code`, `weight_lb`, `pieces`,
`service_minutes`, nor the `_LMD`/`_RTN`/`_INC` suffix — every row is a delivery):

| Column | Notes |
|---|---|
| `origin_dc` | `LGC2`, `LGC7` or `LGC8` (aliases: `DC`, `FRST_SHPG_LOC_CD`, …); blank = LGC2 |
| `pallet_required` | `Y` / `N`, or the product group (aliases `HQ`, `product group`): `HA` = no pallet, every other group (`AC`, `ES`, `BS`, `MS`, …) = pallet; a load with any pallet shipment stays below 400 cu ft |
| `priority` | `1` existing LFLT, `2` add-on candidate, `3` low priority (the stop's tier) |
| `declared_value` | Optional. Declared value per shipment; summed per load in the **Load Summary** export (DECLARED VAL) |
| `Memo Text` | merged into notes; a phone / e-mail in it becomes the stop's appointment contact |

Rows sharing an address (+ zip) become one **stop**, always served together by one load
(per DC: the same address served from LGC2 and from LGC8 is two stops).

## 6. Settings reference

| Setting | Default | Meaning |
|---|---|---|
| Delivery day (D) | today + 2 | the day being planned; runs happen on D-2 |
| Pull-forward pools | none | also try D+1 and/or D+2 shipments if they fit without displacing a D stop (D+1 preferred) |
| Trucks available | all active | subset of the Trucks & Drivers table |
| Shift start | 08:00 | trucks leave pre-loaded, zero loading time |
| On-duty hours | 10 | shift start → return of the last load, incl. driving, service, buffer, reloads and lunch |
| Truck volume | 600 cu ft | never exceeded at any point (deliveries leave, returns board) |
| Weight / pieces cap | off | optional hard caps; always reported |
| Stop buffer | 20 min (CA: 0) | once per stop |
| Stops in transit | CA: 20 min | added to every stop-to-stop drive |
| Reload | 60 min | between loads of the same truck (unload returns, load the next set) |
| Lunch | 30 min | during a reload when the truck has 2+ loads, otherwise an explicit gap |
| Lunch window | 11:00–14:00 start | preferred window for the gap; clear for "any time" |
| Max loads per truck | 3 | upper bound for the model |
| Preset | A | **A** fewest trucks (fixed cost 20,000 per truck), **B** balanced (span cost 10), **Custom** |
| Solve time | 60 s | 10–60; the app always returns a plan (possibly partial) or a clear message |
| ETA window | ± 2 h | customer window, rounded to 15 min, never before shift start |
| Travel times | auto | OSRM if healthy, else straight-line; or force either |
| Pull-forward value | D+1 240 min, D+2 120 min | a pull-forward stop is added if it costs less than this much extra driving |

Drop priority when not everything fits: D-OBS stops first, then D-BUILDER, then D-LTL,
then D+1, then D+2 (a config validator enforces the penalty ordering).

## 7. How the plan is checked

An independent pure-Python **validator** rebuilds every truck's timeline and volume
profile from the plan and the cached travel matrix, checks the hard rules (hub-to-hub,
on-duty ≤ limit, volume ≤ cap at every point, lunch, one load per stop, load count), and
flags any drift from the optimizer's own times.  The validator — not the solver — is the
source of truth: it runs after every optimization, after every manual edit and before
every export.  Unassigned reasons (*Out of volume*, *Out of time*, *Not placed within
the solve time*, *Not worth it* for pull-forwards, *Priority anomaly*) are derived by
trial insertions through the same validator.

## 8. Known limitations

* OR-Tools with a time limit is **not perfectly repeatable**: re-running may give a
  slightly different plan.  Every run is saved, so nothing is lost.
* No traffic modeling; OSRM uses free-flow speeds from OpenStreetMap.  Straight-line
  estimates (fallback) can be optimistic in the metro core and pessimistic on highways.
* The Census geocoder needs internet access and matches street addresses only (no
  intersections/POIs).  Unmatched rows can be excluded from the run in the UI.
* The lunch window is honored by the timeline engine's placement rule and checked by the
  validator (warning if it cannot be met); the optimizer itself budgets the time rather
  than choosing the exact minute (see `ASSUMPTIONS.md`).
* Several pull-forward stops can jointly justify opening a truck under Preset A (each is
  worth 240/120 minutes of driving; a truck costs 333 minutes).
* Map tiles come from the public OpenStreetMap tile servers.  This is fine for an
  internal app used by a few planners; a high-traffic deployment should switch to a tile
  provider that allows it (see <https://operations.osmfoundation.org/policies/tiles/>).
* Windows 11 with Docker Desktop is the target desktop; the app itself has no bash
  dependencies.  Postgres / SQL Server work through `ROUTEOPT_DB_URL` (run
  `alembic upgrade head` to create the schema there; SQLite is created automatically).

## 9. Repository layout

```
app/                     Streamlit pages (no business rules)
routeopt/config.py       AppConfig + RunSettings + penalty invariants
routeopt/domain.py       Shipment, Stop, Truck, Load, StopVisit, Plan, Violation, KPIs
routeopt/ingest/         Excel loader, row validator, Census geocoder, stop consolidation
routeopt/travel/         provider interface, OSRM client, haversine fallback, cache
routeopt/solver/         OR-Tools model, greedy seed, staged search, extraction, validator, reasons
routeopt/edit/           manual operations, re-validation, export gate
routeopt/export/         Excel, PDF route sheets, Folium map
routeopt/db/             SQLAlchemy models, repository, Alembic migrations
routeopt/pipeline.py     end-to-end pipeline + background job runner
routeopt/cli.py          `routeopt` command
docker/osrm/             OSRM compose + setup scripts (+ the built routing data in data/)
start.sh, start.ps1      one-shot launchers: OSRM container + app (see DEPLOY.md)
data/samples/            sample generator (+ generated workbook)
tests/                   pytest suite (offline; OSRM mocked)
```

Quality gates: `ruff check`, `ruff format --check`, `mypy routeopt`, `pytest` with
coverage ≥ 80 % on `routeopt/`.

## 9. Deploying to another machine

`DEPLOY.md` describes the zip that carries the source plus the pre-built OSRM dataset,
and how to run it on a Linux VM or a Windows box with `start.sh` / `start.ps1`.
