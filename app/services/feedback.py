"""Pure helpers that compare AI values with the human-confirmed values.

No database or LLM here, so it is easy to unit-test. The results feed:
  * expense_corrections table      (evaluation dataset)
  * LangSmith feedback scores      (per-field correctness)
"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.schemas.enums import CORRECTABLE_FIELD_KEYS


def normalize_value(value: Any) -> Optional[str]:
    """Comparable string form: amounts to 2 decimals, dates to ISO, blank -> None."""
    if value is None:
        return None
    if isinstance(value, (Decimal, float, int)) and not isinstance(value, bool):
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = " ".join(str(value).split())
    return text or None


def compute_corrections(expense) -> List[Tuple[str, Optional[str], Optional[str]]]:
    """[(field, ai_value, final_value)] for every correctable field that differs.

    A field the AI left empty and the human filled in counts as a correction
    (the AI missed it); both empty is not a correction.
    """
    out = []
    for field in CORRECTABLE_FIELD_KEYS:
        ai = normalize_value(getattr(expense, f"{field}_ai", None))
        final = normalize_value(getattr(expense, field, None))
        if ai != final:
            out.append((field, ai, final))
    return out


def final_snapshot(expense) -> Dict[str, Any]:
    """JSON-safe snapshot of the final values at confirmation time."""
    def jsonable(v):
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        if isinstance(v, Decimal):
            return float(v)
        return v

    keys = ["vendor", "invoice_number", "expense_date", "category", "item", "goods_or_service",
            "expense_amount", "currency", "subtotal", "tax", "description"]
    return {k: jsonable(getattr(expense, k, None)) for k in keys}
