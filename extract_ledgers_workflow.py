import csv
import json
from datetime import date
from pathlib import Path

BASE_DIR = Path("/home/timblackburn/projects/uic_permit_index")
INDEX_DIR = BASE_DIR / "index"

ROLL_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "slide_index": {"type": "integer"},
            "slide_type": {
                "type": "string",
                "enum": ["permits", "cover", "blank", "index", "target", "illegible"],
            },
            "ledger_page_left": {"type": "string"},
            "ledger_page_right": {"type": "string"},
            "observed_date_start": {"type": "string"},
            "observed_date_end": {"type": "string"},
            "permit_no_first": {"type": "string"},
            "observed_region": {"type": "string"},
            "sample_addresses": {"type": "string"},
            "notes": {"type": "string"},
        },
        "required": [
            "slide_index",
            "slide_type",
            "ledger_page_left",
            "ledger_page_right",
            "observed_date_start",
            "observed_date_end",
            "permit_no_first",
            "observed_region",
            "sample_addresses",
            "notes",
        ],
    },
}


def build_prompt(roll: dict, pending_rows: list[dict]) -> str:
    lines = [
        f"Extract date, page/folio number, permit number, and empirical region from {len(pending_rows)} local top-25% Chicago Building Permit microfilm crops.",
        f"Roll: manifest_id={roll['manifest_id']} | era={roll['era']} | UIC title (REFERENCE ONLY — DO NOT TRUST REGION BLINDLY): {roll['title']}",
        "",
        "Instructions:",
        "1. Call read_file on EVERY local JPEG path listed below (you may batch 5-6 read_file calls per turn to finish quickly).",
        "2. For each slide_index, inspect the actual image carefully:",
        "   - slide_type: 'permits' if permit entries are visible; 'blank' if blank ruled pages; 'cover'/'target' if title card or cover; 'index' if alphabetical index; 'illegible' if too dark/blurred to read dates.",
        "   - ledger_page_left / ledger_page_right: printed page/folio numbers in top outer corners (for wide double-spread ledgers where both sides share one folio number, put the same number in both; if one side is blank inside cover, leave that side '').",
        "   - observed_date_start / observed_date_end: ISO 'YYYY-MM-DD' for the earliest and latest visible dates on the crop ('' if non-permit/illegible). Watch out for year headers above the month/day column; if the day is faint, estimate 'YYYY-MM-01' and note 'day est' in notes.",
        "   - permit_no_first: first visible permit number (include any prefix like 'So 14503' or 'No 10616' or 'A 53045' if present).",
        "   - observed_region: Classify EMPIRICALLY from the ACTUAL street addresses (and S-T-R township or permit prefix) visible on the slide — NOT from the UIC title! Use concise labels: 'North', 'South', 'West', 'Northwest', 'Southwest', 'Central', 'Hyde Park', 'Lake View', 'Lake', 'Citywide/Mixed', or '' for non-permits. Note: Chicago grid sanity check — N/S streets with high S numbers (e.g. 4000-13000 S, 50th-138th St, T37/T38) are South; high N numbers (e.g. Sheridan, Belmont, Irving Park, Clark N, T40/T41) are North; W streets / Ave 1600-7200 W (Lexington, Fillmore, Laramie, Cicero, Diversey, Avers) are West/NW; Loop/downtown is Central; if a single slide mixes far North + far South + West addresses, mark 'Citywide/Mixed'.",
        "   - sample_addresses: 2-3 short real street addresses read directly from the image (e.g. '8140 S Kenwood; 5239 N Lacrosse').",
        "   - notes: brief note on anything unusual (year rollover, blank left side, faint stamp, region switch, etc.).",
        "3. Return ALL requested slides in a JSON array via the finish tool.",
        "",
        "Slides to inspect:",
    ]
    for r in pending_rows:
        abs_path = str((BASE_DIR / r["local_path"]).resolve())
        lines.append(f"  - slide_index={r['slide_index']} | path={abs_path}")
    return "\n".join(lines)


