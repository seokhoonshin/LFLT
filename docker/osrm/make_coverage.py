#!/usr/bin/env python3
"""Write ``coverage.geojson``: the circle of N statute miles around the hub.

``docker/osrm/setup.sh`` / ``setup.ps1`` hand this polygon to ``osmium extract`` so the
OSRM server only carries the road network inside it.  Re-run this script when the hub
(``HUB_LAT`` / ``HUB_LON`` in ``routeopt/domain.py``) or the radius changes, then re-run the
setup script (and adjust the Geofabrik region list there if the circle touches different
states).

Usage (from the repo root, venv active):
    python docker/osrm/make_coverage.py                    # hub from routeopt.domain, 250 mi
    python docker/osrm/make_coverage.py --miles 300
    python docker/osrm/make_coverage.py --lat 34.1511 --lon -117.4319 --miles 250
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EARTH_RADIUS_MILES = 3958.7613
DEFAULT_MILES = 250.0
DEFAULT_VERTICES = 180  # one vertex every 2 degrees of bearing


def circle(lat: float, lon: float, miles: float, n: int = DEFAULT_VERTICES) -> list[list[float]]:
    """Closed, counter-clockwise ring of ``n`` [lon, lat] points ``miles`` from (lat, lon)."""
    phi1, lam1 = math.radians(lat), math.radians(lon)
    delta = miles / EARTH_RADIUS_MILES  # angular distance
    ring: list[list[float]] = []
    for i in range(n):
        theta = math.radians(360.0 - i * 360.0 / n)  # N -> W -> S -> E = counter-clockwise
        phi2 = math.asin(
            math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta)
        )
        lam2 = lam1 + math.atan2(
            math.sin(theta) * math.sin(delta) * math.cos(phi1),
            math.cos(delta) - math.sin(phi1) * math.sin(phi2),
        )
        ring.append([round(math.degrees(lam2), 5), round(math.degrees(phi2), 5)])
    ring.append(list(ring[0]))
    return ring


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--lat", type=float, default=None, help="hub latitude (default: routeopt.domain.HUB_LAT)"
    )
    ap.add_argument(
        "--lon", type=float, default=None, help="hub longitude (default: routeopt.domain.HUB_LON)"
    )
    ap.add_argument(
        "--miles",
        type=float,
        default=DEFAULT_MILES,
        help=f"radius in statute miles (default {DEFAULT_MILES:g})",
    )
    ap.add_argument(
        "--vertices", type=int, default=DEFAULT_VERTICES, help="ring vertices (default %(default)s)"
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=HERE / "coverage.geojson",
        help="output file (default: coverage.geojson next to this script)",
    )
    args = ap.parse_args(argv)

    lat, lon, label = args.lat, args.lon, "command line"
    if lat is None or lon is None:
        try:
            from routeopt.domain import HUB_ADDRESS, HUB_LAT, HUB_LON
        except ImportError:
            ap.error("routeopt is not importable here; pass --lat and --lon explicitly")
        lat = HUB_LAT if lat is None else lat
        lon = HUB_LON if lon is None else lon
        label = f"routeopt.domain ({HUB_ADDRESS})"
    if args.miles <= 0 or args.miles > 6000:
        ap.error("--miles must be between 0 and 6000")

    ring = circle(lat, lon, args.miles, args.vertices)
    lons = [p[0] for p in ring]
    lats = [p[1] for p in ring]
    feature = {
        "type": "Feature",
        "properties": {
            "name": f"RouteOpt OSRM coverage: {args.miles:g} miles around the hub",
            "hub": [lon, lat],
            "hub_source": label,
            "radius_miles": args.miles,
            "bbox": [min(lons), min(lats), max(lons), max(lats)],
            "generated": dt.date.today().isoformat(),
            "generator": "docker/osrm/make_coverage.py",
        },
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }
    args.out.write_text(json.dumps(feature, separators=(",", ":")) + "\n", encoding="utf-8")

    print(f"Hub:     {lat:.4f}, {lon:.4f}  (from {label})")
    print(f"Radius:  {args.miles:g} miles, {args.vertices} vertices")
    print(
        f"Bbox:    lon {min(lons):.3f} .. {max(lons):.3f}, lat {min(lats):.3f} .. {max(lats):.3f}"
    )
    print(f"Wrote:   {args.out}")
    print(
        "Next:    check the Geofabrik region list in setup.sh / setup.ps1 still matches, then re-run setup."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
