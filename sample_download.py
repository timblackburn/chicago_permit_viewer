#!/usr/bin/env python3
"""Politely sample and download IIIF slide crops (or full spreads) for a UIC microfilm manifest."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INDEX_DIR = BASE_DIR / "index"

USER_AGENT = "ChicagoBuildingPermitsIndexer/1.0 (historical research; honors robots.txt Crawl-delay)"

SLIDE_CSV_FIELDS = [
    "manifest_id",
    "slide_index",
    "canvas_index",
    "canvas_id",
    "canvas_label",
    "iiif_image_id",
    "iiif_service_url",
    "iiif_crop_url",
    "iiif_full_image_url",
    "iiif_viewer_link",
    "uic_viewer_url",
    "width",
    "height",
    "region",
    "requested_width",
    "local_path",
    "bytes",
    "slide_type",
    "ledger_page_left",
    "ledger_page_right",
    "observed_date_start",
    "observed_date_end",
    "permit_no_first",
    "region_note",
    "order_status",
]


def choose_stratified_slides(
    canvases: list[dict],
    target_percent: float = 6.0,
    min_slides: int = 10,
    max_slides: int = 24,
) -> list[int]:
    spreads = [c["slide_index"] for c in canvases if c.get("is_spread_candidate")]
    if not spreads:
        spreads = [c["slide_index"] for c in canvases]
    if not spreads:
        return []

    first_s, last_s = spreads[0], spreads[-1]
    n = len(spreads)

    target_total = max(min_slides, min(max_slides, round(n * (target_percent / 100.0))))
    target_total = min(target_total, n)

    chosen: set[int] = {first_s, last_s}
    if n >= 2:
        chosen.add(spreads[1])
        chosen.add(spreads[-2])

    target_interior = max(0, target_total - len(chosen))
    # Evenly distribute interior points across spreads[1:-1]
    if target_interior > 0 and n > 4:
        step = (n - 1) / (target_interior + 1)
        for k in range(1, target_interior + 1):
            pos = round(k * step)
            pos = max(0, min(n - 1, pos))
            chosen.add(spreads[pos])

    return sorted(chosen)


def region_slug(region: str) -> str:
    return region.replace(":", "_").replace(",", "-")


def build_iiif_url(service_url: str, region: str, width: str, quality: str = "default") -> str:
    # IIIF Image API 3.0: {scheme}://{server}{/prefix}/{identifier}/{region}/{size}/{rotation}/{quality}.{format}
    size_part = "max" if width.lower() == "max" else f"{width},"
    # Keep colons and commas unescaped as required by IIIF Image API
    return f"{service_url}/{region}/{size_part}/0/{quality}.jpg"


def load_existing_slides(csv_path: Path) -> dict[int, dict]:
    if not csv_path.exists():
        return {}
    rows: dict[int, dict] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            rows[int(row["slide_index"])] = row
    return rows


def save_slides_index(manifest_id: str, rows_by_slide: dict[int, dict]) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    ordered = [rows_by_slide[k] for k in sorted(rows_by_slide.keys())]

    csv_path = INDEX_DIR / f"slides_{manifest_id}.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SLIDE_CSV_FIELDS)
        writer.writeheader()
        for row in ordered:
            writer.writerow({k: row.get(k, "") for k in SLIDE_CSV_FIELDS})

    json_path = INDEX_DIR / f"slides_{manifest_id}.json"
    json_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def append_log(manifest_id: str, entry: dict) -> None:
    log_path = DATA_DIR / manifest_id / "download_log.jsonl"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def download_image(url: str, dest: Path, max_retries: int = 3) -> tuple[int, int, str]:
    """Download an image with circuit-breaker on 403/Cloudflare and backoff on 429/5xx."""
    backoff = 30.0
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/*;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                cf_mit = resp.headers.get("cf-mitigated", "")
                cf_cache = resp.headers.get("cf-cache-status", "")
                ctype = resp.headers.get("content-type", "")
                if cf_mit:
                    raise RuntimeError(f"Cloudflare challenge header ({cf_mit}) on {url}; aborting immediately.")
                if "image" not in ctype.lower():
                    raise RuntimeError(f"Unexpected Content-Type '{ctype}' on {url}; aborting.")
                payload = resp.read()
                if len(payload) < 500 or not payload.startswith(b"\xff\xd8"):
                    raise RuntimeError(f"Response is not a valid JPEG ({len(payload)} bytes) for {url}.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                tmp = dest.with_suffix(".jpg.part")
                tmp.write_bytes(payload)
                tmp.replace(dest)
                return resp.status, len(payload), cf_cache
        except urllib.error.HTTPError as exc:
            cf_mit = exc.headers.get("cf-mitigated", "") if exc.headers else ""
            if exc.code == 403 or cf_mit:
                raise RuntimeError(
                    f"CIRCUIT BREAKER: HTTP {exc.code} (cf-mitigated={cf_mit}) on {url}. Stopping immediately."
                ) from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait_s = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                print(
                    f"  HTTP {exc.code}; backing off {wait_s:.0f}s (attempt {attempt}/{max_retries})...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_s)
                backoff *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if isinstance(getattr(exc, "reason", None), OSError) and getattr(exc.reason, "errno", None) == -3:
                raise
            if attempt < max_retries:
                print(
                    f"  Network error ({exc}); backing off {backoff:.0f}s (attempt {attempt}/{max_retries})...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            raise
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Politely download sampled IIIF slide crops for a UIC manifest.")
    parser.add_argument("manifest_id", help="Manifest ID (e.g. 806145)")
    parser.add_argument(
        "--slides",
        help="Explicit comma-separated 1-based slide numbers (e.g. 4,5,49). Default: auto stratified sample.",
    )
    parser.add_argument(
        "--percent",
        type=float,
        default=6.0,
        help="Target percentage of candidate spreads to sample when --slides is omitted. Default: 6.0.",
    )
    parser.add_argument("--min-slides", type=int, default=10, help="Minimum auto-sampled slides. Default: 10.")
    parser.add_argument("--max-slides", type=int, default=24, help="Maximum auto-sampled slides. Default: 24.")
    parser.add_argument(
        "--region",
        default="pct:0,0,100,25",
        help="IIIF region parameter. Default: 'pct:0,0,100,25' (top 25%% strip capturing both top corners in 1 request).",
    )
    parser.add_argument(
        "--width",
        default="2500",
        help="IIIF output width in px or 'max'. Default: '2500'.",
    )
    parser.add_argument(
        "--quality",
        default="default",
        choices=["default", "gray", "color"],
        help="IIIF image quality. Default: 'default'.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=6.0,
        help="Minimum delay in seconds between HTTP requests (must be >= 5.0 per UIC robots.txt). Default: 6.0.",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=8.0,
        help="Maximum delay in seconds between HTTP requests. Default: 8.0.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print planned requests without downloading.")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s to respect UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    mid = str(args.manifest_id).strip()
    canvases_path = INDEX_DIR / f"canvases_{mid}.json"
    if not canvases_path.exists():
        print(f"Canvas catalog not found: {canvases_path}. Run fetch_manifest.py {mid} first.", file=sys.stderr)
        return 1

    canvases = json.loads(canvases_path.read_text(encoding="utf-8"))
    by_slide = {int(c["slide_index"]): c for c in canvases}

    if args.slides:
        slide_nums = sorted({int(s.strip()) for s in args.slides.split(",") if s.strip()})
    else:
        slide_nums = choose_stratified_slides(
            canvases,
            target_percent=args.percent,
            min_slides=args.min_slides,
            max_slides=args.max_slides,
        )

    reg_tag = region_slug(args.region)
    slides_dir = DATA_DIR / mid / f"slides_{reg_tag}_w{args.width}"
    csv_path = INDEX_DIR / f"slides_{mid}.csv"
    rows_by_slide = load_existing_slides(csv_path)

    print(f"Manifest {mid}: {len(slide_nums)} slides planned -> {slide_nums}")
    print(f"Region={args.region}, Width={args.width}, Delay={args.min_delay:.1f}-{args.max_delay:.1f}s")

    pending: list[tuple[int, dict, str, Path]] = []
    for s in slide_nums:
        if s not in by_slide:
            print(f"  Warning: slide {s} out of range (1..{len(canvases)}); skipping.", file=sys.stderr)
            continue
        c = by_slide[s]
        url = build_iiif_url(c["iiif_service_url"], args.region, args.width, args.quality)
        dest = slides_dir / f"p{s:04d}_{c['iiif_image_id']}.jpg"
        pending.append((s, c, url, dest))
        if args.dry_run:
            status = "CACHED" if dest.exists() else "FETCH"
            print(f"  [{status}] p{s:04d} -> {url}")

    if args.dry_run:
        to_fetch = sum(1 for _, _, _, d in pending if not d.exists())
        est = max(0, to_fetch - 1) * ((args.min_delay + args.max_delay) / 2.0)
        print(f"Dry run complete: {to_fetch} to fetch, {len(pending) - to_fetch} cached (~{est:.0f}s estimated wait).")
        return 0

    fetched_count = 0
    for i, (s, c, url, dest) in enumerate(pending):
        rel_path = str(dest.relative_to(BASE_DIR))
        if dest.exists():
            nbytes = dest.stat().st_size
            print(f"  [CACHED] p{s:04d} ({nbytes:,} bytes) -> {rel_path}")
        else:
            if fetched_count > 0:
                pause = random.uniform(args.min_delay, max(args.min_delay, args.max_delay))
                print(f"  Sleeping {pause:.1f}s...")
                time.sleep(pause)
            print(f"  [FETCH {i + 1}/{len(pending)}] p{s:04d} -> {url}")
            status, nbytes, cf_cache = download_image(url, dest)
            fetched_count += 1
            print(f"    HTTP {status} | {nbytes:,} bytes | cf-cache={cf_cache or 'n/a'} -> {rel_path}")
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

    print(f"Done. Index updated: {csv_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