def save_roll_annotations(mid: str, results: list[dict]) -> tuple[int, list[tuple[int, int, str, str]]]:
    csv_path = INDEX_DIR / f"slides_{mid}.csv"
    rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")))
    by_slide = {int(r["slide_index"]): r for r in rows}

    for item in results:
        s = int(item["slide_index"])
        if s not in by_slide:
            continue
        r = by_slide[s]
        r["slide_type"] = (item.get("slide_type") or "").strip()
        r["ledger_page_left"] = str(item.get("ledger_page_left") or "").strip()
        r["ledger_page_right"] = str(item.get("ledger_page_right") or "").strip()
        r["observed_date_start"] = (item.get("observed_date_start") or "").strip()
        r["observed_date_end"] = (item.get("observed_date_end") or "").strip()
        r["permit_no_first"] = str(item.get("permit_no_first") or "").strip()
        reg = (item.get("observed_region") or "").strip()
        addrs = (item.get("sample_addresses") or "").strip()
        notes = (item.get("notes") or "").strip()
        parts = [p for p in [reg, f"[{addrs}]" if addrs else "", notes] if p]
        r["region_note"] = " ".join(parts)

    ordered = [by_slide[k] for k in sorted(by_slide.keys())]

    # Compute order_status inline (matching check_order.py logic, 7-day backtrack tolerance)
    permit_rows = []
    breaks: list[tuple[int, int, str, str]] = []
    for r in ordered:
        stype = (r.get("slide_type") or "").strip().lower()
        d_raw = (r.get("observed_date_start") or "").strip()
        d_val = None
        if d_raw:
            try:
                d_val = date.fromisoformat(d_raw)
            except ValueError:
                d_val = None
        if stype in ("cover", "blank", "index", "target", "non_permit"):
            r["order_status"] = "non_permit"
        elif stype == "illegible":
            r["order_status"] = "illegible"
        elif d_val is None:
            r["order_status"] = "pending"
        else:
            r["order_status"] = "ok"
            permit_rows.append((r, d_val))

    for i in range(1, len(permit_rows)):
        prev_r, prev_d = permit_rows[i - 1]
        cur_r, cur_d = permit_rows[i]
        if (cur_d - prev_d).days < -7:
            cur_r["order_status"] = "out_of_order"
            breaks.append((int(prev_r["slide_index"]), int(cur_r["slide_index"]), prev_d.isoformat(), cur_d.isoformat()))

    fieldnames = list(ordered[0].keys())
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(ordered)
    (INDEX_DIR / f"slides_{mid}.json").write_text(json.dumps(ordered, indent=2, ensure_ascii=False), encoding="utf-8")
    return len(results), breaks


ERAS = list(args.get("eras", [])) if isinstance(args, dict) else []
MODEL_TIER = (args.get("model_tier") or "inherit") if isinstance(args, dict) else "inherit"

phase("Discover Pending Rolls")
log(f"Eras filter: {ERAS or 'ALL'} | model tier: {MODEL_TIER}")
rolls = json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8"))
rolls_by_mid = {str(r["manifest_id"]): r for r in rolls}

