#!/usr/bin/env bash
# Runs INSIDE the routeopt-osmium container (see Dockerfile); /data is docker/osrm/data.
#
#   routeopt-prepare <name> <coverage.geojson> <extract.osm.pbf>...
#
# 1. osmium tags-filter: keep only what OSRM's car profile reads - highway ways, ferry
#    routes, turn-restriction relations and road-route relations (plus the nodes they
#    reference).  Buildings, land use, POIs etc. are dropped, which makes the next steps
#    and OSRM's own processing much smaller.
# 2. osmium merge: combine the per-region files (shared border objects are written once).
# 3. osmium extract: clip to the coverage polygon, keeping ways that cross its edge whole.
#
# Output: /data/<name>.osm.pbf, ready for osrm-extract.
set -euo pipefail

NAME="${1:?usage: routeopt-prepare <name> <coverage.geojson> <extract.osm.pbf>...}"
POLY="${2:?missing coverage polygon}"
shift 2
if [[ $# -lt 1 ]]; then
  echo "routeopt-prepare: no input extracts given" >&2
  exit 2
fi
for f in "$POLY" "$@"; do
  [[ -f "$f" ]] || { echo "routeopt-prepare: missing $f" >&2; exit 2; }
done

WORK="/data/work"
OUT="/data/$NAME.osm.pbf"
mkdir -p "$WORK"

filtered=()
for src in "$@"; do
  base="$(basename "$src" .osm.pbf)"
  dst="$WORK/$base.roads.osm.pbf"
  echo "== osmium tags-filter $base"
  osmium tags-filter --overwrite -o "$dst" "$src" \
    w/highway w/route=ferry r/type=restriction r/route=road
  filtered+=("$dst")
done

if [[ ${#filtered[@]} -eq 1 ]]; then
  merged="${filtered[0]}"
else
  merged="$WORK/merged.osm.pbf"
  echo "== osmium merge (${#filtered[@]} files)"
  osmium merge --overwrite -o "$merged" "${filtered[@]}"
fi

echo "== osmium extract (clip to $(basename "$POLY"), complete ways)"
osmium extract --overwrite --strategy complete_ways --polygon "$POLY" -o "$OUT" "$merged"

rm -rf "$WORK"
echo "== result: $OUT"
osmium fileinfo --extended "$OUT" | grep -E "Bounding box|Number of (nodes|ways|relations)|Size" || true
