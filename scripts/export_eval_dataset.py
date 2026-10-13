"""Turn human feedback into an evaluation dataset (never touches prompts or model weights).

Examples:
    python -m scripts.export_eval_dataset --stats
    python -m scripts.export_eval_dataset --out eval/feedback.jsonl
    python -m scripts.export_eval_dataset --out eval/feedback.jsonl --langsmith expense-feedback

Each record contains what the AI saw/said, what the human finally confirmed, and which
fields were corrected - ready for categorization-accuracy analysis, prompt iteration
and LangSmith evaluations. Receipt images are NOT exported.
"""
import argparse
import json
import os
from collections import Counter
from datetime import datetime

from app.database.models import SessionLocal
from app.database.repository import ExpenseRepository
from app.schemas.enums import CORRECTABLE_FIELD_KEYS


def build_records(since=None):
    with SessionLocal() as session:
        rows = ExpenseRepository(session).list_confirmed_for_eval(since)
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
    return records


def jsonable(value):
    return json.loads(json.dumps(value, default=str))


def print_stats(records):
    total = len(records)
    if not total:
        print("No confirmed receipts yet.")
        return
    print(f"Confirmed receipts: {total}")
    print(f"Accepted without any change: {sum(not r['corrected_by_user'] for r in records)} "
          f"({sum(not r['corrected_by_user'] for r in records) / total:.0%})")
    counts = Counter(f for r in records for f in r["corrected_fields"])
    print("\nField accuracy (share of receipts where the AI value was kept):")
    for field in CORRECTABLE_FIELD_KEYS:
        print(f"  {field:<18} {1 - counts[field] / total:6.1%}   ({counts[field]} corrected)")
    pairs = Counter((r["ai_outputs"].get("category"), r["reference_outputs"].get("category"))
                    for r in records if "category" in r["corrected_fields"])
    if pairs:
        print("\nMost common category corrections (AI -> human):")
        for (ai, human), n in pairs.most_common(5):
            print(f"  {ai} -> {human}: {n}")


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
    parser.add_argument("--since", help="only receipts confirmed on/after YYYY-MM-DD")
    parser.add_argument("--out", help="write JSON Lines to this file")
    parser.add_argument("--langsmith", metavar="DATASET", help="also upload to this LangSmith dataset")
    parser.add_argument("--stats", action="store_true", help="print accuracy statistics")
    args = parser.parse_args()

    records = build_records(datetime.fromisoformat(args.since) if args.since else None)
    if args.stats or not (args.out or args.langsmith):
        print_stats(records)
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
