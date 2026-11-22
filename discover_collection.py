#!/usr/bin/env python3
"""Politely paginate UIC IIIF Collection 31 (Chicago Building Permit Collection) and catalog all ledger books."""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data" / "collection"
INDEX_DIR = BASE_DIR / "index"

USER_AGENT = "ChicagoBuildingPermitsIndexer/1.0 (historical research; honors robots.txt Crawl-delay)"
START_URL = "https://digital.library.uic.edu/iiif/collection/31"


def fetch_json(url: str, max_retries: int = 3) -> dict:
    backoff = 30.0
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                cf_mit = resp.headers.get("cf-mitigated", "")
                if cf_mit:
                    raise RuntimeError(f"CIRCUIT BREAKER: Cloudflare challenge ({cf_mit}) on {url}; aborting.")
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            cf_mit = exc.headers.get("cf-mitigated", "") if exc.headers else ""
            if exc.code == 403 or cf_mit:
                raise RuntimeError(
                    f"CIRCUIT BREAKER: HTTP {exc.code} (cf-mitigated={cf_mit}) on {url}; aborting."
                ) from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt < max_retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait_s = float(retry_after) if (retry_after and retry_after.isdigit()) else backoff
                print(f"  HTTP {exc.code}; backing off {wait_s:.0f}s (attempt {attempt}/{max_retries})...", file=sys.stderr)
                time.sleep(wait_s)
                backoff *= 2
                continue
            raise
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")


def classify_item(title: str) -> str:
    low = title.lower()
    if "street index" in low:
        return "street_index"
    if "building permit" in low:
        return "ledger_book"
    return "other"


def extract_ark(item_id: str) -> str:
    m = re.search(r"(ark:/\d+/[A-Za-z0-9]+)", item_id)
    return m.group(1) if m else ""


def clean_title(raw_label: str) -> str:
    return re.sub(r"^Collection:\s*", "", raw_label.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description="Paginate UIC IIIF Collection 31 and catalog all ledger books.")
    parser.add_argument("--min-delay", type=float, default=6.0, help="Min delay between HTTP requests (default: 6.0s)")
    parser.add_argument("--max-delay", type=float, default=8.0, help="Max delay between HTTP requests (default: 8.0s)")
    parser.add_argument("--force", action="store_true", help="Re-fetch cached collection pages")
    args = parser.parse_args()

    if args.min_delay < 5.0:
        print("Error: --min-delay must be >= 5.0s per UIC robots.txt Crawl-delay: 5.", file=sys.stderr)
        return 2

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)

    url: str | None = START_URL
    page_num = 1
    fetched_count = 0
    seen_urls: set[str] = set()
    all_records: list[dict] = []
    seen_arks: set[str] = set()

    while url and url not in seen_urls:
        seen_urls.add(url)
        page_path = DATA_DIR / f"page_{page_num:03d}.json"
        if page_path.exists() and not args.force:
            data = json.loads(page_path.read_text(encoding="utf-8"))
            print(f"[CACHED] Page {page_num}: {len(data.get('items', []))} items -> {page_path.name}")
        else:
            if fetched_count > 0:
                pause = random.uniform(args.min_delay, max(args.min_delay, args.max_delay))
                print(f"  Sleeping {pause:.1f}s...")
                time.sleep(pause)
            print(f"[FETCH] Page {page_num}: {url}")
            data = fetch_json(url)
            fetched_count += 1
            tmp = page_path.with_suffix(".json.part")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(page_path)
            print(f"  Saved {len(data.get('items', []))} items -> {page_path.name}")

        for idx, item in enumerate(data.get("items", []), start=1):
            item_id = item.get("id", "")
            raw_label = (item.get("label", {}).get("en") or [""])[0]
            title = clean_title(raw_label)
            ark_id = extract_ark(item_id)
            if ark_id and ark_id in seen_arks:
                continue
            if ark_id:
                seen_arks.add(ark_id)
            kind = classify_item(title)
            all_records.append(
                {
                    "page_num": page_num,
                    "page_pos": idx,
                    "kind": kind,
                    "ark_id": ark_id,
                    "ark_permalink": f"https://n2t.net/{ark_id}" if ark_id else "",
                    "object_collection_url": item_id,
                    "title": title,
                }
            )

        next_url = data.get("next")
        if next_url and isinstance(next_url, dict):
            next_url = next_url.get("id")
        url = next_url if (next_url and data.get("items")) else None
        page_num += 1

    ledgers = [r for r in all_records if r["kind"] == "ledger_book"]
    street_indices = [r for r in all_records if r["kind"] == "street_index"]
    others = [r for r in all_records if r["kind"] == "other"]

    (INDEX_DIR / "collection_31_all.json").write_text(
        json.dumps(
            {
                "collection_id": "31",
                "collection_url": START_URL,
                "harvested_at": datetime.now(timezone.utc).isoformat(),
                "total_items": len(all_records),
                "ledger_book_count": len(ledgers),
                "street_index_count": len(street_indices),
                "other_count": len(others),
                "items": all_records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    (INDEX_DIR / "collection_31_ledgers.json").write_text(
        json.dumps(ledgers, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    csv_path = INDEX_DIR / "collection_31_ledgers.csv"
    fields = ["page_num", "page_pos", "kind", "ark_id", "ark_permalink", "object_collection_url", "title"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(ledgers)

    print(
        f"\nDiscovery complete: {len(all_records)} total items across {page_num - 1} pages | "
        f"{len(ledgers)} ledger books | {len(street_indices)} street index reels | {len(others)} other"
    )
    if others:
        print("Other items (verify classification):")
        for o in others:
            print(f"  - {o['ark_id']} | {o['title']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
