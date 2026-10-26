# OSRM for RouteOpt

RouteOpt asks a local [OSRM](https://project-osrm.org/) server for road travel times
(`/table`) and route polylines (`/route`).  Everything here is free and open source:
OSRM (BSD-2), [osmium-tool](https://osmcode.org/osmium-tool/) (GPL-3) and OpenStreetMap
data via Geofabrik (ODbL).

## What the server covers

The road network within **250 miles of the hub** (5550 Mango Ave, Rialto, CA —
34.1511 N, 117.4319 W).  `coverage.geojson` is that circle.  The setup script downloads
the Geofabrik extracts the circle touches, keeps only what OSRM's car profile reads
(highway ways, ferry routes, turn restrictions, road-route relations), merges them and
clips the result to the circle (`osmium/prepare.sh`), then runs the OSRM pipeline on it.

| Geofabrik region | Download | Inside the circle |
|---|---|---|
| `us/california` | 1.2 GB | everything south of ≈ 37.8 N and east of ≈ 121.8 W: LA, Orange County, San Diego, the Inland Empire, Palm Springs, Santa Barbara, San Luis Obispo, Bakersfield, Visalia, Fresno, Bishop (Monterey, San Jose and the Bay Area are outside) |
| `us/nevada` | 118 MB | the southern tip: Las Vegas, Henderson, Laughlin (Tonopah and Reno are outside) |
| `us/arizona` | 289 MB | the western strip: Yuma, Lake Havasu City, Kingman (Phoenix, Prescott and Flagstaff are outside) |
| `mexico` (optional: `-IncludeMexico` / `INCLUDE_MEXICO=1`) | 617 MB | northern Baja California: Tijuana, Ensenada, Mexicali (San Felipe is outside) |

Utah's nearest corner is 273 miles away, so no other state is needed.  A stop outside
the circle is unroutable for OSRM and gets a straight-line estimate (`ASSUMPTIONS.md`,
M2); with the default 75-mile coverage radius the validator has already flagged it.

The radius was chosen for a 16 GB planner machine; the geometry is a single parameter.
A 500-mile circle (all of California, Reno, Phoenix, Tucson, St. George) would add
`us/utah` and `us/new-mexico` to the region list, roughly double the download and
processing, and need 6–10 GB of RAM during `osrm-extract`.

### Changing the hub or the radius

1. Update `HUB_LAT` / `HUB_LON` in `routeopt/domain.py` (or pass `--lat` / `--lon`).
2. `python docker/osrm/make_coverage.py --miles 250` rewrites `coverage.geojson` and
   prints the new bounding box.
3. Check the region list at the top of `setup.sh` / `setup.ps1` still matches the circle
   (a larger radius may need more states; the circle's bounding box is printed in step 2
   and stored in the file's properties).
4. Update the hub coordinates in the health checks (`docker-compose.yml`, the last lines of
   both setup scripts).
5. Re-run the setup script, then `docker compose up -d`.

## One-time setup

1. Install Docker Desktop (Windows 11) or Docker Engine (Linux/macOS) and give it enough
   memory for `osrm-extract` (see *Resources*).
2. Prepare the map (downloads ≈ 1.6 GB; allow about an hour end to end):

   ```powershell
   # Windows (PowerShell)
   cd docker\osrm
   .\setup.ps1                 # add -IncludeMexico for northern Mexico
   ```

   ```bash
   # Linux / macOS
   cd docker/osrm
   ./setup.sh                  # INCLUDE_MEXICO=1 ./setup.sh for northern Mexico
   ```

3. Start the server:

   ```bash
   docker compose up -d
   ```

4. Check it:

   ```
   http://localhost:5000/nearest/v1/driving/-117.4319,34.1511
   ```

   should return JSON with `"code":"Ok"`.  RouteOpt runs this same health check on app
   start and before every run; when it fails the app falls back to straight-line estimates
   and shows a persistent banner.  To confirm the coverage, route from the hub to Las
   Vegas (≈ 225 miles, roughly 3½ hours free-flow):

   ```
   http://localhost:5000/route/v1/driving/-117.4319,34.1511;-115.1398,36.1699?overview=false
   ```

`OSRM_URL` in `.env` (default `http://localhost:5000`) tells RouteOpt where the server is.

## Resources

Estimates for the 250-mile dataset — measure on the first run.  Published OSRM experience
is the guide: a whole US state builds on a machine with 4 GB of RAM and serves in
1–3 GB, a Germany-sized graph needs 8–16 GB to build and 3–5 GB to serve.  The 250-mile
set (Southern and Central California plus Las Vegas and western Arizona) is a little
more than one state's worth of roads.

| | |
|---|---|
| Downloads | ≈ 1.6 GB (≈ 2.2 GB with Mexico), kept in `docker/osrm/data` for refreshes |
| Clipped road-only extract | ≈ 0.3–0.5 GB (`data/routeopt-coverage.osm.pbf`) |
| Disk after processing | ≈ 6–10 GB in `docker/osrm/data` (`.osrm*` files) — keep 15 GB free |
| RAM during `osrm-extract` | ≈ 4–7 GB peak; the osmium steps and `osrm-partition` / `osrm-customize` need less |
| RAM while serving | ≈ 1.5–3 GB, or close to nothing with `--mmap` (below) |
| Time | about an hour: downloads, osmium filter/merge/clip (≈ 10 min), OSRM (20–45 min) |
| Table limit | `--max-table-size 2000` (RouteOpt chunks requests above this anyway) |

For comparison the previous Georgia-only build was a 300 MB download, ≈ 3 GB of disk and
4–6 GB of RAM.

### On a 16 GB machine

Docker Desktop on Windows runs inside a WSL2 virtual machine that gets half the RAM by
default (8 GB), which should be enough for the 250-mile build.  To be safe, give it 10 GB
plus a swap file for the build: create `%USERPROFILE%\.wslconfig` with

```
[wsl2]
memory=10GB
swap=16GB
```

then run `wsl --shutdown` in PowerShell and restart Docker Desktop.  Close the RouteOpt
app and other heavy programs while the build runs.  If `osrm-extract` still dies
(`std::bad_alloc`, `Killed`, exit code 137), shrink the circle — 150 miles still covers
Los Angeles, Orange County, San Diego, the Inland Empire, Bakersfield and Palm Springs —
and re-run; the downloads are reused:

```powershell
python docker\osrm\make_coverage.py --miles 150
cd docker\osrm
.\setup.ps1 -Regions us/california          # Nevada and Arizona are both > 150 mi away
```

For day-to-day use the server has to share the 16 GB with Streamlit and the OR-Tools
solver.  Either set `memory` back to 8 GB after the build, or add `--mmap` after `mld` in
the `osrm-routed` command in `docker-compose.yml`: the graph is then paged from disk
instead of loaded into RAM (queries are a little slower, which RouteOpt's travel-matrix
cache mostly hides; it works best when `docker/osrm/data` sits on the WSL2 disk rather
than a Windows folder).

The pipeline is `osmium tags-filter` → `osmium merge` → `osmium extract --polygon` →
`osrm-extract -p /opt/car.lua` → `osrm-partition` → `osrm-customize` (MLD algorithm),
served with `osrm-routed --algorithm mld --max-table-size 2000 --port 5000`.  The osmium
steps run in a tiny Debian image built locally from `osmium/Dockerfile`.

## Refreshing the map

Geofabrik updates the extracts daily.  To refresh:

```powershell
docker compose down
.\setup.ps1 -ForceDownload     # or:  FORCE_DOWNLOAD=1 ./setup.sh
docker compose up -d
```

Cached travel matrices in RouteOpt's database are keyed by provider + coordinates, not by
map version; clear the `travel_cache` table (History page → "Clear travel cache", or
`routeopt clear-cache`) after a refresh if you want new road data to take effect for
already-planned stop sets.

## Troubleshooting

* `port is already allocated` — another service uses 5000; change the left side of
  `"5000:5000"` in `docker-compose.yml` and set `OSRM_URL` accordingly.
* `std::bad_alloc`, `Killed` or exit code 137 during `osrm-extract` — raise Docker's
  memory limit and add swap, or shrink the circle (see *On a 16 GB machine*).
* The osmium step is slow on Windows — bind mounts from the Windows file system are slow
  under WSL2; moving the repo (or just `docker/osrm/data`) onto the WSL2 disk helps.
* `docker build` of the helper image fails — it needs internet access to Debian's package
  mirrors; retry, or build it once on a machine that has access and `docker save` / `load` it.
* Download all regions in one session.  Geofabrik rebuilds every extract in the same daily
  run, so files fetched together carry identical border objects, which `osmium merge`
  writes once; files from different days can carry two versions of a border way (both end
  up in the dataset — harmless for routing, but `-ForceDownload` cleans it up).
* Requests time out on the first call after start — the server loads several GB; wait a
  minute (the compose health check allows 2 minutes) and retry.  RouteOpt retries with
  backoff automatically.
* A stop far from the hub gets a straight-line estimate although OSRM is up — it is
  outside the coverage circle (see the table above) or on a road not connected to the
  network (private roads, islands).
