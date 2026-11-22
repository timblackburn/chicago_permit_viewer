#!/usr/bin/env python3
"""Fetch (or read cached) UIC IIIF v3 manifest and build roll & canvas catalogs."""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
INDEX_DIR = BASE_DIR / "index"

USER_AGENT = "ChicagoBuildingPermitsIndexer/1.0 (historical research; honors robots.txt Crawl-delay)"
MANIFEST_URL_TEMPLATE = "https://digital.library.uic.edu/iiif/manifest/{manifest_id}"
ARK_MANIFEST_URL_TEMPLATE = "https://digital.library.uic.edu/iiif/collection/object/{ark_id}"
IIIF_VIEWER_TEMPLATE = "https://uv-v4.netlify.app/#?manifest={manifest_url}&cv={canvas_index}"


def get_metadata_map(manifest: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in manifest.get("metadata", []):
        labels = entry.get("label", {}).get("en", [])
        values = entry.get("value", {}).get("en", [])
        if labels:
            out[labels[0]] = values
    return out


def infer_era(title: str, date_raw: str) -> str:
    years = [int(y) for y in re.findall(r"\b(18\d{2}|19\d{2})\b", f"{title} {date_raw}")]
    if not years:
        return "unknown"
    lo, hi = min(years), max(years)
    if hi < 1912:
        return "pre-1912"
    if lo > 1913:
        return "post-1912"
    return "transition-1912-1913"


def extract_iiif_image_id(canvas: dict) -> str:
    try:
        body = canvas["items"][0]["items"][0]["body"]
        body_id = body.get("id", "")
        m = re.search(r"/iiif/[23]/(\d+)/", body_id)
        if m:
            return m.group(1)
        for svc in body.get("service", []):
            m = re.search(r"/iiif/[23]/(\d+)$", svc.get("id", ""))
            if m:
                return m.group(1)
    except (KeyError, IndexError, TypeError):
        pass
    raise ValueError(f"Could not extract IIIF image ID from canvas: {canvas.get('id')}")


def lookup_cached_id_by_ark(ark_id: str) -> str | None:
    rolls_path = INDEX_DIR / "rolls.json"
    if not rolls_path.exists():
        return None
    for r in json.loads(rolls_path.read_text(encoding="utf-8")):
        if r.get("ark_id") == ark_id:
            return str(r["manifest_id"])
    return None


def fetch_or_load_manifest(id_or_ark: str, force: bool = False, max_retries: int = 3) -> tuple[str, dict, bool]:
    """Return (manifest_id, manifest_dict, fetched_from_network)."""
    raw_arg = str(id_or_ark).strip().rstrip("/")
    is_ark = "ark:/" in raw_arg
    if is_ark:
        ark_id = raw_arg[raw_arg.index("ark:/") :]
        cached_mid = None if force else lookup_cached_id_by_ark(ark_id)
        if cached_mid:
            manifest_path = DATA_DIR / cached_mid / "manifest.json"
            if manifest_path.exists():
                return cached_mid, json.loads(manifest_path.read_text(encoding="utf-8")), False
        url = ARK_MANIFEST_URL_TEMPLATE.format(ark_id=ark_id)
    else:
        mid = raw_arg.split("/")[-1]
        manifest_path = DATA_DIR / mid / "manifest.json"
        if manifest_path.exists() and not force:
            return mid, json.loads(manifest_path.read_text(encoding="utf-8")), False
        url = MANIFEST_URL_TEMPLATE.format(manifest_id=mid)

    backoff = 30.0
    raw = ""
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                cf_mit = resp.headers.get("cf-mitigated", "")
                if cf_mit:
                    raise RuntimeError(f"CIRCUIT BREAKER: Cloudflare challenge ({cf_mit}) on {url}; aborting.")
                raw = resp.read().decode("utf-8")
                break
        except urllib.error.HTTPError as exc:
            cf = exc.headers.get("cf-mitigated", "") if exc.headers else ""
            if exc.code == 403 or cf:
                raise RuntimeError(f"CIRCUIT BREAKER: HTTP {exc.code} (cf-mitigated={cf}) on {url}; aborting.") from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait_s = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                print(f"  HTTP {exc.code}; backing off {wait_s:.0f}s (attempt {attempt}/{max_retries})...", file=sys.stderr)
                time.sleep(wait_s)
                backoff *= 2
                continue
            raise RuntimeError(f"HTTP {exc.code} fetching {url} (cf-mitigated={cf})") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < max_retries:
                print(f"  Network error ({exc}); backing off {backoff:.0f}s (attempt {attempt}/{max_retries})...", file=sys.stderr)
                time.sleep(backoff)
                backoff *= 2
                continue
            raise

    data = json.loads(raw)
    m = re.search(r"/iiif/manifest/(\d+)", data.get("id", ""))
    mid = m.group(1) if m else (raw_arg.split("/")[-1] if not is_ark else "")
    if not mid:
        raise RuntimeError(f"Could not determine numeric manifest_id from response for {url}")

    roll_dir = DATA_DIR / mid
    roll_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = roll_dir / "manifest.json"
    tmp = manifest_path.with_suffix(".json.part")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(manifest_path)
    return mid, data, True


def build_catalog(manifest_id: str, manifest: dict) -> tuple[dict, list[dict]]:
    meta = get_metadata_map(manifest)
    title = (manifest.get("label", {}).get("en") or meta.get("Title") or [""])[0]
    reel_uid = (meta.get("Identifier") or [""])[0]
    ark_id = (meta.get("ARK Identifier") or [""])[0]
    date_raw = (meta.get("Date of Original") or [""])[0]
    extent = (meta.get("Extent") or [""])[0]
    homepage = ""
    if manifest.get("homepage"):
        homepage = manifest["homepage"][0].get("id", "")
    manifest_url = manifest.get("id") or MANIFEST_URL_TEMPLATE.format(manifest_id=manifest_id)
    ark_permalink = f"https://n2t.net/{ark_id}" if ark_id else ""

    items = manifest.get("items", [])
    widths = [c.get("width", 0) for c in items if c.get("width", 0) > 0]
    median_w = statistics.median(widths) if widths else 0

    canvases: list[dict] = []
    for idx, c in enumerate(items, start=1):
        w = int(c.get("width", 0))
        h = int(c.get("height", 0))
        aspect = round(w / h, 3) if h else 0.0
        is_spread = bool(w > h and aspect >= 1.25 and (median_w == 0 or w >= 0.8 * median_w))
        img_id = extract_iiif_image_id(c)
        canvas_index = idx - 1
        iiif_service = f"https://digital.library.uic.edu/iiif/3/{img_id}"
        canvases.append(
            {
                "manifest_id": manifest_id,
                "slide_index": idx,
                "canvas_index": canvas_index,
                "canvas_id": c.get("id", ""),
                "canvas_label": (c.get("label", {}).get("en") or [""])[0],
                "iiif_image_id": img_id,
                "iiif_service_url": iiif_service,
                "iiif_full_image_url": f"{iiif_service}/full/2500,/0/default.jpg",
                "iiif_viewer_link": IIIF_VIEWER_TEMPLATE.format(
                    manifest_url=manifest_url, canvas_index=canvas_index
                ),
                "uic_viewer_url": homepage,
                "width": w,
                "height": h,
                "aspect_ratio": aspect,
                "is_spread_candidate": is_spread,
            }
        )

    spread_indices = [c["slide_index"] for c in canvases if c["is_spread_candidate"]]
    first_spread = spread_indices[0] if spread_indices else (1 if canvases else None)
    last_spread = spread_indices[-1] if spread_indices else (len(canvases) if canvases else None)

    roll_record = {
        "manifest_id": manifest_id,
        "reel_uid": reel_uid,
        "ark_id": ark_id,
        "ark_permalink": ark_permalink,
        "uic_viewer_url": homepage,
        "manifest_url": manifest_url,
        "title": title,
        "date_range_raw": date_raw,
        "era": infer_era(title, date_raw),
        "extent": extent,
        "total_canvases": len(canvases),
        "spread_candidate_count": len(spread_indices),
        "first_spread_candidate": first_spread,
        "last_spread_candidate": last_spread,
        "multi_region_flagged": None,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    return roll_record, canvases


def upsert_roll(roll_record: dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    rolls_path = INDEX_DIR / "rolls.json"
    rolls: dict[str, dict] = {}
    if rolls_path.exists():
        existing = json.loads(rolls_path.read_text(encoding="utf-8"))
        for r in existing:
            rolls[str(r["manifest_id"])] = r
    mid_key = str(roll_record["manifest_id"])
    if mid_key in rolls and roll_record.get("multi_region_flagged") is None:
        roll_record["multi_region_flagged"] = rolls[mid_key].get("multi_region_flagged")
    rolls[mid_key] = roll_record
    ordered = [rolls[k] for k in sorted(rolls.keys())]
    rolls_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/parse UIC IIIF manifests into local index catalogs.")
    parser.add_argument(
        "manifest_ids",
        nargs="*",
        help="One or more numeric manifest IDs (e.g. 806145) or ARKs (e.g. ark:/81984/d31n7xw9g)",
    )
    parser.add_argument(
        "--from-catalog",
        help="Path to collection_31_ledgers.json (or similar JSON list containing ark_id entries).",
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch manifest even if cached")
    parser.add_argument("--min-delay", type=float, default=6.0, help="Min delay between network fetches (default: 6.0s)")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max delay between network fetches (default: 8.0s)")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s per UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    targets: list[str] = list(args.manifest_ids)
    if args.from_catalog:
        cat_items = json.loads(Path(args.from_catalog).read_text(encoding="utf-8"))
        for entry in cat_items:
            ark = entry.get("ark_id") or entry.get("manifest_id")
            if ark:
                targets.append(str(ark))

    if not targets:
        print("Error: provide manifest_ids/ARKs or --from-catalog.", file=sys.stderr)
        return 2

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fetched_count = 0
    total = len(targets)
    for idx, item in enumerate(targets, start=1):
        # Peek cache first so we don't sleep unnecessarily before cached manifests
        raw_arg = str(item).strip().rstrip("/")
        will_fetch = args.force
        if not will_fetch:
            if "ark:/" in raw_arg:
                ark_id = raw_arg[raw_arg.index("ark:/") :]
                cmid = lookup_cached_id_by_ark(ark_id)
                will_fetch = not (cmid and (DATA_DIR / cmid / "manifest.json").exists())
            else:
                cmid = raw_arg.split("/")[-1]
                will_fetch = not (DATA_DIR / cmid / "manifest.json").exists()

        if will_fetch and fetched_count > 0:
            pause = random.uniform(args.min_delay, max(args.min_delay, args.max_delay))
            time.sleep(pause)

        mid, manifest, did_fetch = fetch_or_load_manifest(item, force=args.force)
        if did_fetch:
            fetched_count += 1
        roll_record, canvases = build_catalog(mid, manifest)
        upsert_roll(roll_record)
        canvases_path = INDEX_DIR / f"canvases_{mid}.json"
        canvases_path.write_text(json.dumps(canvases, indent=2, ensure_ascii=False), encoding="utf-8")
        src = "FETCHED" if did_fetch else "CACHED"
        print(
            f"({idx:2d}/{total}) [{mid} {src}] {roll_record['title']} | reel={roll_record['reel_uid']} | "
            f"canvases={roll_record['total_canvases']} | "
            f"spreads={roll_record['first_spread_candidate']}..{roll_record['last_spread_candidate']} "
            f"({roll_record['spread_candidate_count']})",
            flush=True,
        )
    print(f"\nManifest pass complete: {total} total ({fetched_count} fetched, {total - fetched_count} cached).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
