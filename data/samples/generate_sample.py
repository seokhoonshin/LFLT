"""Generate a synthetic shipment workbook in the RouteOpt input schema.

Usage:
    python data/samples/generate_sample.py --shipments 120 --plan-date 2026-09-11 --seed 42
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from routeopt.domain import hub_today  # noqa: E402
from routeopt.sample_data import generate_sample  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shipments", type=int, default=120)
    ap.add_argument(
        "--plan-date",
        type=lambda s: dt.date.fromisoformat(s),
        default=hub_today() + dt.timedelta(days=2),
    )
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output .xlsx (default: data/samples/sample_shipments_<D>.xlsx)",
    )
    ap.add_argument(
        "--all-coords",
        action="store_true",
        help="fill every row's lat/lon (no Census geocoding needed to run it)",
    )
    args = ap.parse_args(argv)
    out = (
        args.out
        or ROOT / "data" / "samples" / f"sample_shipments_{args.plan_date.isoformat()}.xlsx"
    )
    path = generate_sample(
        args.shipments, args.plan_date, args.seed, out, blank_coords=not args.all_coords
    )
    print(f"Wrote {args.shipments} shipments for D={args.plan_date.isoformat()} to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
