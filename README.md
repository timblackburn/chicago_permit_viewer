# Chicago Building Permit Viewer (1872–1954)

A 3rd-party web navigator and built-in IIIF microfilm viewer for Chicago's historical building permit collection (1872–1954), designed to help homeowners, researchers, and historians go from a street address to the original permit ledger entry without getting lost in 228+ microfilm reels.

## Features

- **2-Step Guided Lookup**:
  1. **Address Search → 3×5 Street Index Cards**: Enter any Chicago street address (with precomputed autocomplete across 2,531 official Chicago streets) to jump directly to the interpolated microfilm slide in the 138 Street Index reels (~675,788 cards), complete with live slide thumbnails.
  2. **Automatic Corner-Lot Detection**: Uses a precomputed 52,029-block Chicago street network (`web/data/chicago_cross_streets.json`) to flag corner addresses and automatically show the intersecting cross-street reel as well, since corner-lot permits were often filed under either street.
  3. **Card → Ledger Handoff**: Inside the microfilm viewer, jump directly from an index card to the **Permit Ledger** either **By Book & Page** or **By Permit Date** (auto-interpolating to the target slide).
- **Save & Review Queue (`Yes` / `Maybe` / `No` + Notes)**:
  - Tag candidate index cards as `Yes`, `Maybe`, or `No` and attach notes (stored locally in `localStorage`), then step through your `Yes` and `Maybe` cards one by one while looking each up in the ledgers.
- **Permit Ledgers & Community Area Timeline**:
  - Filter all 90 ledger books (138 contiguous sections) by any of Chicago's **77 Community Areas**, decade, or custom year range, with an interactive visual timeline and floating hover tooltips.
- **Built-in IIIF Microfilm Darkroom & Export**:
  - Powered by [OpenSeadragon](https://openseadragon.github.io/) against UIC's CORS-enabled IIIF Image API 3.0.
  - Includes one-click legibility presets (**Faded Ink**, **Dark Scan**, **Negative**, **Crisp B&W**), **Sharpen**, **Gamma**, **Contrast**, **Brightness**, **Invert**, and **Rotate**.
  - **Save Slide** & **Crop / Highlight**: Download full-resolution PNGs of entire slides, crop a single 6-per-page square (post-1912) or wide row strip (pre-1912), or highlight a pre-1912 ledger row while keeping column headers intact.

## Running Locally

Because the app fetches local JSON catalogs (`web/data/*.json`, `index/canvases_*.json`, `street_index/index/canvases_*.json`), serve the repository root over HTTP:

```bash
python3 -m http.server 8765 --bind 127.0.0.1
```

Then open **http://127.0.0.1:8765/web/** in your browser.

## Repository Layout

- `web/` — Static web application (`index.html`, `styles.css`, `app.js`, and compiled JSON bundles in `web/data/`).
- `index/` — Ledger reel manifests, 90-book segment index (`ledger_segments.json`), and per-book IIIF canvas lists (`canvases_*.json`).
- `street_index/index/` — Street Index reel catalog (`reels.json`) and per-reel IIIF canvas lists (`canvases_*.json`).
- `build_web_data.py`, `build_ledger_segments.py`, `build_chicago_streets.py` — Offline scripts that compile the web bundles from the indexed catalogs.

## Credits & Acknowledgments

- **Created by Tim Blackburn**, Historian, Ridge Historical Society.
- **Thank you to the University of Illinois at Chicago Library** ([digital.library.uic.edu](https://digital.library.uic.edu/)) for digitizing the Chicago Building Permit Collection and making the microfilm openly available via IIIF.

## Privacy

This is an open-source static site hosted on GitHub Pages. Any user data — including tagged index cards (`Yes` / `Maybe` / `No`) and notes — is stored entirely in your local browser storage (`localStorage`) and is never collected, tracked, or transmitted to any server.
