# ASSUMPTIONS.md — interpretations and deviations from the build brief

Every item below is either an interpretation of something the brief leaves open, or a
deliberate deviation.  Items are grouped by milestone; "(deviation)" marks the latter.

## CA LFLT profile

The rules are listed in `docs/CA_LFLT_RouteOpt_Conditions_EN.md`; this is how each is
modelled.  `RunSettings.profile` (`ca_lflt` default, `legacy` = the original brief) fills
every setting a run does not give explicitly; a run saved before profiles existed is read
as legacy.

- **Sites.** LGC2 and LGC7 (5550 Mango Ave) are one site at the legacy hub point; LGC8 is
  5267 Sierra Ave, Fontana (OpenStreetMap house point 34.1441316, -117.4359977).  Every
  site is a depot node ahead of the stops, used or not, so node numbers never shift.  A
  stop is keyed by address + ZIP *per site*.  A load leaves from and returns to the site
  of its stops; a later turn at another site first drives DC-to-DC.
- **Vehicles.** One OR-Tools vehicle per (truck, turn, site, trip kind); at most one of
  each (truck, turn) runs and turn k+1 only if turn k does.  A stop may only ride on its
  site's vehicles (`VehicleVar.RemoveValues`).  Unused depot nodes are forced inactive.
- **Volume.** "Less than" limits: 799.9 / 399.9 cu ft in the solver.  A *pallet trip* is a
  load with any pallet shipment; pallet stops only ride on pallet-trip vehicles, other
  stops may join them (rule 7: mixed loads take the pallet limit).  `pallet_required`
  takes Y / N or the product group: HA ships without a pallet (< 800), every other
  group (AC, ES, BS, MS, ...) needs one (< 400).
- **Time.** Shift 06:00; turn 1 departs after `first_load_minutes` (60).  Stops are not
  served before 08:00 (the truck waits; the wait counts on duty).  Stop time = 20 min +
  round(cu ft x 0.1) per stop; the upload's `service_minutes` is ignored.
- **Turns.** Turn k+1 departs at the later of (return + DC-to-DC + reload) and (turn k's
  last delivery = departure from its last stop + `turn_gap_minutes` 180).  A twin of the
  time dimension (`last_dep`, return arcs carry no travel) gives the solver that
  departure.  Lunch during the reload as before.
- **Distance.** One way = road miles from the DC to a load's last stop (< 200, a
  dimension); 150-200 mi is a *carrier check* warning.  A truck that runs two turns keeps
  the miles of all its turns < 300; a single-turn truck has only the one-way limit.
- **Two Stop Rule (50 mi = 2 h).** Off, as decided: transit is OSRM x traffic factor;
  stop count is the goal.
- **Closest orders first.** Not a constraint: the presets / first-solution strategy
  cover it.
- **Priority.** Upload `priority` 1/2/3 is the D-stop tier (existing tier penalties).
- **OBS.** `obs_mode`: `last` (plan the rest first, then add OBS-only stops to existing
  loads within `obs_radius_miles` (5, straight line) of a planned non-OBS stop, cheapest
  extra time first, never removing a stop or opening a load), `exclude`, or `normal`.
- **Appointment contact.** The first phone / e-mail in Memo Text or notes (with the name
  after "call / contact / ask for / attn" when present), otherwise "Look up the official
  contact by address".
- **Drift check.** A short route that waits for the lunch window on its way back is not a
  drift (the solver budgets the lunch, not the wait).
- **Trucks.** The CA profile selects T01 and T02.
- **Traffic.** OSRM times are free-flow: CA LFLT plans on OSRM x `traffic_factor` (1.3;
  legacy 1.0); distances are not scaled.  Runs save the raw provider matrix (the travel
  cache is shared) and `planning_matrix()` re-applies the run's factor and same-point floor
  whenever a run is planned or reopened.
