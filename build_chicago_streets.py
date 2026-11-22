#!/usr/bin/env python3
"""Precompute compact Chicago street autocomplete and block-level cross-street lookup tables."""

from __future__ import annotations

import csv
import json
import re
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "data" / "chicago_street_centerlines.csv"
OUT_AUTO = BASE_DIR / "web" / "data" / "chicago_streets_autocomplete.json"
OUT_CROSS = BASE_DIR / "web" / "data" / "chicago_cross_streets.json"


def title_street(s: str) -> str:
    parts = []
    for tok in (s or "").strip().split():
        if re.match(r"^\d+(ST|ND|RD|TH)$", tok, re.I):
            parts.append(tok.lower())
        elif tok.upper() in ("MC", "ST"):
            parts.append(tok.capitalize())
        else:
            parts.append(tok.capitalize())
    return " ".join(parts)


def normalize_key(name: str) -> str:
    s = (name or "").upper().strip()
    s = re.sub(r'[.,"\']', "", s)
    if re.match(r"^AVENUE\s+[A-Z]$", s):
        return s
    return re.sub(r"\s+", " ", s).strip()


def format_cross(raw: str) -> str:
    # Format: "2500|W|108TH|ST|" -> "2500 W 108th St"
    if not raw:
        return ""
    parts = [p.strip() for p in raw.split("|")]
    if len(parts) < 4:
        return ""
    num, pre_dir, name, st_type = parts[0], parts[1], parts[2], parts[3]
    if not name:
        return ""
    tokens = []
    if num and num != "0":
        tokens.append(num)
    if pre_dir:
        tokens.append(pre_dir.upper())
    tokens.append(title_street(name))
    if st_type:
        tokens.append(st_type.capitalize())
    return " ".join(tokens)


def parse_int(v: str) -> int:
    try:
        return int(v)
    except Exception:
        return 0


def main() -> int:
    if not CSV_PATH.exists():
        raise SystemExit(f"Missing {CSV_PATH}")

    autocomplete_set: set[str] = set()
    # key -> list of [dir, lo, hi, f_cross, t_cross]
    blocks_by_key: dict[str, list[list]] = defaultdict(list)
    seen_block: set[tuple] = set()

    with CSV_PATH.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            name = (row.get("STREET_NAM") or "").strip()
            if not name:
                continue
            pre_dir = (row.get("PRE_DIR") or "").strip().upper()
            st_type = (row.get("STREET_TYP") or "").strip().upper()

            # Build readable autocomplete label
            label_parts = []
            if pre_dir:
                label_parts.append(pre_dir)
            label_parts.append(title_street(name))
            if st_type:
                label_parts.append(st_type.capitalize())
            label = " ".join(label_parts)
            autocomplete_set.add(label)

            # Determine valid address range for this block segment
            nums = [
                n
                for n in (
                    parse_int(row.get("L_F_ADD", "")),
                    parse_int(row.get("L_T_ADD", "")),
                    parse_int(row.get("R_F_ADD", "")),
                    parse_int(row.get("R_T_ADD", "")),
                )
                if n > 0
            ]
            if not nums:
                continue
            lo, hi = min(nums), max(nums)

            # Determine which cross street belongs to lo vs hi
            f_add = parse_int(row.get("L_F_ADD", "")) or parse_int(row.get("R_F_ADD", ""))
            t_add = parse_int(row.get("L_T_ADD", "")) or parse_int(row.get("R_T_ADD", ""))
            f_cross = format_cross(row.get("F_CROSS", ""))
            t_cross = format_cross(row.get("T_CROSS", ""))
            if f_add > t_add and t_add > 0:
                f_cross, t_cross = t_cross, f_cross

            if not f_cross and not t_cross:
                continue

            key = normalize_key(name)
            dedup = (key, pre_dir, lo, hi, f_cross, t_cross)
            if dedup in seen_block:
                continue
            seen_block.add(dedup)
            blocks_by_key[key].append([pre_dir, lo, hi, f_cross, t_cross])

    for key in blocks_by_key:
        blocks_by_key[key].sort(key=lambda b: (b[0], b[1], b[2]))

    auto_sorted = sorted(autocomplete_set)
    OUT_AUTO.parent.mkdir(parents=True, exist_ok=True)
    OUT_AUTO.write_text(json.dumps(auto_sorted, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    OUT_CROSS.write_text(json.dumps(blocks_by_key, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    print(
        f"Built Chicago street data: {len(auto_sorted)} autocomplete streets ({OUT_AUTO.stat().st_size // 1024} KB) | "
        f"{len(blocks_by_key)} street keys / {sum(len(v) for v in blocks_by_key.values())} blocks ({OUT_CROSS.stat().st_size // 1024} KB)"
    )
    # Sanity check 6500 S Kilpatrick & 10858 S Campbell
    for test_k, test_n in [("KILPATRICK", 6500), ("CAMPBELL", 10858), ("WESTERN", 10400)]:
        hits = [b for b in blocks_by_key.get(test_k, []) if b[1] <= test_n <= b[2]]
        print(f"  Test {test_n} {test_k}: {hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
