"""Measure accuracy on receipts YOU have labelled (ground truth), without waiting for live usage.

1. Put receipt images in a folder, e.g. eval/receipts/
2. Create the label sheet:   python -m scripts.eval_receipts --images eval/receipts --init-truth eval/truth.csv
3. Open eval/truth.csv and fill in what is REALLY on each paper receipt
   (leave a cell blank to skip that field; dates like 07/10/2026 are read day-first).
4. Run the evaluation:       python -m scripts.eval_receipts --images eval/receipts --truth eval/truth.csv

It runs the SAME pipeline as the bot (resize -> vision LLM -> validation -> categorization),
scores each field against your labels, and prints accuracy with 95% confidence intervals plus
a résumé sentence. Uses your real API keys, so --delay keeps free tiers from rate-limiting.
"""
import argparse
import asyncio
import csv
import json
import os
import sys
import time
from datetime import date

from app.services.evaluation import SCORED_FIELDS, resume_line, sample_size_note, score_receipt, summarize
from app.utils.parsing import clean_text, parse_amount, parse_date_flexible

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp")
TRUTH_COLUMNS = ["file", "vendor", "expense_date", "expense_amount", "category", "invoice_number"]


def init_truth(images_dir: str, path: str) -> None:
    files = sorted(f for f in os.listdir(images_dir) if f.lower().endswith(IMAGE_EXT))
    if os.path.exists(path):
        sys.exit(f"{path} already exists - not overwriting your labels.")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(TRUTH_COLUMNS)
        for name in files:
            writer.writerow([name, "", "", "", "", ""])
    print(f"Wrote {path} with {len(files)} rows. Fill it in from the paper receipts, then run with --truth.")


def load_truth(path: str):
    from app.schemas.enums import normalize_category

    rows = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for raw in csv.DictReader(fh):
            cat = clean_text(raw.get("category"))
            norm = normalize_category(cat) if cat else None
            if cat and norm is None:
                sys.exit(f"{raw.get('file')}: category {cat!r} is not one of your categories.")
            rows.append({
                "file": (raw.get("file") or "").strip(),
                "vendor": clean_text(raw.get("vendor")),
                "expense_date": parse_date_flexible(raw.get("expense_date")),
                "expense_amount": parse_amount(raw.get("expense_amount")),
                "category": norm.value if norm else None,
                "invoice_number": clean_text(raw.get("invoice_number"), 100),
            })
    return [r for r in rows if r["file"] and any(r[f] not in (None, "") for f in SCORED_FIELDS)]


async def predict(image_bytes: bytes) -> dict:
    """Exactly what the bot does for a photo (no database involved)."""
    from app.ai.llm_provider import LLMUnavailableError
    from app.ai.receipt_extractor import ExpenseCategorizer, ReceiptExtractor
    from app.schemas.expense import ReceiptExtraction
    from app.services.validation import sanitize_extraction

    out = {"vendor": None, "expense_date": None, "expense_amount": None, "category": None,
           "invoice_number": None, "error": None, "model": None}
    try:
        extraction, info = await ReceiptExtractor().extract(image_bytes)
        cleaned, _assumed = sanitize_extraction(extraction.model_dump(), date.today())
        extraction = ReceiptExtraction(**cleaned)
        out.update(vendor=extraction.vendor, expense_date=extraction.expense_date,
                   expense_amount=extraction.expense_amount, invoice_number=extraction.invoice_number,
                   model=info.model)
        if not extraction.is_empty():
            result, _ = await ExpenseCategorizer().categorize(extraction)
            out["category"] = result.category.value
    except LLMUnavailableError as exc:
        out["error"] = str(exc)
    return out


async def run(images_dir: str, truth_path: str, delay: float, report_path: str):
    truth_rows = load_truth(truth_path)
    if not truth_rows:
        sys.exit("No labelled rows found in the truth file.")
    results, scores = [], []
    for i, truth in enumerate(truth_rows, start=1):
        path = os.path.join(images_dir, truth["file"])
        if not os.path.isfile(path):
            print(f"[{i}/{len(truth_rows)}] {truth['file']}: image not found - skipped")
            continue
        pred = await predict(open(path, "rb").read())
        score = score_receipt(truth, pred)
        scores.append(score)
        wrong = [f for f, ok in score.items() if ok is False]
        status = "ERROR " + pred["error"][:60] if pred["error"] else ("OK" if not wrong else "WRONG: " + ", ".join(wrong))
        print(f"[{i}/{len(truth_rows)}] {truth['file'][:28]:<28} {status}")
        for f in wrong:
            print(f"      {f:<15} truth={truth[f]!s:<28} AI={pred[f]!s}")
        results.append({"file": truth["file"], "truth": json.loads(json.dumps(truth, default=str)),
                        "predicted": json.loads(json.dumps(pred, default=str)), "score": score})
        if i < len(truth_rows):
            time.sleep(delay)

    summary = summarize(scores)
    errors = sum(1 for r in results if r["predicted"]["error"])
    print("\n" + "=" * 70)
    print(f"Receipts scored: {summary['n']}   ({sample_size_note(summary['n'])})")
    if errors:
        print(f"WARNING: {errors} receipt(s) failed because no AI model answered - counted as wrong. "
              "Re-run when the models are available (python -m scripts.check_llm).")
    full = summary["fully_correct"]
    print(f"Fully correct (all labelled fields): {full['pct']}%  (95% CI {full['ci_low']}-{full['ci_high']}%)  [{full['k']}/{full['n']}]")
    for field, p in summary["per_field"].items():
        print(f"  {field:<16} {p['pct']:5.1f}%  (95% CI {p['ci_low']:.1f}-{p['ci_high']:.1f}%)  [{p['k']}/{p['n']}]")
    if "category" in summary["per_field"]:
        print("\nResume sentence:\n  " + resume_line(summary["n"], full, summary["per_field"]["category"],
                                                     label="labelled receipts"))
    os.makedirs(os.path.dirname(report_path) or ".", exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "results": results}, fh, indent=2, ensure_ascii=False)
    print(f"\nFull report: {report_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--images", required=True, help="folder with receipt images")
    ap.add_argument("--init-truth", metavar="CSV", help="create an empty label sheet for the images, then exit")
    ap.add_argument("--truth", metavar="CSV", help="filled-in label sheet")
    ap.add_argument("--delay", type=float, default=4.0, help="seconds between receipts (free-tier rate limits)")
    ap.add_argument("--report", default="eval/report.json")
    args = ap.parse_args()
    if args.init_truth:
        return init_truth(args.images, args.init_truth)
    if not args.truth:
        ap.error("--truth is required (or use --init-truth to create it)")
    asyncio.run(run(args.images, args.truth, args.delay, args.report))


if __name__ == "__main__":
    main()