- **TomTom.** Optional travel source (`travel_provider = "tomtom"`): Matrix Routing v2,
  truck mode, historical traffic at one representative departure time per run (the
  routing model has one time per arc, so time-of-day variation is not modelled).  Its
  times already include traffic, so the traffic factor is not applied.  The matrix source
  is `tomtom:<hash of departure and truck>` so cache hits only reuse the same day, time and
  vehicle.  Map polylines still come from OSRM (drawing only).  TomTom's truck dimensions
  default to a 26 ft box truck, 13.5 ft high, 26,000 lb - confirm for the pilot trucks.

## General

- **Repo location.** The repository is delivered as `routeopt/` inside the connected
  "Smart Logistics [2026] - proj" folder, plus a zip of the same tree.
- **Python version.** Code targets Python 3.12 (`requires-python >= 3.12`) and was developed
  and tested on CPython 3.12.  Nothing in it is 3.12-specific beyond typing syntax, so 3.11
  will most likely also work, but it is not tested.
- **Hub coordinates.** The hub (5550 Mango Ave, Rialto, CA 92377) is fixed at
  34.1511 N, 117.4319 W: the ArcGIS point-address match for the street address, confirmed
  against the OpenStreetMap road network (nearest road = Mango Avenue, under 1 m).  The US
  Census geocoder has no address ranges on that stretch of Mango Ave (the newer logistics
  park north of the 210), so the hub cannot be geocoded at run time and is a constant in
  `routeopt/domain.py`; change it there if the yard moves, then rebuild the OSRM data
  (`docker/osrm/README.md`).  Mango Ave is the Fontana/Rialto city line, so some geocoders
  label the parcel "Fontana 92336"; that does not affect routing.
- **Clock.** Every "today" (the D-2 run day, the default delivery day) and every audit
  timestamp (runs, versions, edits, exports) is taken at the hub (`HUB_TIMEZONE`,
  America/Los_Angeles) via `hub_now()` / `hub_today()` in `routeopt/domain.py`, not from the
  machine's clock, so a planner or server in another time zone plans and logs in the hub's
  day.  Timestamps are stored naive, in hub local time.
- **Nondeterminism.** OR-Tools with a time limit is not perfectly deterministic; the UI and
  README say "re-running may give a slightly different plan".  Tests therefore assert
  properties (feasibility, tier ordering, limits) rather than exact routes.

## M1 — Foundation

- **Required columns.** `model_code`, `weight_lb` and `volume_cuft` are treated as required
  *columns* (they are required for every `_LMD`/`_RTN` row, which is the bulk of every file);
  the *values* may be blank on `_INC` rows.  If the columns are missing entirely the
  column-mapping step is shown.
- **ZIP handling.** ZIPs are kept as text; numeric ZIPs are zero-padded to 5 digits; ZIP+4
  is kept as typed but only the 5-digit part is used in the stop-consolidation key and the
  geocoder query.
- **Stop key punctuation.** The consolidation key strips periods and commas in addition to
  the brief's upper-casing and whitespace collapsing ("100 Main St." and "100 Main St"
  are the same stop).  Everything else (e.g. "St" vs "Street") is left alone and, if the
  coordinates are within 10 m, surfaced as the "same coordinates, different address text"
  warning.
- **Coordinate sanity.** A latitude/longitude that is non-numeric, out of range, or exactly
  0 is treated as blank (row is geocoded) with a warning, rather than trusted.
- **Return with zero volume.** An `_RTN` row with `volume_cuft = 0` is accepted with a
  warning ("nothing to pick up?"); the brief only specifies the `_INC` case explicitly.
- **Day tag of a mixed stop.** A stop is tagged with the *earliest* day among its
  shipments (a D+1 shipment at a D address rides along on D).  Tier is computed from the
  tag and the order types of *all* its shipments.
- **Census geocoder errors.** If the Census service is unreachable after retries, the rows
  are reported as "could not be geocoded" (hard error with the exclude-and-continue
  option) rather than crashing the run.
