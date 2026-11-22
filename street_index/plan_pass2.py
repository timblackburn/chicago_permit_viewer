#!/usr/bin/env python3
"""Pass 2 planner for Street Index: report legibility stats and queue nearby recovery cards for illegible anchors."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

SI_DIR = Path(__file__).resolve().parent
INDEX_DIR = SI_DIR / "index"

DEFAULT_OFFSETS = [5, -5, 15, -15, 30, -30, 60, -60]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Analyze street-index legibility across sampled slides and generate Pass-2 recovery queue."
    )
    parser.add_argument(
        "--offsets",
        default=",".join(str(x) for x in DEFAULT_OFFSETS),
        help="Comma-separated slide offsets to try around illegible anchors (default: 5,-5,15,-15,30,-30,60,-60).",
    )
    parser.add_argument(
        "--out",
        default=str(INDEX_DIR / "pass2_queue.json"),
        help="Output JSON queue path for sample_reels.py --queue.",
    )
    parser.add_argument("--pass-name", default="pass2", help="Pass name label for queued items (default: pass2).")
    args = parser.parse_args()

    offsets = [int(x.strip()) for x in args.offsets.split(",") if x.strip()]
    reels_path = INDEX_DIR / "reels.json"
    if not reels_path.exists():
        print(f"Not found: {reels_path}", file=sys.stderr)
        return 1
    reels_by_mid = {str(r["manifest_id"]): r for r in json.loads(reels_path.read_text(encoding="utf-8"))}

    leg_counts: Counter[str] = Counter()
    street_leg_counts: Counter[str] = Counter()
    num_leg_counts: Counter[str] = Counter()
    queue: list[dict] = []
    reels_scanned = 0
    total_rows = 0

    for csv_path in sorted(INDEX_DIR.glob("slides_*.csv")):
        mid = csv_path.stem.replace("slides_", "")
        reel = reels_by_mid.get(mid)
        if not reel:
            continue
        reels_scanned += 1
        max_slide = int(reel["total_canvases"])
        rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")))
        total_rows += len(rows)

        tried_slides = {int(r["slide_index"]) for r in rows}
        by_anchor: dict[int, list[dict]] = {}
        for r in rows:
            leg = (r.get("legibility") or "pending").strip().lower()
            leg_counts[leg] += 1
            s_ok = (r.get("street_name_legible") or "").strip().lower()
            n_ok = (r.get("house_num_legible") or "").strip().lower()
            if s_ok:
                street_leg_counts[s_ok] += 1
            if n_ok:
                num_leg_counts[n_ok] += 1
            anchor = int(r.get("anchor_slide") or r["slide_index"])
            by_anchor.setdefault(anchor, []).append(r)

        for anchor, group in sorted(by_anchor.items()):
            # Anchor is satisfied if any slide in this anchor group has BOTH street name & house number legible (legibility == ok)
            if any((g.get("legibility") or "").strip().lower() == "ok" for g in group):
                continue
            # Skip anchors that are still pending inspection
            if any((g.get("legibility") or "pending").strip().lower() == "pending" for g in group):
                continue
            # Pick the first untried nearby offset within [1, max_slide]
            for off in offsets:
                cand = anchor + off
                if 1 <= cand <= max_slide and cand not in tried_slides:
                    queue.append(
                        {
                            "reel_number": reel.get("reel_number"),
                            "manifest_id": mid,
                            "slide_index": cand,
                            "anchor_slide": anchor,
                            "offset": off,
                            "pass_name": args.pass_name,
                        }
                    )
                    tried_slides.add(cand)
                    break

    annotated = total_rows - leg_counts.get("pending", 0)
    print(f"=== Street Index Legibility Audit ===")
    print(f"Reels scanned: {reels_scanned} | Total sampled slides: {total_rows} | Annotated: {annotated}")
    for k, v in sorted(leg_counts.items()):
        pct = (100.0 * v / total_rows) if total_rows else 0.0
        print(f"  legibility={k:10s}: {v:5d} ({pct:5.1f}%)")
    if street_leg_counts:
        s_tot = sum(street_leg_counts.values())
        print(f"  street_name_legible=yes : {street_leg_counts['yes']}/{s_tot} ({100.0 * street_leg_counts['yes'] / s_tot:.1f}%)")
    if num_leg_counts:
        n_tot = sum(num_leg_counts.values())
        print(f"  house_num_legible=yes   : {num_leg_counts['yes']}/{n_tot} ({100.0 * num_leg_counts['yes'] / n_tot:.1f}%)")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    print(f"\nPass-2 recovery queue written: {out_path} ({len(queue)} nearby slides queued)")
    if queue:
        print(f"Run: python3 street_index/sample_reels.py --queue {out_path} --pass-name {args.pass_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
