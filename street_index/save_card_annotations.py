#!/usr/bin/env python3
"""Save extracted street-index card annotations into street_index/index/slides_<mid>.csv and .json."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

BASE_DIR = Path("/home/timblackburn/projects/uic_permit_index")
SI_INDEX_DIR = BASE_DIR / "street_index" / "index"

SLIDE_CSV_FIELDS = [
    "reel_number",
    "manifest_id",
    "slide_index",
    "canvas_index",
    "iiif_image_id",
    "iiif_crop_url",
    "iiif_viewer_link",
    "uic_viewer_url",
    "width",
    "height",
    "region",
    "requested_width",
    "local_path",
    "bytes",
    "pass_name",
    "anchor_slide",
    "legibility",          # ok | partial | illegible | non_card | pending
    "street_name_legible", # yes | no | ""
    "house_num_legible",   # yes | no | ""
    "street_name",
    "direction",           # N | S | E | W | ""
    "house_number",
    "permit_date",
    "permit_no",
    "notes",
]


def save_reel(mid: str, items: list[dict]) -> int:
    csv_path = SI_INDEX_DIR / f"slides_{mid}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing {csv_path}")

    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    by_slide = {int(r["slide_index"]): r for r in rows}

    updated = 0
    for item in items:
        s = int(item["slide_index"])
        if s not in by_slide:
            continue
        r = by_slide[s]
        r["legibility"] = str(item.get("legibility") or "ok").strip().lower()
        r["street_name_legible"] = str(item.get("street_name_legible") or "").strip().lower()
        r["house_num_legible"] = str(item.get("house_num_legible") or "").strip().lower()
        r["street_name"] = str(item.get("street_name") or "").strip()
        r["direction"] = str(item.get("direction") or "").strip().upper()
        r["house_number"] = str(item.get("house_number") or "").strip()
        r["permit_date"] = str(item.get("permit_date") or "").strip()
        r["permit_no"] = str(item.get("permit_no") or "").strip()
        r["notes"] = str(item.get("notes") or "").strip()
        updated += 1

    ordered = [by_slide[k] for k in sorted(by_slide.keys())]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=SLIDE_CSV_FIELDS)
        w.writeheader()
        for row in ordered:
            w.writerow({k: row.get(k, "") for k in SLIDE_CSV_FIELDS})

    json_path = SI_INDEX_DIR / f"slides_{mid}.json"
    json_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(description="Save street-index card annotations into slides_<mid>.csv/.json.")
    parser.add_argument("manifest_id", help="Reel manifest_id (or 'batch' if passing a dict of {mid: [items]}).")
    parser.add_argument("json_input", help="Path to JSON file or inline JSON string of extracted card records.")
    args = parser.parse_args()

    p = Path(args.json_input)
    raw = p.read_text(encoding="utf-8") if p.exists() else args.json_input
    data = json.loads(raw)

    if args.manifest_id == "batch" and isinstance(data, dict):
        tot = 0
        for mid, items in data.items():
            cnt = save_reel(str(mid), items)
            tot += cnt
            print(f"Saved {cnt} slides for manifest {mid}")
        print(f"Total updated: {tot}")
        return 0

    cnt = save_reel(str(args.manifest_id), data)
    print(f"Saved {cnt} slides for manifest {args.manifest_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
