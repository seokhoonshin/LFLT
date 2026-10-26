# DEMO.md — 5-minute walkthrough (manual acceptance, brief §15.14)

Target machine: Windows 11 with Docker Desktop.  Times assume the venv is active and the
OSRM container has been prepared once (`docker\osrm\setup.ps1`, see README §2).

## 0. Before you start (once)

```powershell
cd docker\osrm
docker compose up -d
cd ..\..
routeopt osrm-check              # expect: "OSRM at http://localhost:5000: healthy"
routeopt sample --shipments 120  # writes data\samples\sample_shipments_<D>.xlsx for D = today + 2
streamlit run app\Home.py
```

The sample has 120 shipments (~73 stops): ~70/20/10 OBS/BUILDER/LTL, ~80/12/8 LMD/RTN/INC,
several shared addresses and POs, dates on D/D+1/D+2, a 588 cu ft builder stop, and a
few rows with **blank coordinates on real public addresses** (San Bernardino County
Government Center, Riverside County Administrative Center, …) so you can watch the Census
geocoder fill them in.

## 1. Trucks & Drivers (20 s)

Open **Trucks & Drivers**, type a driver name for T01–T06, untick T10 (out of service),
press **Save trucks**.

## 2. New Run — Preset A (90 s)

1. **New Run** → upload `data\samples\sample_shipments_<D>.xlsx`.
2. Validation report: 120 rows read, 0 errors, a few warnings (e.g. the 588 cu ft stop's
   long service time), *Geocoded rows* > 0, and the summary line
   **"120 shipments → 73 stops"** with counts by day and tier.
3. Settings: leave **Preset A**, delivery day = today + 2 (matches the sample), tick
   **D+1** in *Pull-forward pools*, solve time 60 s.  Hover any setting for its tooltip.
4. Press **Optimize**.  The progress bar counts the seconds; the run is saved and the app
   jumps to **Results** when done (about 65 s).

## 3. Results (60 s)

* KPI cards: trucks used, loads, stops served, D stops served %, unassigned, fleet volume
  use, drive time, miles, on-duty averages, pull-forward included.
* The map shows the hub, one color per truck, markers like `T03·L1·#4` and **road
  geometry from OSRM** (straight dashed lines only if OSRM were down).  Toggle trucks in
  the layer control.
* Expand a truck: each load is a table with ETA, customer window (ETA ± 2 h, rounded to
  15 min), address, volume after the stop and the lunch marker.  A truck with the 588 cu ft
  stop runs it alone on one load and reloads for a second load; its lunch is taken during
  the reload.
* **Unassigned stops**: grouped by tier with counts and volumes, a plain-English reason per
  stop (*Out of time*, *Out of volume*, *Not worth it* for pull-forwards) and the
  capacity-vs-demand line.
* Press **Export Excel** and **Export PDF route sheets**; download both.  The Excel has
  Summary / Routes / Stops / Unassigned / Trucks / Settings; the PDF has a fleet overview
  page and one page per load.

## 4. New Run — Preset B (75 s)

Back to **New Run**, same file, choose **B — Balanced**, Optimize.  Results now use all
selected trucks with similar return times (compare *Longest day* and *Avg on duty* with
the Preset A run on the History page).

## 5. Edit (60 s)

1. Results → **Edit this plan**.
2. Tab *Move to another load*: pick a stop and move it to a truck whose load is already
   full — the row turns red with *"After stop n the truck would carry … cu ft (max 600)"*
   and the export buttons are blocked with the reason.
3. Tick **Export anyway**, type a reason, export — the reason is logged and printed on the
   Summary sheet / PDF footer.
4. **Revert to optimizer result (v0)**, then do a harmless change (re-sequence two stops)
   and **Save as new version** → v1.

## 6. History (20 s)

**History** lists both runs with D, preset, trucks used, stops, unassigned and the version
count.  Open a run, pick v0 or v1, re-export, compare the two runs' KPIs side by side,
download the original upload, and (optionally) delete a run — it is only hidden and can
be restored.

## What "good" looks like

* Every plan passes the validator (no red banner on Results); the Summary sheet says
  *VALIDATOR: no violations*.
* No D stop is unassigned while a lower-tier stop is served (the *Priority anomaly* list is
  empty).
* With OSRM up, the map shows road polylines and the Summary sheet says
  *Travel times: OSRM road network*.  If the OSRM container is stopped, the persistent
  banner appears, straight lines are drawn and every export carries the ESTIMATED warning.
