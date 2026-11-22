#!/usr/bin/env python3
"""Offline date-order checker and binary-search bisection planner for sampled microfilm slides."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INDEX_DIR = BASE_DIR / "index"


def parse_iso(val: str) -> date | None:
    val = (val or "").strip()
    if not val:
        return None
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze offline-annotated dates in slides_<id>.csv and flag out-of-order / multi-region intervals."
    )
    parser.add_argument("manifest_id", help="Manifest ID (e.g. 806145)")
    parser.add_argument(
        "--backtrack-tolerance-days",
        type=int,
        default=7,
        help="Ignore minor backwards date jitter up to this many days (default: 7).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Write computed order_status flags back to slides_<id>.csv and rolls.json.",
    )
    args = parser.parse_args()

    mid = str(args.manifest_id).strip()
    csv_path = INDEX_DIR / f"slides_{mid}.csv"
    if not csv_path.exists():
        print(f"Not found: {csv_path}", file=sys.stderr)
        return 1

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
        fieldnames = f.readline()  # unused
    rows.sort(key=lambda r: int(r["slide_index"]))

    permit_rows = []
    bisect_slides: list[int] = []
    out_of_order_intervals: list[tuple[int, int, str, str]] = []

    for r in rows:
        stype = (r.get("slide_type") or "").strip().lower()
        d_start = parse_iso(r.get("observed_date_start", ""))
        if stype in ("cover", "blank", "index", "target", "non_permit"):
            r["order_status"] = "non_permit"
        elif stype == "illegible":
            r["order_status"] = "illegible"
        elif d_start is None:
            r["order_status"] = "pending"
        else:
            r["order_status"] = "ok"
            permit_rows.append((r, d_start))

    for i in range(1, len(permit_rows)):
        prev_r, prev_d = permit_rows[i - 1]
        cur_r, cur_d = permit_rows[i]
        delta_days = (cur_d - prev_d).days
        if delta_days < -args.backtrack_tolerance_days:
            cur_r["order_status"] = "out_of_order"
            s_a = int(prev_r["slide_index"])
            s_b = int(cur_r["slide_index"])
            out_of_order_intervals.append((s_a, s_b, prev_d.isoformat(), cur_d.isoformat()))
            if s_b - s_a > 1:
                bisect_slides.append((s_a + s_b) // 2)

    print(f"=== Offline Order Check: Manifest {mid} ===")
    print(f"Total sampled slides: {len(rows)} | Annotated permit slides: {len(permit_rows)}")
    for r in rows:
        print(
            f"  p{int(r['slide_index']):04d} | type={r.get('slide_type') or '-':10s} | "
            f"pages={r.get('ledger_page_left') or '?'}/{r.get('ledger_page_right') or '?':4s} | "
            f"start={r.get('observed_date_start') or '-':10s} | "
            f"status={r.get('order_status')}"
        )

    if out_of_order_intervals:
        print("\n[!] OUT-OF-ORDER / MULTI-REGION BREAKS DETECTED:")
        for s_a, s_b, d_a, d_b in out_of_order_intervals:
            print(f"  Interval p{s_a:04d} ({d_a}) -> p{s_b:04d} ({d_b})")
        if bisect_slides:
            slide_arg = ",".join(str(x) for x in sorted(set(bisect_slides)))
            print(f"\nSuggested Pass-2 bisection command:")
            print(f"  python3 sample_download.py {mid} --slides {slide_arg}")
    elif permit_rows:
        print("\n[OK] All annotated permit dates are monotonically non-decreasing.")

    if args.write and rows:
        cols = list(rows[0].keys())
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)
        json_path = INDEX_DIR / f"slides_{mid}.json"
        json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

        rolls_path = INDEX_DIR / "rolls.json"
        if rolls_path.exists():
            rolls = json.loads(rolls_path.read_text(encoding="utf-8"))
            for roll in rolls:
                if str(roll["manifest_id"]) == mid:
                    roll["multi_region_flagged"] = bool(out_of_order_intervals)
            rolls_path.write_text(json.dumps(rolls, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Updated {csv_path} and {rolls_path}.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
