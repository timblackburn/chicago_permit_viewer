#!/usr/bin/env python3
"""Ledger Pass 2: politely fetch bisection midpoints, edge walk-ins, and illegible-anchor neighbors.

Reads index/pass1_audit_report.json (written by extract_ledgers_workflow.py). Safe to rerun:
cached files are skipped. After this, rerun extract_ledgers_workflow.py to annotate the new slides
(it only picks up slides with an empty slide_type), then rerun the audit to find the next bisection round.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from sample_download import (
    BASE_DIR,
    DATA_DIR,
    INDEX_DIR,
    append_log,
    build_iiif_url,
    download_image,
    load_existing_slides,
    region_slug,
    save_slides_index,
)


def build_queue(only_ids: set[str] | None) -> list[tuple[str, int, str]]:
    report = json.loads((INDEX_DIR / "pass1_audit_report.json").read_text(encoding="utf-8"))
    canv_cache: dict[str, dict[int, dict]] = {}

    def canv(mid: str) -> dict[int, dict]:
        if mid not in canv_cache:
            cs = json.loads((INDEX_DIR / f"canvases_{mid}.json").read_text(encoding="utf-8"))
            canv_cache[mid] = {int(c["slide_index"]): c for c in cs}
        return canv_cache[mid]

    queue: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()

    def add(mid: str, s: int, why: str) -> None:
        if only_ids and mid not in only_ids:
            return
        if s in canv(mid) and (mid, s) not in seen:
            seen.add((mid, s))
            queue.append((mid, s, why))

    for b in report.get("out_of_order_breaks", []):
        mid_id = str(b["manifest_id"])
        if mid_id == "816751" and int(b["slide_a"]) >= 305:
            continue
        if b.get("midpoint"):
            add(mid_id, int(b["midpoint"]), f"bisect {b['slide_a']}-{b['slide_b']}")
    for e in report.get("edge_issues", []):
        mid = str(e["manifest_id"])
        lo, hi = e["slides"]
        if e["edge"] == "start":
            add(mid, hi + 1, "edge walk-in")
            add(mid, hi + 2, "edge walk-in")
        else:
            add(mid, lo - 1, "edge walk-in")
            add(mid, lo - 2, "edge walk-in")
    for i in report.get("illegible_anchors", []):
        mid, s = str(i["manifest_id"]), int(i["slide_index"])
        add(mid, s + 1, "illegible retry")
        add(mid, s - 1, "illegible retry")
    return queue


def main() -> int:
    ap = argparse.ArgumentParser(description="Ledger Pass 2 polite downloader.")
    ap.add_argument("--region", default="pct:0,0,100,25")
    ap.add_argument("--width", default="2500")
    ap.add_argument("--min-delay", type=float, default=17.0, help="default 17s (gentler than other jobs)")
    ap.add_argument("--max-delay", type=float, default=24.0)
    ap.add_argument("--ids", help="Comma-separated manifest IDs to limit to.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.min_delay < 5.0:
        print("--min-delay must be >= 5.0", file=sys.stderr)
        return 2

    only = {x.strip() for x in args.ids.split(",")} if args.ids else None
    queue = build_queue(only)
    reg_tag = region_slug(args.region)
    rolls = {str(r["manifest_id"]): r for r in json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8"))}

    todo = []
    for mid, s, why in queue:
        cs = {int(c["slide_index"]): c for c in json.loads((INDEX_DIR / f"canvases_{mid}.json").read_text(encoding="utf-8"))}
        c = cs[s]
        dest = DATA_DIR / mid / f"slides_{reg_tag}_w{args.width}" / f"p{s:04d}_{c['iiif_image_id']}.jpg"
        todo.append((mid, s, why, c, dest))
    n_fetch = sum(1 for t in todo if not t[4].exists())
    print(f"Ledger Pass 2: {len(todo)} slides queued ({n_fetch} to fetch) | delay {args.min_delay}-{args.max_delay}s | "
          f"est ~{n_fetch * (args.min_delay + args.max_delay) / 2 / 60:.1f} min", flush=True)
    if args.dry_run:
        for mid, s, why, _, dest in todo:
            print(f"  [{'CACHED' if dest.exists() else 'FETCH'}] {mid} p{s:04d} ({why}) | {rolls[mid]['title']}")
        return 0

    fetched = 0
    for mid, s, why, c, dest in todo:
        url = build_iiif_url(c["iiif_service_url"], args.region, args.width)
        rel = str(dest.relative_to(BASE_DIR))
        if not dest.exists():
            if fetched > 0:
                time.sleep(random.uniform(args.min_delay, max(args.min_delay, args.max_delay)))
            try:
                status, nbytes, cf = download_image(url, dest)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    print(f"  {mid} p{s:04d} HTTP 404, skipped", flush=True)
                    continue
                raise
            fetched += 1
            print(f"  [{fetched}/{n_fetch}] {mid} p{s:04d} ({why}) HTTP {status} {nbytes:,}B", flush=True)
            append_log(mid, {"ts": datetime.now(timezone.utc).isoformat(), "manifest_id": mid, "slide_index": s,
                             "pass": "ledger_pass2", "reason": why, "url": url, "status": status, "bytes": nbytes,
                             "cf_cache_status": cf, "local_path": rel})
        else:
            nbytes = dest.stat().st_size
        rows = load_existing_slides(INDEX_DIR / f"slides_{mid}.csv")
        if s not in rows:
            rows[s] = {
                "manifest_id": mid, "slide_index": s, "canvas_index": c["canvas_index"], "canvas_id": c["canvas_id"],
                "canvas_label": c["canvas_label"], "iiif_image_id": c["iiif_image_id"],
                "iiif_service_url": c["iiif_service_url"], "iiif_crop_url": url,
                "iiif_full_image_url": c["iiif_full_image_url"], "iiif_viewer_link": c["iiif_viewer_link"],
                "uic_viewer_url": c["uic_viewer_url"], "width": c["width"], "height": c["height"],
                "region": args.region, "requested_width": args.width, "local_path": rel, "bytes": nbytes,
                "slide_type": "", "order_status": "pending", "region_note": f"pass2: {why}",
            }
            save_slides_index(mid, rows)
    print(f"Ledger Pass 2 complete: {fetched} fetched.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
