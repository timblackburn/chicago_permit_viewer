#!/usr/bin/env python3
"""Compile all 90 annotated slides_<id>.csv files into a unified contiguous segment index (ledger_segments.json/.csv)."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
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


def extract_region_tag(region_note: str) -> str:
    s = (region_note or "").strip()
    if not s:
        return ""
    head = s.split("[")[0].strip().rstrip("-:, ")
    head = re.sub(r"\s*\(.*?\)", "", head).strip()
    return head.split()[0] if head in ("South", "North", "West", "Northwest", "Southwest", "Central") else head


def main() -> int:
    rolls = json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8"))
    segments: list[dict] = []
    rolls_with_multi_segments = 0

    for roll in rolls:
        mid = str(roll["manifest_id"])
        csv_path = INDEX_DIR / f"slides_{mid}.csv"
        if not csv_path.exists():
            continue
        rows = sorted(
            csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")),
            key=lambda r: int(r["slide_index"]),
        )
        permit_rows = [
            r for r in rows if (r.get("slide_type") or "").strip().lower() == "permits" and parse_iso(r.get("observed_date_start", ""))
        ]
        if not permit_rows:
            continue

        # Group into contiguous monotonic segments (splitting on > 7-day reversals, except keeping contiguous reverse runs together)
        groups: list[list[dict]] = [[permit_rows[0]]]
        for r in permit_rows[1:]:
            prev = groups[-1][-1]
            d_prev = parse_iso(prev["observed_date_start"])
            d_cur = parse_iso(r["observed_date_start"])
            assert d_prev and d_cur
            if (d_cur - d_prev).days < -7:
                # Check if current group is already descending and this continues descending cleanly
                if len(groups[-1]) >= 2:
                    d_prev2 = parse_iso(groups[-1][-2]["observed_date_start"])
                    if d_prev2 and (d_prev - d_prev2).days < -7 and (d_prev - d_cur).days <= 60:
                        groups[-1].append(r)
                        continue
                groups.append([r])
            else:
                groups[-1].append(r)

        if len(groups) > 1:
            rolls_with_multi_segments += 1

        for seg_idx, grp in enumerate(groups, start=1):
            d_first = parse_iso(grp[0]["observed_date_start"])
            d_last = parse_iso(grp[-1]["observed_date_start"])
            d_all = [d for r in grp for d in (parse_iso(r["observed_date_start"]), parse_iso(r.get("observed_date_end", ""))) if d]
            reg_counts = Counter(extract_region_tag(r.get("region_note", "")) for r in grp)
            reg_counts.pop("", None)
            obs_regions = ", ".join(k for k, _ in reg_counts.most_common(3))
            direction = "single" if len(grp) == 1 else ("desc" if (d_last and d_first and d_last < d_first) else "asc")

            segments.append(
                {
                    "manifest_id": mid,
                    "reel_uid": roll.get("reel_uid", ""),
                    "title": roll.get("title", ""),
                    "era": roll.get("era", ""),
                    "segment_index": seg_idx,
                    "segment_count_in_roll": len(groups),
                    "sampled_permit_slides": len(grp),
                    "slide_start": int(grp[0]["slide_index"]),
                    "slide_end": int(grp[-1]["slide_index"]),
                    "page_start": grp[0].get("ledger_page_left") or grp[0].get("ledger_page_right") or "",
                    "page_end": grp[-1].get("ledger_page_right") or grp[-1].get("ledger_page_left") or "",
                    "date_min": min(d_all).isoformat() if d_all else "",
                    "date_max": max(d_all).isoformat() if d_all else "",
                    "order_direction": direction,
                    "observed_regions": obs_regions,
                    "ark_permalink": roll.get("ark_permalink", ""),
                    "uic_viewer_url": roll.get("uic_viewer_url", ""),
                    "iiif_viewer_link_start": grp[0].get("iiif_viewer_link", ""),
                    "iiif_viewer_link_end": grp[-1].get("iiif_viewer_link", ""),
                }
            )

    out_json = INDEX_DIR / "ledger_segments.json"
    out_csv = INDEX_DIR / "ledger_segments.csv"
    out_json.write_text(json.dumps(segments, indent=2, ensure_ascii=False), encoding="utf-8")
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(segments[0].keys()))
        w.writeheader()
        w.writerows(segments)

    print(
        f"Built {out_csv.name}: {len(segments)} segments across {len(rolls)} rolls "
        f"({rolls_with_multi_segments} rolls with >1 segment)."
    )
    for seg in segments:
        if seg["segment_count_in_roll"] > 1:
            print(
                f"  [{seg['manifest_id']}] seg {seg['segment_index']}/{seg['segment_count_in_roll']} "
                f"slides {seg['slide_start']:3d}..{seg['slide_end']:3d} | {seg['date_min']}..{seg['date_max']} "
                f"({seg['order_direction']}) | {seg['observed_regions']} | {seg['title'][:55]}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
