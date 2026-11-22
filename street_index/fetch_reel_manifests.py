#!/usr/bin/env python3
"""Pass 0 for Street Index: fetch/cache IIIF v3 manifests (gzipped) for the 138 street index reels."""

from __future__ import annotations

import argparse
import gzip
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

SI_DIR = Path(__file__).resolve().parent
DATA_DIR = SI_DIR / "data"
INDEX_DIR = SI_DIR / "index"

USER_AGENT = "ChicagoBuildingPermitsIndexer/1.0 (historical research; honors robots.txt Crawl-delay)"
MANIFEST_URL_TEMPLATE = "https://digital.library.uic.edu/iiif/manifest/{manifest_id}"
ARK_MANIFEST_URL_TEMPLATE = "https://digital.library.uic.edu/iiif/collection/object/{ark_id}"


def get_metadata_map(manifest: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for entry in manifest.get("metadata", []):
        labels = entry.get("label", {}).get("en", [])
        values = entry.get("value", {}).get("en", [])
        if labels:
            out[labels[0]] = values
    return out


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


def parse_reel_title(title: str) -> tuple[int | None, str, str]:
    m_num = re.search(r"reel\s+(\d+)\s*:\s*(.+)$", title, re.I)
    if not m_num:
        return None, "", ""
    reel_no = int(m_num.group(1))
    rest = m_num.group(2).strip()
    if re.search(r"\s+to\s+", rest, re.I):
        start_addr, end_addr = re.split(r"\s+to\s+", rest, maxsplit=1, flags=re.I)
    elif "," in rest:
        start_addr, end_addr = [p.strip() for p in rest.split(",", 1)]
    else:
        m_dir = re.match(r"^(.+?\b(?:North|South|East|West))\s+(.+)$", rest, re.I)
        if m_dir:
            start_addr, end_addr = m_dir.group(1).strip(), m_dir.group(2).strip()
        else:
            start_addr, end_addr = rest, ""
    return reel_no, start_addr.strip(), end_addr.strip()


def lookup_cached_id_by_ark(ark_id: str) -> str | None:
    reels_path = INDEX_DIR / "reels.json"
    if not reels_path.exists():
        return None
    for r in json.loads(reels_path.read_text(encoding="utf-8")):
        if r.get("ark_id") == ark_id:
            return str(r["manifest_id"])
    return None


def manifest_cache_path(mid: str) -> Path:
    return DATA_DIR / mid / "manifest.json.gz"


def load_cached_manifest(mid: str) -> dict | None:
    gz = manifest_cache_path(mid)
    if gz.exists():
        with gzip.open(gz, "rt", encoding="utf-8") as f:
            return json.load(f)
    plain = DATA_DIR / mid / "manifest.json"
    if plain.exists():
        return json.loads(plain.read_text(encoding="utf-8"))
    return None


def fetch_or_load_manifest(id_or_ark: str, force: bool = False, max_retries: int = 3) -> tuple[str, dict, bool]:
    raw_arg = str(id_or_ark).strip().rstrip("/")
    is_ark = "ark:/" in raw_arg
    if is_ark:
        ark_id = raw_arg[raw_arg.index("ark:/") :]
        cached_mid = None if force else lookup_cached_id_by_ark(ark_id)
        if cached_mid:
            cached = load_cached_manifest(cached_mid)
            if cached is not None:
                return cached_mid, cached, False
        url = ARK_MANIFEST_URL_TEMPLATE.format(ark_id=ark_id)
    else:
        mid = raw_arg.split("/")[-1]
        if not force:
            cached = load_cached_manifest(mid)
            if cached is not None:
                return mid, cached, False
        url = MANIFEST_URL_TEMPLATE.format(manifest_id=mid)

    backoff = 30.0
    raw = ""
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
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
                print(
                    f"  HTTP {exc.code}; backing off {wait_s:.0f}s (attempt {attempt}/{max_retries})...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_s)
                backoff *= 2
                continue
            raise RuntimeError(f"HTTP {exc.code} fetching {url} (cf-mitigated={cf})") from exc
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

    data = json.loads(raw)
    m = re.search(r"/iiif/manifest/(\d+)", data.get("id", ""))
    mid = m.group(1) if m else (raw_arg.split("/")[-1] if not is_ark else "")
    if not mid:
        raise RuntimeError(f"Could not determine numeric manifest_id from response for {url}")

    roll_dir = DATA_DIR / mid
    roll_dir.mkdir(parents=True, exist_ok=True)
    gz_path = manifest_cache_path(mid)
    tmp = gz_path.with_suffix(".gz.part")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    tmp.replace(gz_path)
    return mid, data, True


def build_reel_catalog(manifest_id: str, manifest: dict) -> tuple[dict, list[dict]]:
    meta = get_metadata_map(manifest)
    title = (manifest.get("label", {}).get("en") or meta.get("Title") or [""])[0]
    reel_uid = (meta.get("Identifier") or [""])[0]
    ark_id = (meta.get("ARK Identifier") or [""])[0]
    extent = (meta.get("Extent") or [""])[0]
    homepage = manifest["homepage"][0].get("id", "") if manifest.get("homepage") else ""
    manifest_url = manifest.get("id") or MANIFEST_URL_TEMPLATE.format(manifest_id=manifest_id)
    ark_permalink = f"https://n2t.net/{ark_id}" if ark_id else ""
    reel_no, start_addr, end_addr = parse_reel_title(title)

    items = manifest.get("items", [])
    widths = [int(c.get("width", 0)) for c in items if int(c.get("width", 0)) > 0]
    heights = [int(c.get("height", 0)) for c in items if int(c.get("height", 0)) > 0]
    median_w = statistics.median(widths) if widths else 0
    median_h = statistics.median(heights) if heights else 0

    # Keep canvas rows compact (5,000 canvases per reel)
    canvases: list[dict] = []
    for idx, c in enumerate(items, start=1):
        w = int(c.get("width", 0))
        h = int(c.get("height", 0))
        img_id = extract_iiif_image_id(c)
        canvases.append(
            {
                "s": idx,           # 1-based slide_index
                "ci": idx - 1,      # 0-based canvas_index
                "id": img_id,       # iiif_image_id
                "w": w,
                "h": h,
            }
        )

    reel_record = {
        "reel_number": reel_no,
        "manifest_id": manifest_id,
        "reel_uid": reel_uid,
        "ark_id": ark_id,
        "ark_permalink": ark_permalink,
        "uic_viewer_url": homepage,
        "manifest_url": manifest_url,
        "title": title,
        "start_address_raw": start_addr,
        "end_address_raw": end_addr,
        "extent": extent,
        "total_canvases": len(canvases),
        "median_width": int(median_w),
        "median_height": int(median_h),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
    }
    return reel_record, canvases


def upsert_reel(reel_record: dict) -> None:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    reels_path = INDEX_DIR / "reels.json"
    reels: dict[str, dict] = {}
    if reels_path.exists():
        for r in json.loads(reels_path.read_text(encoding="utf-8")):
            reels[str(r["manifest_id"])] = r
    reels[str(reel_record["manifest_id"])] = reel_record
    ordered = sorted(
        reels.values(),
        key=lambda r: (r.get("reel_number") is None, r.get("reel_number") or 9999, str(r["manifest_id"])),
    )
    reels_path.write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/cache UIC street index IIIF manifests (gzipped).")
    parser.add_argument("targets", nargs="*", help="Manifest IDs (e.g. 1038601) or ARKs")
    parser.add_argument(
        "--from-catalog",
        help="Path to street_index/index/collection_reels.json to harvest all 138 reels.",
    )
    parser.add_argument("--force", action="store_true", help="Re-fetch even if cached")
    parser.add_argument("--min-delay", type=float, default=6.0, help="Min delay between fetches (default: 6.0s)")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max delay between fetches (default: 8.0s)")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s per UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    targets: list[str] = list(args.targets)
    if args.from_catalog:
        for entry in json.loads(Path(args.from_catalog).read_text(encoding="utf-8")):
            ark = entry.get("ark_id") or entry.get("manifest_id")
            if ark:
                targets.append(str(ark))

    if not targets:
        print("Error: provide manifest IDs/ARKs or --from-catalog.", file=sys.stderr)
        return 2

    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    fetched_count = 0
    total = len(targets)
    for idx, item in enumerate(targets, start=1):
        raw_arg = str(item).strip().rstrip("/")
        will_fetch = args.force
        if not will_fetch:
            if "ark:/" in raw_arg:
                ark_id = raw_arg[raw_arg.index("ark:/") :]
                cmid = lookup_cached_id_by_ark(ark_id)
                will_fetch = not (cmid and load_cached_manifest(cmid) is not None)
            else:
                cmid = raw_arg.split("/")[-1]
                will_fetch = load_cached_manifest(cmid) is None

        if will_fetch and fetched_count > 0:
            time.sleep(random.uniform(args.min_delay, max(args.min_delay, args.max_delay)))

        mid, manifest, did_fetch = fetch_or_load_manifest(item, force=args.force)
        if did_fetch:
            fetched_count += 1
        reel_record, canvases = build_reel_catalog(mid, manifest)
        upsert_reel(reel_record)
        canvases_path = INDEX_DIR / f"canvases_{mid}.json"
        canvases_path.write_text(json.dumps(canvases, separators=(",", ":")), encoding="utf-8")
        gz_kb = manifest_cache_path(mid).stat().st_size / 1024.0 if manifest_cache_path(mid).exists() else 0.0
        src = "FETCHED" if did_fetch else "CACHED"
        print(
            f"({idx:3d}/{total}) [Reel {str(reel_record['reel_number'] or '?'):>3s} | {mid} {src}] "
            f"canvases={reel_record['total_canvases']} ({reel_record['median_width']}x{reel_record['median_height']}) "
            f"gz={gz_kb:.0f}KB | {reel_record['start_address_raw']} -> {reel_record['end_address_raw']}",
            flush=True,
        )

    print(f"\nStreet-index manifest pass complete: {total} total ({fetched_count} fetched, {total - fetched_count} cached).", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
