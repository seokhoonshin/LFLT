#!/usr/bin/env bash
# Prepare the OSRM data for RouteOpt: the road network within 250 miles of the hub.
#
# Downloads the Geofabrik extracts that the coverage circle (coverage.geojson) touches,
# builds the small routeopt-osmium helper image, filters/merges/clips the extracts to the
# circle (osmium/prepare.sh) and runs the OSRM MLD pipeline
# (osrm-extract -> osrm-partition -> osrm-customize).  Re-run to refresh the map.
#
#   ./setup.sh                              # uses ./data next to this script
#   FORCE_DOWNLOAD=1 ./setup.sh             # re-download the extracts
#   INCLUDE_MEXICO=1 ./setup.sh             # also northern Baja California / Sonora
#   REGIONS="us/california us/nevada" ./setup.sh   # override the region list
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${DATA_DIR:-$HERE/data}"
GEOFABRIK="${GEOFABRIK:-https://download.geofabrik.de/north-america}"
OSRM_IMAGE="${OSRM_IMAGE:-ghcr.io/project-osrm/osrm-backend:v5.27.1}"
OSMIUM_IMAGE="${OSMIUM_IMAGE:-routeopt-osmium}"   # built locally from ./osmium
NAME="${NAME:-routeopt-coverage}"                   # must match docker-compose.yml
POLY="$HERE/coverage.geojson"

# Geofabrik regions the 250-mile circle around the hub (Rialto, CA) touches (Utah's nearest
# corner is 273 miles away).  If the hub or the radius changes (make_coverage.py), check this
# list against the new circle: 500 miles would add us/utah and us/new-mexico.
REGIONS="${REGIONS:-us/california us/nevada us/arizona}"
if [[ "${INCLUDE_MEXICO:-0}" == "1" ]]; then
  REGIONS="$REGIONS mexico"
fi

command -v docker >/dev/null || { echo "docker is not installed or not on PATH" >&2; exit 1; }
[[ -f "$POLY" ]] || { echo "Missing $POLY - run: python docker/osrm/make_coverage.py" >&2; exit 1; }

mkdir -p "$DATA_DIR"
cp "$POLY" "$DATA_DIR/coverage.geojson"

inputs=()
for region in $REGIONS; do
  base="$(basename "$region")-latest.osm.pbf"
  file="$DATA_DIR/$base"
  url="$GEOFABRIK/$region-latest.osm.pbf"
  if [[ -f "$file" && "${FORCE_DOWNLOAD:-0}" != "1" ]]; then
    echo "Using existing $file (set FORCE_DOWNLOAD=1 to re-download)"
  else
    echo "Downloading $url ..."
    curl -L --fail --progress-bar -o "$file" "$url"
  fi
  inputs+=("/data/$base")
done

echo "Building the osmium helper image ($OSMIUM_IMAGE) ..."
docker build -q -t "$OSMIUM_IMAGE" "$HERE/osmium" >/dev/null

run() { docker run --rm -t -v "$DATA_DIR:/data" "$@"; }

echo "Filtering, merging and clipping the extracts to coverage.geojson ..."
run "$OSMIUM_IMAGE" routeopt-prepare "$NAME" /data/coverage.geojson "${inputs[@]}"

echo "osrm-extract (car profile) ..."
run "$OSRM_IMAGE" osrm-extract -p /opt/car.lua "/data/$NAME.osm.pbf"
echo "osrm-partition ..."
run "$OSRM_IMAGE" osrm-partition "/data/$NAME.osrm"
echo "osrm-customize ..."
run "$OSRM_IMAGE" osrm-customize "/data/$NAME.osrm"

echo
echo "Done. Start the server with:  docker compose -f \"$HERE/docker-compose.yml\" up -d"
echo "Health check (hub):           curl 'http://localhost:5000/nearest/v1/driving/-117.4319,34.1511'"
echo "Coverage check (hub -> LV):   curl 'http://localhost:5000/route/v1/driving/-117.4319,34.1511;-115.1398,36.1699?overview=false'"
