# Chicago Building Permit Street Index — Multi-Pass Sampling Plan

## 1. Collection Discovery (Already Complete Offline)
From UIC IIIF Collection `31` (`/iiif/collection/31`), we already discovered and parsed **all 138 street index reels (`Reel 1` – `Reel 138`, zero missing)** with their cataloged start/end addresses:
- Catalog files: [collection_reels.json](file:///home/timblackburn/projects/uic_permit_index/street_index/index/collection_reels.json) and [collection_reels.csv](file:///home/timblackburn/projects/uic_permit_index/street_index/index/collection_reels.csv)
- Scope: `Reel 1` (`Abbott 9041-51 South` → `Adams Street 4213 West`) through `Reel 138` (`114th Place 141 East` → `138th Street 2701 East`).
- Typical size: ~5,000 index-card slides per reel (~690,000 total slides across 138 reels).

> [!IMPORTANT]
> **Politeness Policy**: Do **not** run street-index downloads concurrently with the ledger Pass-1 scraper. Always keep `concurrency = 1` across the entire `digital.library.uic.edu` host, `6.0–8.0s` delay (`Crawl-delay: 5` + jitter), consistent `User-Agent`, and immediate abort on `HTTP 403` / `cf-mitigated`.

---

## 2. Multi-Pass Indexing Workflow

### Pilot (1 Reel: `1038601`) — Run First After Ledger Pass 1 Finishes
1. Fetch manifest `1038601` (`fetch_reel_manifests.py 1038601`) and check canvas count, dimensions, and compressed manifest size (`manifest.json.gz`).
2. Download a 3-slide layout probe (e.g., slides `10`, `2500`, `4500`) testing `/full/512,` vs. a regional crop (e.g., `pct:0,0,100,45/1500,` or `pct:0,0,100,50/2000,`) to confirm:
   - Exact position of **street name**, **house number**, **direction**, and **permit date/number** on the index cards.
   - Optimal single-request IIIF crop + width that avoids UIC's `/full/` 512px clamp while maximizing OCR/visual legibility.

### Pass 0 — Harvest All 138 Reel Manifests (`fetch_reel_manifests.py`)
- Fetch all 138 manifests via their ARKs from `collection_reels.json`.
- Cache raw manifests gzipped (`street_index/data/<id>/manifest.json.gz`) to keep disk compact (~80–120 MB instead of ~1+ GB).
- Build `street_index/index/reels.json` and compact `street_index/index/canvases_<id>.json`.
- **Budget**: 138 requests @ 7–9s ≈ **~18–20 minutes**.

### Pass 1 — Coarse Anchor Grid Every 500 Slides (`sample_reels.py --step 500`)
- Sample `[first, 500, 1000, 1500, ..., last]` (~11–12 slides per 5,000-slide reel).
- **Budget**: ~138 × 11 ≈ **~1,500 requests** (~3.2 hours at 6–8s delay), fully resumable.
- **Outcome**: Every legible anchor pair brackets a 500-slide window, letting address interpolation land within ~100–250 slides of any target address.

### Pass 2 — Legibility Audit & Nearby-Card Recovery (`plan_pass2.py`)
- After Pass 1 offline inspection, record per sampled slide:
  - `legibility`: `ok` | `partial` (street readable, number illegible, or vice versa) | `illegible` | `non_card` (target/blank/divider)
  - `street_name`, `direction`, `house_number`, `permit_date`, `permit_no`
- `plan_pass2.py` prints collection-wide legibility stats (`% street legible`, `% number legible`, `% both legible`, `% illegible`) and generates a Pass-2 recovery queue (`street_index/index/pass2_queue.json`) picking the next nearby untried offset (`+5`, `-5`, `+15`, `-15`, `+30`, `-30`) around every illegible/partial/non-card anchor.
- Feed `pass2_queue.json` back into `sample_reels.py --queue street_index/index/pass2_queue.json`.

**Filing order of the cards** (checked against every hand-read anchor, 2026-11-23): named streets first, A to Z letter by letter with spaces ignored, then numbered streets. "St." files as *Saint* (Saginaw, St. Lawrence, St. Louis, Sangamon … Seeley) and "So." as *South* (Southport, So. Shore). Two streets file under another word: Martin L. King Drive under *King* (Kimbark, King, Kingston; with its old name South Parkway) and 5th Avenue as *Fifth*, among the F streets. `streetSortKey` in `web/app.js` and `street_sort_key` in `street_index/plan_pass3_neighborhood.py` apply this rule; keep the two the same.

### Pass 3 — Targeted High-Resolution Neighborhood Pass (`plan_pass3_neighborhood.py`)
- Accepts a user-supplied CSV of Far Southwest Side target streets (Beverly / Morgan Park / Washington Heights / Mt. Greenwood — street name, direction, min/max house number).
- Cross-references those streets against `reels.json` (start/end reel bounds) and Pass 1/2 observed anchors to isolate **only** the narrow slide sub-windows containing those streets.
- Generates a high-resolution Pass-3 queue (e.g., `--step 50` or `--step 100`) only inside those matched windows.
