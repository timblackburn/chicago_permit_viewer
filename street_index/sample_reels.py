#!/usr/bin/env python3
"""Politely sample street-index card images from UIC IIIF reels (supports Pass 1 stride, Pass 2/3 queues, and pilots)."""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SI_DIR = Path(__file__).resolve().parent
DATA_DIR = SI_DIR / "data"
INDEX_DIR = SI_DIR / "index"

USER_AGENT = "ChicagoBuildingPermitsIndexer/1.0 (historical research; honors robots.txt Crawl-delay)"
IIIF_VIEWER_TEMPLATE = "https://uv-v4.netlify.app/#?manifest={manifest_url}&cv={canvas_index}"

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


def region_slug(region: str) -> str:
    return region.replace(":", "_").replace(",", "-")


def build_iiif_url(img_id: str, region: str, width: str, quality: str = "default", canvas_width: int = 0) -> str:
    if width.lower() == "max":
        size_part = "max"
    else:
        req_w = int(width)
        if canvas_width > 0 and req_w > canvas_width:
            req_w = canvas_width
        size_part = f"{req_w},"
    return f"https://digital.library.uic.edu/iiif/3/{img_id}/{region}/{size_part}/0/{quality}.jpg"


def choose_step_slides(canvases: list[dict], step: int = 500) -> list[int]:
    """Select [first_card, step, 2*step, ..., last_card], skipping oversized target cards at reel ends."""
    if not canvases:
        return []
    ws = sorted(c["w"] for c in canvases if c.get("w", 0) > 0)
    hs = sorted(c["h"] for c in canvases if c.get("h", 0) > 0)
    med_w = ws[len(ws) // 2] if ws else 0
    med_h = hs[len(hs) // 2] if hs else 0

    card_slides = [
        int(c["s"])
        for c in canvases
        if med_w == 0 or (0.75 * med_w <= c["w"] <= 1.25 * med_w and 0.75 * med_h <= c["h"] <= 1.25 * med_h)
    ]
    if not card_slides:
        card_slides = [int(c["s"]) for c in canvases]

    first_s, last_s = card_slides[0], card_slides[-1]
    chosen = {first_s, last_s}
    s = step
    while s < last_s:
        if s >= first_s:
            chosen.add(s)
        s += step
    return sorted(chosen)


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
    backoff = 30.0
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "image/jpeg,image/*;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                cf_mit = resp.headers.get("cf-mitigated", "")
                cf_cache = resp.headers.get("cf-cache-status", "")
                ctype = resp.headers.get("content-type", "")
                if cf_mit:
                    raise RuntimeError(f"CIRCUIT BREAKER: Cloudflare challenge ({cf_mit}) on {url}; aborting.")
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
                    f"CIRCUIT BREAKER: HTTP {exc.code} (cf-mitigated={cf_mit}) on {url}; aborting."
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
    parser = argparse.ArgumentParser(description="Politely sample street-index card images from UIC IIIF reels.")
    parser.add_argument("--manifest-ids", help="Comma-separated manifest IDs (e.g. 1038601). Default: all reels in reels.json.")
    parser.add_argument("--reels", help="Comma-separated reel numbers (e.g. 1,2,45).")
    parser.add_argument("--slides", help="Explicit comma-separated 1-based slide numbers (used with single/selected reels).")
    parser.add_argument("--queue", help="Path to Pass-2/Pass-3 JSON queue: list of {manifest_id, slide_index, pass_name, anchor_slide}.")
    parser.add_argument("--step", type=int, default=500, help="Slide stride for Pass 1 (default: 500).")
    parser.add_argument("--region", default="pct:0,0,100,99", help="IIIF region parameter (default: pct:0,0,100,99; full card bypassing /full/ 512px clamp).")
    parser.add_argument("--width", default="1000", help="IIIF output width in px (default: 1000).")
    parser.add_argument("--quality", default="default", choices=["default", "gray", "color"])
    parser.add_argument("--pass-name", default="pass1", help="Pass label recorded in slides_<id>.csv (default: pass1).")
    parser.add_argument("--min-delay", type=float, default=6.0, help="Min delay between HTTP requests (default: 6.0s).")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max delay between HTTP requests (default: 8.0s).")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without downloading.")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s per UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    reels_path = INDEX_DIR / "reels.json"
    if not reels_path.exists():
        print(f"Not found: {reels_path}. Run street_index/fetch_reel_manifests.py first.", file=sys.stderr)
        return 1

    all_reels = json.loads(reels_path.read_text(encoding="utf-8"))
    reels_by_mid = {str(r["manifest_id"]): r for r in all_reels}

    # Build work items grouped by manifest_id: list of (slide_index, pass_name, anchor_slide)
    work_by_mid: dict[str, list[tuple[int, str, str]]] = {}

    if args.queue:
        q_items = json.loads(Path(args.queue).read_text(encoding="utf-8"))
        for q in q_items:
            mid = str(q["manifest_id"])
            if mid not in reels_by_mid:
                print(f"Warning: manifest {mid} in queue not found in reels.json; skipping.", file=sys.stderr)
                continue
            work_by_mid.setdefault(mid, []).append(
                (int(q["slide_index"]), str(q.get("pass_name", args.pass_name)), str(q.get("anchor_slide", "")))
            )
    else:
        selected_reels = all_reels
        if args.manifest_ids:
            want_mids = {x.strip() for x in args.manifest_ids.split(",") if x.strip()}
            selected_reels = [r for r in selected_reels if str(r["manifest_id"]) in want_mids]
        if args.reels:
            want_rnos = {int(x.strip()) for x in args.reels.split(",") if x.strip()}
            selected_reels = [r for r in selected_reels if r.get("reel_number") in want_rnos]

        explicit_slides = (
            sorted({int(s.strip()) for s in args.slides.split(",") if s.strip()}) if args.slides else None
        )
        for r in selected_reels:
            mid = str(r["manifest_id"])
            total_c = int(r["total_canvases"])
            if explicit_slides is not None:
                s_list = [s for s in explicit_slides if 1 <= s <= total_c]
            else:
                cp = INDEX_DIR / f"canvases_{mid}.json"
                cs = json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else []
                s_list = choose_step_slides(cs, args.step)
            work_by_mid[mid] = [(s, args.pass_name, str(s)) for s in s_list]

    reg_tag = region_slug(args.region)
    plan: list[tuple[dict, list[tuple[int, dict, str, Path, str, str]]]] = []
    total_slides = 0
    total_cached = 0
    total_to_fetch = 0

    for mid, slide_specs in work_by_mid.items():
        reel = reels_by_mid[mid]
        canvases_path = INDEX_DIR / f"canvases_{mid}.json"
        if not canvases_path.exists():
            print(f"Warning: missing {canvases_path}; skipping {mid}.", file=sys.stderr)
            continue
        canvases = json.loads(canvases_path.read_text(encoding="utf-8"))
        by_slide = {int(c["s"]): c for c in canvases}
        slides_dir = DATA_DIR / mid / f"slides_{reg_tag}_w{args.width}"
        items = []
        seen_s: set[int] = set()
        for s, pname, anchor in sorted(slide_specs, key=lambda x: x[0]):
            if s in seen_s or s not in by_slide:
                continue
            seen_s.add(s)
            c = by_slide[s]
            url = build_iiif_url(c["id"], args.region, args.width, args.quality, canvas_width=int(c.get("w", 0)))
            dest = slides_dir / f"p{s:05d}_{c['id']}.jpg"
            items.append((s, c, url, dest, pname, anchor))
            total_slides += 1
            if dest.exists():
                total_cached += 1
            else:
                total_to_fetch += 1
        if items:
            plan.append((reel, items))

    avg_delay = (args.min_delay + max(args.min_delay, args.max_delay)) / 2.0
    est_sec = total_to_fetch * (avg_delay + 1.0)
    print(
        f"Street Index Plan: {len(plan)} reels | {total_slides} slides ({total_cached} cached, {total_to_fetch} to fetch) | "
        f"region={args.region} w={args.width} | est ~{est_sec / 60:.1f} min ({est_sec / 3600:.2f} hr)",
        flush=True,
    )

    if args.dry_run:
        for idx, (reel, items) in enumerate(plan, start=1):
            c_cnt = sum(1 for _, _, _, d, _, _ in items if d.exists())
            f_cnt = len(items) - c_cnt
            print(
                f"  ({idx:3d}/{len(plan)}) [Reel {str(reel.get('reel_number') or '?'):>3s} | {reel['manifest_id']}] "
                f"canvases={reel['total_canvases']:5d} sample={len(items):3d} (fetch={f_cnt:3d}, cached={c_cnt:3d}) | "
                f"{reel['start_address_raw']} -> {reel['end_address_raw']}"
            )
        return 0

    fetched_global = 0
    bytes_global = 0
    started_at = time.time()
    progress_path = DATA_DIR / f"{args.pass_name}_progress.json"

    for r_idx, (reel, items) in enumerate(plan, start=1):
        mid = str(reel["manifest_id"])
        csv_path = INDEX_DIR / f"slides_{mid}.csv"
        rows_by_slide = load_existing_slides(csv_path)
        roll_fetched = 0
        roll_cached = 0

        print(
            f"\n=== Reel {r_idx}/{len(plan)} [Reel {reel.get('reel_number') or '?'} | {mid}] "
            f"{reel['start_address_raw']} -> {reel['end_address_raw']} ({len(items)} slides) ===",
            flush=True,
        )
        for s_idx, (s, c, url, dest, pname, anchor) in enumerate(items, start=1):
            rel_path = str(dest.relative_to(SI_DIR.parent))
            missing_404 = False
            if dest.exists():
                nbytes = dest.stat().st_size
                roll_cached += 1
            else:
                if fetched_global > 0:
                    time.sleep(random.uniform(args.min_delay, max(args.min_delay, args.max_delay)))
                try:
                    status, nbytes, cf_cache = download_image(url, dest)
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        missing_404 = True
                        status, nbytes, cf_cache = 404, 0, ""
                        fetched_global += 1
                        print(
                            f"  [{fetched_global:4d}/{total_to_fetch}] {mid} p{s:05d} ({s_idx}/{len(items)}) | "
                            f"HTTP 404 (skipping, logged as missing_404)",
                            flush=True,
                        )
                        append_log(
                            mid,
                            {
                                "ts": datetime.now(timezone.utc).isoformat(),
                                "manifest_id": mid,
                                "slide_index": s,
                                "pass_name": pname,
                                "url": url,
                                "status": 404,
                                "bytes": 0,
                                "cf_cache_status": "",
                                "local_path": "",
                            },
                        )
                    else:
                        raise
                if not missing_404:
                    fetched_global += 1
                    roll_fetched += 1
                    bytes_global += nbytes
                    elapsed = time.time() - started_at
                    rem = max(0, total_to_fetch - fetched_global)
                    eta_min = (rem * (elapsed / fetched_global)) / 60.0 if fetched_global else 0.0
                    print(
                        f"  [{fetched_global:4d}/{total_to_fetch}] {mid} p{s:05d} ({s_idx}/{len(items)}) | "
                        f"HTTP {status} {nbytes:,}B cf={cf_cache or 'n/a'} | ETA ~{eta_min:.1f}m",
                        flush=True,
                    )
                    append_log(
                        mid,
                        {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "manifest_id": mid,
                            "slide_index": s,
                            "pass_name": pname,
                            "url": url,
                            "status": status,
                            "bytes": nbytes,
                            "cf_cache_status": cf_cache,
                            "local_path": rel_path,
                        },
                    )

            prev = rows_by_slide.get(s, {})
            rows_by_slide[s] = {
                "reel_number": reel.get("reel_number") or "",
                "manifest_id": mid,
                "slide_index": s,
                "canvas_index": c["ci"],
                "iiif_image_id": c["id"],
                "iiif_crop_url": url,
                "iiif_viewer_link": IIIF_VIEWER_TEMPLATE.format(
                    manifest_url=reel["manifest_url"], canvas_index=c["ci"]
                ),
                "uic_viewer_url": reel["uic_viewer_url"],
                "width": c["w"],
                "height": c["h"],
                "region": args.region,
                "requested_width": args.width,
                "local_path": "" if missing_404 else rel_path,
                "bytes": nbytes,
                "pass_name": prev.get("pass_name") or pname,
                "anchor_slide": prev.get("anchor_slide") or anchor,
                "legibility": "missing_404" if missing_404 else prev.get("legibility", "pending"),
                "street_name_legible": prev.get("street_name_legible", ""),
                "house_num_legible": prev.get("house_num_legible", ""),
                "street_name": prev.get("street_name", ""),
                "direction": prev.get("direction", ""),
                "house_number": prev.get("house_number", ""),
                "permit_date": prev.get("permit_date", ""),
                "permit_no": prev.get("permit_no", ""),
                "notes": prev.get("notes", ""),
            }
            save_slides_index(mid, rows_by_slide)

        print(f"  Reel [{mid}] complete: {roll_fetched} fetched, {roll_cached} cached.", flush=True)
        progress_path.write_text(
            json.dumps(
                {
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "reels_completed": r_idx,
                    "reels_total": len(plan),
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
        f"\nSampling COMPLETE ({args.pass_name}): {len(plan)} reels | {fetched_global} fetched "
        f"({bytes_global / (1024 * 1024):.1f} MB) | {total_cached} cached | {elapsed_min:.1f} min.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
