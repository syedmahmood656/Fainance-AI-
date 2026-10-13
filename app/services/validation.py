"""The "validate" step of the receipt workflow. Pure Python - no LLM.

Two jobs:
1. sanitize_extraction(): clean obviously wrong AI output (never invent values;
   we only null out things that cannot be right).
2. card_warnings(): compute the "please check" notes shown on the confirmation
   card from the CURRENT values, so a warning disappears once the user fixes it.
"""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.config.settings import settings

MAX_REASONABLE_AGE_DAYS = 5 * 365


def sanitize_extraction(data: Dict[str, Any], today: date) -> Tuple[Dict[str, Any], List[str]]:
    """data: ReceiptExtraction as a dict. Returns (cleaned dict, assumed_fields)."""
    out = dict(data)
    assumed: List[str] = []

    amount = out.get("expense_amount")
    if amount is not None and amount <= 0:
        out["expense_amount"] = None
    for key in ("subtotal", "tax"):
        if out.get(key) is not None and out[key] < 0:
            out[key] = None

    the_date = out.get("expense_date")
    if the_date is not None and (the_date > today + timedelta(days=1)
                                 or the_date < today - timedelta(days=MAX_REASONABLE_AGE_DAYS)):
        out["expense_date"] = None  # an implausible date is more likely a misread than a fact

    if not out.get("currency"):
        out["currency"] = settings.DEFAULT_CURRENCY
        assumed.append("currency")
    return out, assumed


def card_warnings(expense, today: date, meta: Optional[Dict[str, Any]] = None) -> List[str]:
    """Warnings for the confirmation card. `expense` has the FINAL attribute values."""
    meta = meta or {}
    warnings: List[str] = []
    if meta.get("extraction_failed"):
        warnings.append("I couldn't read this receipt automatically. Tap Correct to fill in the details.")
        if meta.get("failure_reason"):
            warnings.append("Reason: " + str(meta["failure_reason"])[:160])

    missing = []
    if expense.expense_amount in (None, 0):
        missing.append("amount")
    if expense.expense_date is None:
        missing.append("date")
    if not expense.category:
        missing.append("category")
    if missing and not meta.get("extraction_failed"):
        warnings.append("Missing " + ", ".join(missing) + " — tap Correct to add it before confirming.")
    elif missing:
        warnings.append("Required before confirming: " + ", ".join(missing) + ".")

    if meta.get("categorization_failed") and not expense.category:
        warnings.append("Auto-categorization was unavailable — please choose a category.")
    if "currency" in (meta.get("assumed_fields") or []):
        warnings.append(f"Currency not shown on the receipt; assuming {expense.currency}.")

    amount, subtotal, tax = expense.expense_amount, expense.subtotal, expense.tax
    if amount and subtotal is not None and tax is not None:
        if abs(float(subtotal) + float(tax) - float(amount)) > max(1.0, float(amount) * 0.02):
            warnings.append("Subtotal + tax does not match the total — please double-check the amount.")
    return warnings
