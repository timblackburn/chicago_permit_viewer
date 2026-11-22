#!/usr/bin/env python3
"""Print pending street-index card images for a range of reel numbers."""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

BASE_DIR = Path("/home/timblackburn/projects/uic_permit_index")
SI_INDEX_DIR = BASE_DIR / "street_index" / "index"


def main() -> int:
    if len(sys.argv) < 3:
        print("Usage: list_pending_reels.py <start_reel> <end_reel>", file=sys.stderr)
        return 1
    r_lo = int(sys.argv[1])
    r_hi = int(sys.argv[2])

    reels = sorted(
        json.loads((SI_INDEX_DIR / "reels.json").read_text(encoding="utf-8")),
        key=lambda r: int(r["reel_number"]),
    )
    selected = [r for r in reels if r_lo <= int(r["reel_number"]) <= r_hi]
    total_pending = 0
    for r in selected:
        mid = str(r["manifest_id"])
        csv_path = SI_INDEX_DIR / f"slides_{mid}.csv"
        rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")))
        pending = [row for row in rows if (row.get("legibility") or "pending").strip().lower() == "pending"]
        total_pending += len(pending)
        print(
            f"REEL {r['reel_number']} | manifest_id={mid} | range: {r['start_address_raw']} -> {r['end_address_raw']} | pending={len(pending)}"
        )
        for row in pending:
            abs_p = BASE_DIR / row["local_path"]
            print(f"  slide_index={row['slide_index']} | path={abs_p}")
    print(f"TOTAL PENDING in Reels {r_lo}..{r_hi}: {total_pending}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