- **Sample data — real addresses.** The rows generated with blank lat/lon use real public
  addresses (the San Bernardino County Government Center, the Riverside County
  Administrative Center, Fontana / Ontario / Rancho Cucamonga city halls, Cal State San
  Bernardino) with fictional customer names, so the live Census geocoder can match them
  during the demo; each was verified against the Census batch benchmark.  Newer
  subdivisions in the Inland Empire are often missing from the Census (TIGER) data, so
  real builder uploads should carry coordinates where possible.
  All other sample addresses are synthetic street names with synthetic coordinates.
- **Sample mix.** The generator targets the brief's mixes statistically (≈70/20/10 order
  types, ≈80/12/8 shipment types); small files (e.g. 30 rows) will drift from the targets.
- **Lunch window default (deviation).** `lunch_window` defaults to "may start between
  11:00 and 14:00" instead of "any time in shift".  With no cost on break placement the
  solver would otherwise put lunch at 08:00 in the yard.  Clearing both fields in the UI
  restores "any time".
- **Pull-forward penalty scaling.** Under a preset with a span cost (Preset B, or Custom
  with a span coefficient), the pull-forward penalty is multiplied by (1 + span
  coefficient) so the tooltip "added if it costs less than N minutes of extra driving"
  stays approximately true; under Preset A the penalty is exactly N × 60 as in the brief.
- **Tier-3 invariant.** In addition to the brief's rule (tier 3 > Σ fixed truck costs + T ×
  on-duty seconds) the config validator checks the *marginal* bound tier 3 > fixed cost +
  on-duty seconds × (1 + span coefficient), which is the one that actually prevents a D
  stop from being dropped for cost under Preset B.

## M2 — Travel

- **Cache order-independence.** The cache key is the hash of the sorted, 5-decimal-rounded
  coordinate set; the matrix is stored in that canonical order and permuted back on read,
  so any run over the same points is a hit regardless of stop order.
- **OSRM chunking.** Blocks of `max_table_size // 2` sources x destinations are requested
  with `sources=`/`destinations=` so no single request exceeds the server's table limit;
  unroutable pairs (`null` in the OSRM answer) are filled with straight-line estimates and
  reported in the matrix notes.
- **Health check.** `GET /nearest/v1/driving/<hub>` with a 3-second timeout; used at app
  start and before every run.
