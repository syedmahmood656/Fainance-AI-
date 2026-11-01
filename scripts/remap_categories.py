"""Re-label OLD category names in existing rows after you change the category list.

    python -m scripts.remap_categories            # dry run: shows what would change
    python -m scripts.remap_categories --apply    # actually update

Only the FINAL `category` column is changed (so reports/Excel/charts are consistent). The AI's original
values (`category_ai`, `ai_extraction`) are never touched - for accuracy numbers, measure only receipts
created after the change:  python -m scripts.export_eval_dataset --stats --since <date>.
Edit LEGACY_MAP to taste before applying.
"""
import argparse
from collections import Counter

from sqlalchemy import func

from app.schemas.enums import ExpenseCategory as C
from app.schemas.enums import normalize_category

# old name (lower-case) -> new category. Anything not listed is reported as "unmapped" and left alone.
LEGACY_MAP = {
    "travel": C.TRAVELLING_EXPENSES, "accommodation": C.TRAVELLING_EXPENSES,
    "food & meals": C.GUEST_HOSPITALITY, "food and meals": C.GUEST_HOSPITALITY,
    "transportation": C.CONVEYANCE,
    "office supplies": C.PRINTING_STATIONARY,
    "utilities": C.INTERNET,
    "communication": C.MOBILE_RECHARGE,
    "software & subscriptions": C.MISCELLANEOUS, "equipment": C.MISCELLANEOUS, "marketing": C.MISCELLANEOUS,
    "professional services": C.MISCELLANEOUS,
}


def plan(counts):
    """counts: {category_text: n} -> ([(old, new_value, n)], [(old, n) unmapped])"""
    changes, unmapped = [], []
    for old, n in counts.items():
        if old is None or normalize_category(old) is not None and normalize_category(old).value == old:
            continue                                              # already a valid current category
        target = LEGACY_MAP.get(str(old).strip().lower()) or normalize_category(old)
        (changes if target else unmapped).append((old, target.value, n) if target else (old, n))
    return changes, unmapped


def main():
    from app.database.models import Expense, SessionLocal

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with SessionLocal() as session:
        counts = Counter(dict(session.query(Expense.category, func.count(Expense.id)).group_by(Expense.category).all()))
        changes, unmapped = plan(counts)
        for old, new, n in changes:
            print(f"{'UPDATE' if args.apply else 'would update'}: {old!r} -> {new!r}  ({n} rows)")
            if args.apply:
                session.query(Expense).filter(Expense.category == old).update({Expense.category: new})
        for old, n in unmapped:
            print(f"unmapped (left as is): {old!r} ({n} rows) - add it to LEGACY_MAP")
        if args.apply:
            session.commit()
        elif changes:
            print("\nDry run only. Re-run with --apply to change the rows.")
        if not changes and not unmapped:
            print("Nothing to do: every row already uses a current category.")


if __name__ == "__main__":
    main()
