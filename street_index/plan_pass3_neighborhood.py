#!/usr/bin/env python3
"""Pass 3 planner: match target neighborhood streets (e.g. Beverly/Morgan Park/Wash Hgts/Mt Greenwood) to reel windows."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

SI_DIR = Path(__file__).resolve().parent
INDEX_DIR = SI_DIR / "index"


def normalize_street(name: str) -> str:
    s = (name or "").upper().strip()
    s = re.sub(r"[.,]", "", s)
    # Strip trailing street type suffixes for alphabetical key matching
    s = re.sub(
        r"\b(AVENUE|AVE|STREET|ST|BOULEVARD|BLVD|DRIVE|DR|PLACE|PL|ROAD|RD|COURT|CT|TERRACE|TER|PARKWAY|PKWY|LANE|LN)\b",
        "",
        s,
    )
    # Strip leading direction if supplied in street name
    s = re.sub(r"^(NORTH|SOUTH|EAST|WEST|N|S|E|W)\s+", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def parse_reel_endpoint(raw: str) -> tuple[str, int | None]:
    """Parse e.g. 'Hermosa Avenue 11337 South' -> ('HERMOSA', 11337)."""
    m = re.match(r"^(.+?)\s+(\d+)(?:-\d+)?(?:\s+(?:North|South|East|West))?$", (raw or "").strip(), re.I)
    if m:
        return normalize_street(m.group(1)), int(m.group(2))
    return normalize_street(raw), None


def street_sort_key(norm_street: str) -> tuple[int, int, str]:
    """UIC street index files named streets A-Z first, then numbered streets (1st..138th) at the end.
    Named streets file letter by letter (spaces ignored), with "St." as "Saint": Saginaw, St. Lawrence, St. Louis,
    Sangamon ... Seeley; Southport, So. Shore. Martin L. King Drive files as KING (Kimbark ... King ... Kingston) and
    5th Avenue as FIFTH among the named streets. (Same rule as streetSortKey in web/app.js.)"""
    if re.match(r"^MARTIN (L|LUTHER) KING( JR)?$", norm_street):
        norm_street = "KING"
    elif norm_street == "5TH":
        norm_street = "FIFTH"
    m = re.match(r"^(\d+)(?:ST|ND|RD|TH)?$", norm_street)
    if m:
        return (1, int(m.group(1)), norm_street)
    return (0, 0, re.sub(r"\s+", "", re.sub(r"^SO\s+", "SOUTH ", re.sub(r"^ST\s+", "SAINT ", norm_street))))  # So. Shore = South Shore


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Match a neighborhood street list CSV (street_name[,num_min,num_max]) to street-index reel windows."
    )
    parser.add_argument("streets_csv", help="CSV with column 'street_name' (and optional 'num_min', 'num_max').")
    parser.add_argument("--step", type=int, default=100, help="High-resolution slide step inside matched windows (default: 100).")
    parser.add_argument("--out", default=str(INDEX_DIR / "pass3_neighborhood_queue.json"), help="Output queue JSON path.")
    parser.add_argument("--pass-name", default="pass3_neighborhood", help="Pass label (default: pass3_neighborhood).")
    args = parser.parse_args()

    reels_path = INDEX_DIR / "reels.json"
    if not reels_path.exists():
        reels_path = INDEX_DIR / "collection_reels.json"
    reels = json.loads(reels_path.read_text(encoding="utf-8"))

    target_streets = list(csv.DictReader(Path(args.streets_csv).open("r", encoding="utf-8", newline="")))
    print(f"Loaded {len(target_streets)} target neighborhood streets from {args.streets_csv}")

    matched_reels: dict[str, list[str]] = {}
    queue: list[dict] = []

    for r in reels:
        s_key = street_sort_key(parse_reel_endpoint(r.get("start_address_raw", ""))[0])
        e_key = street_sort_key(parse_reel_endpoint(r.get("end_address_raw", ""))[0])
        hits = []
        for ts in target_streets:
            st_norm = normalize_street(ts.get("street_name", ""))
            if not st_norm:
                continue
            t_key = street_sort_key(st_norm)
            if s_key <= t_key <= e_key:
                hits.append(st_norm)
        if not hits:
            continue

        mid = str(r.get("manifest_id") or "")
        matched_reels[f"Reel {r.get('reel_number')} ({mid or r.get('ark_id')})"] = sorted(set(hits))
        if not mid or not r.get("total_canvases"):
            continue

        # Narrow to Pass 1/2 observed anchor intervals if slides_<mid>.csv is annotated
        slides_csv = INDEX_DIR / f"slides_{mid}.csv"
        max_slide = int(r["total_canvases"])
        windows: list[tuple[int, int]] = []
        if slides_csv.exists():
            anchors = [
                row
                for row in csv.DictReader(slides_csv.open("r", encoding="utf-8", newline=""))
                if (row.get("street_name") or "").strip()
            ]
            anchors.sort(key=lambda x: int(x["slide_index"]))
            if anchors:
                for st_norm in sorted(set(hits)):
                    t_key = street_sort_key(st_norm)
                    lo_s, hi_s = 1, max_slide
                    for a in anchors:
                        a_key = street_sort_key(normalize_street(a["street_name"]))
                        a_idx = int(a["slide_index"])
                        if a_key < t_key:
                            lo_s = max(lo_s, a_idx)
                        elif a_key > t_key:
                            hi_s = min(hi_s, a_idx)
                            break
                    windows.append((lo_s, hi_s))
        if not windows:
            windows = [(1, max_slide)]

        # Merge overlapping windows and emit high-res slide samples
        windows.sort()
        merged: list[list[int]] = []
        for lo, hi in windows:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])

        for lo, hi in merged:
            s = lo
            while s <= hi:
                queue.append(
                    {
                        "reel_number": r.get("reel_number"),
                        "manifest_id": mid,
                        "slide_index": s,
                        "anchor_slide": s,
                        "pass_name": args.pass_name,
                    }
                )
                s += args.step

    print(f"Matched {len(matched_reels)} reels covering target streets:")
    for label, sts in matched_reels.items():
        print(f"  {label}: {', '.join(sts[:8])}{' ...' if len(sts) > 8 else ''}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    print(f"\nPass-3 neighborhood queue written: {out_path} ({len(queue)} slides @ step={args.step})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