- **OSRM coverage.** The server built by `docker/osrm` carries the road network inside a
  250-mile circle around the hub (`docker/osrm/coverage.geojson`: southern and central
  California up to Fresno, southern Nevada with Las Vegas, western Arizona with Yuma and
  Kingman; northern Baja California optional).  250 rather than 500 miles because the
  planner's machine has 16 GB of RAM; the radius is one parameter (`make_coverage.py
  --miles`) plus the Geofabrik region list in the setup scripts.  A stop outside the
  circle is unroutable for OSRM and gets a straight-line estimate (see *OSRM chunking*);
  with the default 75-mile coverage radius the validator has already flagged it.

## M3/M4 — Solver

- **Lunch formulation (deviation from 7.5).** The optional-break-interval formulation
  (`FixedDurationIntervalVar` + `SetBreakIntervalsOfVehicle` + "performed(b) + active(slot
  2) >= active(slot 1)") proved unstable: as soon as a global span cost is present (Preset
  B, or any span coefficient) every constructed solution was rejected and the search never
  left "all stops dropped"; it also produced solver/validator time drift of several minutes
  through slack placement.  `lunch_mode = "smart"` therefore models lunch as a *time
  budget*: load 1's return arc carries `lunch_minutes`, and slot >= 2 start arcs carry
  `reload_minutes - lunch_minutes` (so a second load pays exactly the reload, lunch being
  taken during it).  The timeline engine places the actual gap deterministically: during
  the reload for multi-load trucks, otherwise on the best arc for the lunch window (an arc
  departing inside the window; else an arc whose drive spans the window start, so lunch
  interrupts the drive without waiting; else the arc closest before the window, with the
  driver waiting for it to open; else the earliest arc after the window, which the
  validator flags).  The validator checks the
  business rule directly (a >= 30-min gap with no service, or a reload) and flags lunches
  outside the preferred window as warnings.  `lunch_mode = "simple"` keeps the brief's
  interval-based mandatory break on slot 1 (over-conservative for multi-load trucks; it
  can show harmless drift warnings because OR-Tools places slack around the break).
- **Lunch after a short route.** If a truck's day ends before the lunch window opens, the
  gap is placed on the last arc and starts when the window opens (the driver waits); the
  on-duty span counts up to the end of that lunch.
- **Solver-vs-validator drift check.** In smart mode the solver's cumul values exclude the
  lunch gap by construction; the validator translates its rebuilt times to the solver's
  frame (+/- the lunch length depending on whether the gap comes before the visit) before
  comparing with the 60-second tolerance.
- **Initial solution (addition).** OR-Tools' first-solution heuristics are unaware of the
  load-slot symmetry (slot 2 may only be used when slot 1 is), so their solutions were
  routinely rejected and the search had to build a plan from "everything dropped", which
  took 5-10 s on 70 stops and could exceed the budget on 300.  A slot-aware sweep-and-
  append greedy (using the validator's timeline engine) now seeds the search; the built-in
  heuristic is only used if the greedy start is rejected.
- **Fleet reduction stage (addition).** Guided local search penalises arcs, not vehicle
  usage, so under Preset A it can leave a lightly used truck in place on small instances
  (e.g. five stops on two trucks when one suffices).  After the main search (65% of the
  budget) the solver repeatedly tries to close the least-used truck (rebuild with that
  truck disabled, resume from the current routes, keep only if the objective improves),
  then spends any remaining time polishing.  Total wall-clock stays within
  `solve_time_seconds`.  Reported in `solver_stats.fleet_reduction`.
- **First-solution strategy.** `PARALLEL_CHEAPEST_INSERTION` and `PATH_CHEAPEST_ARC` behave
  the same once the greedy seed is used (both are only fallbacks); PCI is kept as the
  default because it produced solutions more often when the seed was rejected.
- **Pull-forward economics.** The invariant "a pull-forward penalty is below the Preset A
  truck cost" is per stop, as in the brief.  Several pull-forward stops can jointly justify
  opening a truck (two D+1 stops are worth 480 min > the 333-min truck cost).  If the
  business never wants a truck opened for pull-forwards alone, lower
  `pull_forward_value_minutes` or ask for a hard constraint (not implemented).
- **Pull-forward scope.** D+1/D+2 stops are only part of the model when their day is
  enabled in `pull_forward_days`; otherwise they are listed under "not in scope" rather
  than "unassigned" so unassigned KPIs describe real overflow only.
- **Unassigned reasons.** Insertion trials append/insert the stop at every position of
  every load (and as a new load where a slot is free); the priority-anomaly check tries
  single swaps with a lower-priority served stop (bounded to 5,000 trials) - it is a bug
  signal, not a proof.  "Out of time" is reported when at least one trial passed the
  volume check but all failed on time; "Out of volume" when every trial failed on volume.
- **Solve time lower bound.** The settings model accepts 1-60 s (the UI slider offers
  10-60) so the offline tests can run short solves.
- **Solver status names.** OR-Tools 9.15 reports `ROUTING_PARTIAL_SUCCESS_LOCAL_OPTIMUM_
  NOT_REACHED` when the time limit stops the search before a local optimum; the UI shows it
  as "stopped at the time limit".
- **Sample data service times.** Two-piece shipments take 1.5x (not 2x) the single-piece
  service time so a 120-shipment sample is only moderately over capacity.

## M5 — UI

- **Background solve.** OR-Tools holds the Python GIL while searching, so a worker *thread*
  would freeze the page.  The optimizer runs in a separate *process* (`OptimizeJob`,
  spawn context) and the New Run page polls it, showing elapsed seconds and a Cancel
  button; the page falls back to a thread only if a process cannot be started.  While the
  worker starts, the parent's `__main__` module is swapped for an empty one so the spawned
  child does not re-execute the Streamlit page script (multiprocessing's spawn re-runs the
  parent's main file by default).
- **Map embedding.** The Folium map is embedded with Streamlit's HTML component at a fixed
  height (stable layout even when the map's JavaScript loads slowly or a corporate proxy
  blocks it).  `ROUTEOPT_MAP_COMPONENT=st_folium` switches to the streamlit-folium
  component; both render the same Folium map from `routeopt/export/mapping.py`.
- **Road geometry cache.** Route polylines are fetched from OSRM per load and cached in the
  session; when OSRM is down (or the run used estimates) straight lines are drawn and a
  caption says so.
- **Capacity vs demand.** The summary counts D stops only ("delivery volume X vs T × cap
  across T trucks × 1 load; service minutes X vs T × (on-duty − lunch)"), because extra
  loads add capacity only at the cost of reload time.
- **Column mapping.** When required headers are missing, every canonical column gets a
  selectbox over the workbook's headers; the mapping is remembered for that upload.
- **Excluding rows.** Rows the Census geocoder could not match can be excluded with one
  click ("Exclude the N row(s) … and continue"); the exclusion is recorded in the run's
  warnings.

## M6 — Exports

- **Routes sheet round trip.** Shipment fields on the Routes sheet use the exact input
  header names so the sheet re-imports through the loader; the shipment type column is
  named `shipment_type` (not `type`) because `type` is an alias of `order_type` in the
  header matcher.
- **Time cells** are real Excel times formatted `h:mm AM/PM`; ZIP and phone are text.
- **PDF** is landscape Letter, one page (or more) per load plus a fleet overview page;
  lunch rows are printed inside the stop table where the gap falls.

## M7 — Editing

- **Structure vs. timeline.** Operations change only which stop sits where; times,
  windows, volumes, KPIs and violations are recomputed by the shared timeline engine and
  validator.  After an edit the lunch gap is re-placed by rule (a truck that gains a
  second load takes lunch during the reload), and solver-time drift checks are dropped
  because the solver never saw the edited structure.
- **Unassigned reasons after edits.** Stops taken off a route by the planner are listed as
  *Removed from a route by the planner*; stops the optimizer left unassigned keep their
  original reason.  Out-of-scope pull-forward stops stay out of scope unless the planner
  assigns one, which is allowed.
- **Export override.** A version with hard violations (volume, on-duty, lunch, reload,
  duplicate stop) blocks exports unless *Export anyway* is ticked with a reason; the
  reason is stored on the export record, written to the edit log and printed on the
  Summary sheet and every PDF page.
- **Version numbering.** Versions are append-only per run (v0 = optimizer, v1… = planner);
  saving from an older version still creates the next number.

## M8 — Persistence

- **Schema portability.** Only String/Integer/Float/Boolean/Text/Date/DateTime/LargeBinary
  columns; JSON documents are stored as text.  SQLite is created automatically
  (`create_all`); Alembic (`alembic upgrade head`, URL from `ROUTEOPT_DB_URL`) carries
  the same schema for Postgres / SQL Server.
- **Full plan JSON + normalized rows.** Each version stores the complete Plan as JSON
  (exact round trip) *and* the normalized `loads` / `load_stops` / `unassigned` rows the
  brief asks for (reporting/queries).
- **Travel matrix storage.** The run references its matrix in `travel_cache` by
  provider + coordinate hash; `clear-cache` never removes a matrix a saved run needs.
  If a matrix is missing (older database), opening the run reports it and asks for a
  re-run.
- **Trucks snapshot.** `run_trucks` stores the truck/driver table used by the run;
  exports and route sheets use that snapshot, never the live table.
- **Deleting a run** only sets a flag (`deleted`, `deleted_at`); History can show and
  restore deleted runs.
- **Last-used settings** are stored in `app_state` and become the next run's defaults.
- **Census geocoder cache** lives in `geocode_cache` keyed by the normalized address.
