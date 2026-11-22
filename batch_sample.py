#!/usr/bin/env python3
"""Pass 1 batch sampler: politely download stratified 6% top-25% crops across all indexed rolls."""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from sample_download import (
    BASE_DIR,
    DATA_DIR,
    INDEX_DIR,
    append_log,
    build_iiif_url,
    choose_stratified_slides,
    download_image,
    load_existing_slides,
    region_slug,
    save_slides_index,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Pass-1 stratified IIIF sampling across all rolls in index/rolls.json.")
    parser.add_argument("--region", default="pct:0,0,100,25", help="IIIF region parameter (default: pct:0,0,100,25)")
    parser.add_argument("--width", default="2500", help="IIIF output width in px (default: 2500)")
    parser.add_argument("--quality", default="default", choices=["default", "gray", "color"])
    parser.add_argument("--percent", type=float, default=6.0, help="Target percent of spreads per roll (default: 6.0)")
    parser.add_argument("--min-slides", type=int, default=10, help="Min slides per roll (default: 10)")
    parser.add_argument("--max-slides", type=int, default=24, help="Max slides per roll (default: 24)")
    parser.add_argument("--min-delay", type=float, default=6.0, help="Min delay between HTTP requests (default: 6.0s)")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max delay between HTTP requests (default: 8.0s)")
    parser.add_argument("--limit-rolls", type=int, default=0, help="Optional cap on number of rolls to process (0 = all)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan and totals without downloading.")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s to respect UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    rolls_path = INDEX_DIR / "rolls.json"
    if not rolls_path.exists():
        print(f"Not found: {rolls_path}. Run Pass 0 (fetch_manifest.py) first.", file=sys.stderr)
        return 1

    rolls = json.loads(rolls_path.read_text(encoding="utf-8"))
    if args.limit_rolls > 0:
        rolls = rolls[: args.limit_rolls]

    reg_tag = region_slug(args.region)
    plan: list[tuple[dict, list[tuple[int, dict, str, Path]]]] = []
    total_slides = 0
    total_cached = 0
    total_to_fetch = 0

    for roll in rolls:
        mid = str(roll["manifest_id"])
        canvases_path = INDEX_DIR / f"canvases_{mid}.json"
        if not canvases_path.exists():
            print(f"Warning: missing {canvases_path}; skipping {mid}.", file=sys.stderr)
            continue
        canvases = json.loads(canvases_path.read_text(encoding="utf-8"))
        by_slide = {int(c["slide_index"]): c for c in canvases}
        slide_nums = choose_stratified_slides(
            canvases,
            target_percent=args.percent,
            min_slides=args.min_slides,
            max_slides=args.max_slides,
        )
        slides_dir = DATA_DIR / mid / f"slides_{reg_tag}_w{args.width}"
        items: list[tuple[int, dict, str, Path]] = []
        for s in slide_nums:
            c = by_slide[s]
            url = build_iiif_url(c["iiif_service_url"], args.region, args.width, args.quality)
            dest = slides_dir / f"p{s:04d}_{c['iiif_image_id']}.jpg"
            items.append((s, c, url, dest))
            total_slides += 1
            if dest.exists():
                total_cached += 1
            else:
                total_to_fetch += 1
        plan.append((roll, items))

    avg_delay = (args.min_delay + max(args.min_delay, args.max_delay)) / 2.0
    est_sec = max(0, total_to_fetch) * (avg_delay + 1.0)
    print(
        f"Pass 1 Plan: {len(plan)} rolls | {total_slides} total sampled slides "
        f"({total_cached} cached, {total_to_fetch} to fetch) | "
        f"region={args.region} w={args.width} | est ~{est_sec / 60:.1f} min ({est_sec / 3600:.2f} hr)",
        flush=True,
    )

    if args.dry_run:
        for idx, (roll, items) in enumerate(plan, start=1):
            c_cnt = sum(1 for _, _, _, d in items if d.exists())
            f_cnt = len(items) - c_cnt
            print(
                f"  ({idx:2d}/{len(plan)}) [{roll['manifest_id']}] spreads={roll['spread_candidate_count']:3d} "
                f"sample={len(items):2d} (fetch={f_cnt:2d}, cached={c_cnt:2d}) | {roll['title']}"
            )
        return 0

    fetched_global = 0
    bytes_global = 0
    started_at = time.time()
    progress_path = DATA_DIR / "pass1_progress.json"

    for r_idx, (roll, items) in enumerate(plan, start=1):
        mid = str(roll["manifest_id"])
        csv_path = INDEX_DIR / f"slides_{mid}.csv"
        rows_by_slide = load_existing_slides(csv_path)
        roll_fetched = 0
        roll_cached = 0

        print(f"\n=== Roll {r_idx}/{len(plan)} [{mid}] {roll['title']} ({len(items)} slides) ===", flush=True)
        for s_idx, (s, c, url, dest) in enumerate(items, start=1):
            rel_path = str(dest.relative_to(BASE_DIR))
            if dest.exists():
                nbytes = dest.stat().st_size
                roll_cached += 1
            else:
                if fetched_global > 0:
                    pause = random.uniform(args.min_delay, max(args.min_delay, args.max_delay))
                    time.sleep(pause)
                status, nbytes, cf_cache = download_image(url, dest)
                fetched_global += 1
                roll_fetched += 1
                bytes_global += nbytes
                elapsed = time.time() - started_at
                rem = max(0, total_to_fetch - fetched_global)
                eta_min = (rem * (elapsed / fetched_global)) / 60.0 if fetched_global else 0.0
                print(
                    f"  [{fetched_global:4d}/{total_to_fetch}] {mid} p{s:04d} ({s_idx}/{len(items)}) | "
                    f"HTTP {status} {nbytes:,}B cf={cf_cache or 'n/a'} | ETA ~{eta_min:.1f}m",
                    flush=True,
                )
                append_log(
                    mid,
                    {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "manifest_id": mid,
                        "slide_index": s,
                        "url": url,
                        "status": status,
                        "bytes": nbytes,
                        "cf_cache_status": cf_cache,
                        "local_path": rel_path,
                    },
                )

            prev = rows_by_slide.get(s, {})
            rows_by_slide[s] = {
                "manifest_id": mid,
                "slide_index": s,
                "canvas_index": c["canvas_index"],
                "canvas_id": c["canvas_id"],
                "canvas_label": c["canvas_label"],
                "iiif_image_id": c["iiif_image_id"],
                "iiif_service_url": c["iiif_service_url"],
                "iiif_crop_url": url,
                "iiif_full_image_url": c["iiif_full_image_url"],
                "iiif_viewer_link": c["iiif_viewer_link"],
                "uic_viewer_url": c["uic_viewer_url"],
                "width": c["width"],
                "height": c["height"],
                "region": args.region,
                "requested_width": args.width,
                "local_path": rel_path,
                "bytes": nbytes,
                "slide_type": prev.get("slide_type", ""),
                "ledger_page_left": prev.get("ledger_page_left", ""),
                "ledger_page_right": prev.get("ledger_page_right", ""),
                "observed_date_start": prev.get("observed_date_start", ""),
                "observed_date_end": prev.get("observed_date_end", ""),
                "permit_no_first": prev.get("permit_no_first", ""),
                "region_note": prev.get("region_note", ""),
                "order_status": prev.get("order_status", "pending"),
            }
            save_slides_index(mid, rows_by_slide)

        print(f"  Roll [{mid}] complete: {roll_fetched} fetched, {roll_cached} cached.", flush=True)
        progress_path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "rolls_completed": r_idx,
                    "rolls_total": len(plan),
                    "last_manifest_id": mid,
                    "slides_fetched_this_run": fetched_global,
                    "slides_to_fetch_planned": total_to_fetch,
                    "bytes_fetched_this_run": bytes_global,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    elapsed_min = (time.time() - started_at) / 60.0
    print(
        f"\nPass 1 COMPLETE: {len(plan)} rolls | {fetched_global} fetched ({bytes_global / (1024 * 1024):.1f} MB) | "
        f"{total_cached} already cached | {elapsed_min:.1f} min elapsed.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