work_list = []
for roll in rolls:
    mid = str(roll["manifest_id"])
    csv_path = INDEX_DIR / f"slides_{mid}.csv"
    if not csv_path.exists():
        continue
    rows = list(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")))
    pending = [r for r in rows if (r.get("slide_type") or "").strip() == ""]
    if pending and (not ERAS or roll.get("era") in ERAS):
        work_list.append((roll, pending))

log(f"Found {len(work_list)} rolls with pending slides ({sum(len(p) for _, p in work_list)} total images to inspect).")

wave_size = int(args.get("wave_size", 6)) if isinstance(args, dict) else 6
max_waves = int(args.get("max_waves", 0)) if isinstance(args, dict) else 0

total_waves = (len(work_list) + wave_size - 1) // wave_size
if max_waves > 0:
    total_waves = min(total_waves, max_waves)

for w_idx in range(total_waves):
    batch = work_list[w_idx * wave_size : (w_idx + 1) * wave_size]
    phase(f"Wave {w_idx + 1}/{total_waves} ({len(batch)} rolls)")
    log(f"Starting wave {w_idx + 1}/{total_waves}: {[r['manifest_id'] for r, _ in batch]}")

    async def process_roll(item):
        # Run one roll and SAVE IMMEDIATELY so a quota failure on one roll never loses finished rolls.
        roll, pending = item
        mid = str(roll["manifest_id"])
        era = roll.get("era", "")
        try:
            res = await agent(
                build_prompt(roll, pending),
                schema=ROLL_SCHEMA,
                role=f"Extract {mid} ({era}, {len(pending)} imgs)",
                type_name="research",
                model=MODEL_TIER,
            )
        except Exception as exc:
            log(f"  FAILED [{mid}] (will be retried on resume): {str(exc)[:160]}")
            return False
        cnt, breaks = save_roll_annotations(mid, res)
        rolls_disk = json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8"))
        for r in rolls_disk:
            if str(r["manifest_id"]) == mid:
                r["multi_region_flagged"] = bool(breaks)
        (INDEX_DIR / "rolls.json").write_text(json.dumps(rolls_disk, indent=2, ensure_ascii=False), encoding="utf-8")
        flag_str = f" [BREAKS: {len(breaks)}]" if breaks else ""
        log(f"  Saved [{mid}] ({cnt} slides){flag_str} | {roll['title']}")
        return True

    wave_ok = await parallel(batch, process_roll)
    if not any(wave_ok):
        log("Entire wave failed (likely quota). Stopping early; rerun the workflow to resume.")
        break

phase("Audit & Pass-2 Bisection Summary")
all_breaks = []
edge_issues = []
illegible_list = []
pending_left = 0

for roll in json.loads((INDEX_DIR / "rolls.json").read_text(encoding="utf-8")):
    mid = str(roll["manifest_id"])
    csv_path = INDEX_DIR / f"slides_{mid}.csv"
    if not csv_path.exists():
        continue
    rows = sorted(csv.DictReader(csv_path.open("r", encoding="utf-8", newline="")), key=lambda x: int(x["slide_index"]))
    permit_rows = []
    for r in rows:
        st = (r.get("order_status") or "").strip()
        if st == "pending":
            pending_left += 1
        elif st == "illegible":
            illegible_list.append({"manifest_id": mid, "slide_index": int(r["slide_index"]), "title": roll["title"]})
        elif st in ("ok", "out_of_order"):
            permit_rows.append(r)

    for i in range(1, len(permit_rows)):
        if permit_rows[i]["order_status"] == "out_of_order":
            s_a = int(permit_rows[i - 1]["slide_index"])
            s_b = int(permit_rows[i]["slide_index"])
            all_breaks.append(
                {
                    "manifest_id": mid,
                    "title": roll["title"],
                    "slide_a": s_a,
                    "date_a": permit_rows[i - 1]["observed_date_start"],
                    "slide_b": s_b,
                    "date_b": permit_rows[i]["observed_date_start"],
                    "midpoint": (s_a + s_b) // 2 if (s_b - s_a) > 1 else None,
                }
            )

    # Check if first two or last two sampled slides are BOTH non-permit (signals unwalked blank edge)
    if len(rows) >= 2 and rows[0]["order_status"] == "non_permit" and rows[1]["order_status"] == "non_permit":
        edge_issues.append({"manifest_id": mid, "edge": "start", "slides": [int(rows[0]["slide_index"]), int(rows[1]["slide_index"])], "title": roll["title"]})
    if len(rows) >= 2 and rows[-1]["order_status"] == "non_permit" and rows[-2]["order_status"] == "non_permit":
        edge_issues.append({"manifest_id": mid, "edge": "end", "slides": [int(rows[-2]["slide_index"]), int(rows[-1]["slide_index"])], "title": roll["title"]})

report = {
    "rolls_total": len(rolls),
    "pending_slides_remaining": pending_left,
    "out_of_order_breaks_count": len(all_breaks),
    "out_of_order_breaks": all_breaks,
    "edge_issues_count": len(edge_issues),
    "edge_issues": edge_issues,
    "illegible_anchors_count": len(illegible_list),
    "illegible_anchors": illegible_list,
}
(INDEX_DIR / "pass1_audit_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

print(
    f"AUDIT COMPLETE | pending_left={pending_left} | out_of_order_breaks={len(all_breaks)} | "
    f"edge_issues={len(edge_issues)} | illegible_anchors={len(illegible_list)}"
)
for b in all_breaks:
    print(f"  BREAK [{b['manifest_id']}] p{b['slide_a']} ({b['date_a']}) -> p{b['slide_b']} ({b['date_b']}) mid={b['midpoint']} | {b['title']}")
