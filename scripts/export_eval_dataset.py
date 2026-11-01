"""Turn human feedback into evaluation numbers and a dataset (never touches prompts or model weights).

    python -m scripts.export_eval_dataset --stats --since 2026-10-24
    python -m scripts.export_eval_dataset --out eval/feedback.jsonl
    python -m scripts.export_eval_dataset --out eval/feedback.jsonl --langsmith expense-feedback

--stats prints N, the share accepted without ANY correction (X), category accuracy (Y),
95% confidence intervals, per-field accuracy, how many receipts were discarded, and a
ready-to-paste résumé sentence. Receipt images are NOT exported.
"""
import argparse
import json
import os
from collections import Counter
from datetime import datetime

from app.database.models import SessionLocal
from app.database.repository import ExpenseRepository
from app.schemas.enums import CORRECTABLE_FIELD_KEYS
from app.services.evaluation import live_summary, resume_line, sample_size_note


def build_records(since=None):
    with SessionLocal() as session:
        repo = ExpenseRepository(session)
        rows = repo.list_confirmed_for_eval(since)
        status_counts = repo.receipt_status_counts(since)
        records = []
        for e in rows:
            blob = e.ai_extraction or {}
            records.append({
                "expense_id": e.id,
                # what the categorizer saw (AI-extracted values, before any human edit)
                "inputs": blob.get("extraction") or {},
                "ai_outputs": {f: getattr(e, f"{f}_ai", None) for f in CORRECTABLE_FIELD_KEYS},
                "reference_outputs": e.final_result or {},     # human-confirmed truth
                "corrected_by_user": bool(e.corrected_by_user),
                "corrected_fields": e.corrected_fields or [],
                "model": (e.extraction_meta or {}).get("extraction_llm"),
                "confirmed_at": e.confirmed_at.isoformat() if e.confirmed_at else None,
            })
    return records, status_counts


def jsonable(value):
    return json.loads(json.dumps(value, default=str))


def fmt(p):
    return f"{p['pct']:5.1f}%  (95% CI {p['ci_low']:.1f}-{p['ci_high']:.1f}%)  [{p['k']}/{p['n']}]"


def print_stats(records, status_counts=None):
    status_counts = status_counts or {}
    if not records:
        print("No confirmed receipts yet - confirm some real receipts first.")
        return
    s = live_summary(records, discarded=status_counts.get("discarded", 0))
    print(f"N  confirmed receipts : {s['n']}   ({sample_size_note(s['n'])})")
    print(f"X  accepted untouched : {fmt(s['accepted'])}")
    print(f"Y  category accuracy  : {fmt(s['category'])}")
    if s["discarded"]:
        print(f"   incl. {s['discarded']} discarded as failures: {fmt(s['accepted_strict'])}   <- stricter, no survivorship bias")
    print("\nPer-field accuracy (AI value kept):")
    for field, p in s["per_field"].items():
        print(f"  {field:<18} {fmt(p)}")
    pairs = Counter((r["ai_outputs"].get("category"), r["reference_outputs"].get("category"))
                    for r in records if "category" in r["corrected_fields"])
    if pairs:
        print("\nMost common category mistakes (AI -> human):")
        for (ai, human), n in pairs.most_common(5):
            print(f"  {ai} -> {human}: {n}")
    by_model = Counter(); by_model_ok = Counter()
    for r in records:
        name = (r.get("model") or {}).get("model") or "unknown"
        by_model[name] += 1
        by_model_ok[name] += 0 if r["corrected_by_user"] else 1
    if len(by_model) > 1:
        print("\nBy vision model:")
        for name, n in by_model.most_common():
            print(f"  {name:<40} {by_model_ok[name]}/{n} accepted untouched")
    print("\nResume sentence:\n  " + resume_line(s["n"], s["accepted"], s["category"]))


def upload_to_langsmith(records, dataset_name):
    from langsmith import Client

    client = Client()
    dataset = (client.read_dataset(dataset_name=dataset_name) if client.has_dataset(dataset_name=dataset_name)
               else client.create_dataset(dataset_name, description="Human-verified expense extraction/categorization"))
    client.create_examples(
        inputs=[jsonable(r["inputs"]) for r in records],
        outputs=[jsonable(r["reference_outputs"]) for r in records],
        metadata=[{"expense_id": r["expense_id"], "ai_outputs": jsonable(r["ai_outputs"]),
                   "corrected_fields": r["corrected_fields"]} for r in records],
        dataset_id=dataset.id,
    )
    print(f"Uploaded {len(records)} examples to LangSmith dataset '{dataset_name}'")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="only receipts created on/after YYYY-MM-DD (use your go-live date)")
    parser.add_argument("--out", help="write JSON Lines to this file")
    parser.add_argument("--langsmith", metavar="DATASET", help="also upload to this LangSmith dataset")
    parser.add_argument("--stats", action="store_true", help="print accuracy statistics")
    args = parser.parse_args()

    records, status_counts = build_records(datetime.fromisoformat(args.since) if args.since else None)
    if args.stats or not (args.out or args.langsmith):
        print_stats(records, status_counts)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
        print(f"Wrote {len(records)} records to {args.out}")
    if args.langsmith and records:
        upload_to_langsmith(records, args.langsmith)


if __name__ == "__main__":
    main()
