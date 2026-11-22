#!/usr/bin/env python3
"""Compile local ledger and street-index catalogs into compact JSON bundles for the web viewer."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INDEX_DIR = BASE_DIR / "index"
SI_INDEX_DIR = BASE_DIR / "street_index" / "index"
WEB_DATA_DIR = BASE_DIR / "web" / "data"


def normalize_street(name: str) -> str:
    s = (name or "").upper().strip()
    s = re.sub(r'[.,"\']', "", s)
    # Preserve Chicago's "AVENUE A..Z" (e.g. Avenue L, Avenue O)
    if re.match(r"^AVENUE\s+[A-Z]$", s):
        return s
    # Strip trailing street type suffix only (keeps "NORTH AVE" -> "NORTH", "WEST END AVE" -> "WEST END")
    s = re.sub(
        r"\s+(AVENUE|AVE|STREET|ST|BOULEVARD|BLVD|DRIVE|DR|PLACE|PL|ROAD|RD|COURT|CT|TERRACE|TER|PARKWAY|PKWY|LANE|LN)\b.*$",
        "",
        s,
    )
    # Strip leading single-letter directional (N/S/E/W) only if followed by a street name
    s = re.sub(r"^(N|S|E|W)\s+(?=\S)", "", s)
    return re.sub(r"\s+", " ", s).strip()


def parse_endpoint(raw: str) -> tuple[str, int | None, str]:
    raw = (raw or "").strip()
    m = re.match(r"^(.+?)\s+(\d+)(?:-[\d-]+)?(?:\s+(North|South|East|West))?$", raw, re.I)
    if m:
        return normalize_street(m.group(1)), int(m.group(2)), (m.group(3) or "").upper()[:1]
    return normalize_street(raw), None, ""


def main() -> int:
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Ledger bundle (rolls + segments + annotated anchor slides)
    rolls = json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8"))
    segments = json.loads((INDEX_DIR / "ledger_segments.json").read_text(encoding="utf-8"))
    anchors_by_mid: dict[str, list[dict]] = {}

    for roll in rolls:
        mid = str(roll["manifest_id"])
        csv_path = INDEX_DIR / f"slides_{mid}.csv"
        if not csv_path.exists():
            continue
        rows = []
        with csv_path.open("r", encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(
                    {
                        "s": int(r["slide_index"]),
                        "ci": int(r["canvas_index"]),
                        "id": r["iiif_image_id"],
                        "t": (r.get("slide_type") or "").strip(),
                        "pl": r.get("ledger_page_left", ""),
                        "pr": r.get("ledger_page_right", ""),
                        "d0": r.get("observed_date_start", ""),
                        "d1": r.get("observed_date_end", ""),
                        "p0": r.get("permit_no_first", ""),
                        "rn": r.get("region_note", ""),
                        "st": r.get("order_status", ""),
                    }
                )
        rows.sort(key=lambda x: x["s"])
        anchors_by_mid[mid] = rows

    ledger_bundle = {
        "rolls": rolls,
        "segments": segments,
        "anchors": anchors_by_mid,
    }
    ledger_out = WEB_DATA_DIR / "ledgers_bundle.json"
    ledger_out.write_text(json.dumps(ledger_bundle, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    # 2. Street index bundle (138 reels + any annotated anchor cards)
    reels_path = SI_INDEX_DIR / "reels.json"
    reels_raw = json.loads(reels_path.read_text(encoding="utf-8")) if reels_path.exists() else []
    reels_out = []
    si_anchors_count = 0

    for r in reels_raw:
        mid = str(r["manifest_id"])
        s_st, s_num, s_dir = parse_endpoint(r.get("start_address_raw", ""))
        e_st, e_num, e_dir = parse_endpoint(r.get("end_address_raw", ""))
        anchors = []
        scsv = SI_INDEX_DIR / f"slides_{mid}.csv"
        if scsv.exists():
            with scsv.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if (row.get("street_name") or "").strip():
                        anchors.append(
                            {
                                "s": int(row["slide_index"]),
                                "id": row["iiif_image_id"],
                                "st": normalize_street(row["street_name"]),
                                "raw_st": row["street_name"],
                                "dir": row.get("direction", ""),
                                "num": row.get("house_number", ""),
                                "dt": row.get("permit_date", ""),
                            }
                        )
                        si_anchors_count += 1
        reels_out.append(
            {
                "n": r.get("reel_number"),
                "mid": mid,
                "ark": r.get("ark_id", ""),
                "url": r.get("uic_viewer_url", ""),
                "tot": int(r.get("total_canvases", 0)),
                "s_raw": r.get("start_address_raw", ""),
                "e_raw": r.get("end_address_raw", ""),
                "s_st": s_st,
                "s_num": s_num,
                "s_dir": s_dir,
                "e_st": e_st,
                "e_num": e_num,
                "e_dir": e_dir,
                "anchors": anchors,
            }
        )

    si_out = WEB_DATA_DIR / "street_reels_bundle.json"
    si_out.write_text(json.dumps({"reels": reels_out}, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    print(
        f"Built web bundles: {ledger_out.name} ({ledger_out.stat().st_size/1024:.0f} KB, "
        f"{len(rolls)} rolls / {len(segments)} segments) | "
        f"{si_out.name} ({si_out.stat().st_size/1024:.0f} KB, {len(reels_out)} reels / {si_anchors_count} annotated cards)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
