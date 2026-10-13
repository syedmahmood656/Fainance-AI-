"""Forgiving parsers for amounts, dates, currencies and human corrections.

Pure Python (no pydantic) so they are trivial to unit-test. LLMs and humans both
write things like "₹2,450.00", "Rs 2450", "08/10/2026" or "8 Oct 26" - we accept
all of these and return clean values (or None when unsure; we never guess).
"""
import re
from datetime import date, datetime
from typing import Any, Optional

from app.schemas.enums import (
    ExpenseCategory,
    GoodsOrService,
    normalize_category,
    normalize_goods_or_service,
)

_NULL_WORDS = {"", "null", "none", "n/a", "na", "unknown", "not found", "not available", "-", "—"}


def clean_text(value: Any, max_len: int = 255) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    if text.lower() in _NULL_WORDS:
        return None
    return text[:max_len]


def parse_amount(value: Any) -> Optional[float]:
    """'₹2,450.00' -> 2450.0 ; '1.234,56' -> 1234.56 ; 'abc' -> None"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[^\d.,\-]", "", str(value))
    if not re.search(r"\d", text):
        return None
    negative = text.startswith("-")
    text = text.replace("-", "")
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):      # 1.234,56 (European)
            text = text.replace(".", "").replace(",", ".")
        else:                                       # 1,234.56
            text = text.replace(",", "")
    elif "," in text:
        tail = text.rsplit(",", 1)[1]
        if len(tail) == 2 and text.count(",") == 1:  # 12,50 -> decimal comma
            text = text.replace(",", ".")
        else:                                        # 2,450 / 1,23,456 -> thousands
            text = text.replace(",", "")
    try:
        number = float(text)
    except ValueError:
        return None
    return -number if negative else number


_CURRENCY_MAP = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR",
    "£": "GBP", "gbp": "GBP",
}


_KNOWN_CODES = {
    "INR", "USD", "EUR", "GBP", "AED", "SAR", "QAR", "KWD", "OMR", "BHD", "SGD", "AUD", "CAD",
    "JPY", "CNY", "CHF", "MYR", "THB", "LKR", "NPR", "BDT", "PKR", "ZAR", "NZD", "HKD",
}


def normalize_currency(value: Any) -> Optional[str]:
    text = clean_text(value, 10)
    if not text:
        return None
    mapped = _CURRENCY_MAP.get(text.lower())
    if mapped:
        return mapped
    code = text.upper()
    return code if code in _KNOWN_CODES else None  # unknown code -> None, never an invented currency


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def _year(y: int) -> int:
    return y + 2000 if y < 100 else y


def parse_date_flexible(value: Any) -> Optional[date]:
    """Accepts ISO and the day-first formats common on Indian receipts."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value, 40)
    if not text:
        return None
    try:
        m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", text)   # day first
        if m:
            return date(_year(int(m[3])), int(m[2]), int(m[1]))
        m = re.match(r"^(\d{1,2})[\s\-]+([A-Za-z]{3,9})[a-z]*[\s\-,]+(\d{2,4})$", text)  # 8 Oct 2026
        if m and m[2][:3].lower() in _MONTHS:
            return date(_year(int(m[3])), _MONTHS[m[2][:3].lower()], int(m[1]))
        m = re.match(r"^([A-Za-z]{3,9})[a-z]*\.?[\s\-]+(\d{1,2})(?:st|nd|rd|th)?,?[\s\-]+(\d{2,4})$", text)  # Oct 8, 2026
        if m and m[1][:3].lower() in _MONTHS:
            return date(_year(int(m[3])), _MONTHS[m[1][:3].lower()], int(m[2]))
    except ValueError:
        return None
    return None


def parse_correction(field: str, text: str):
    """Validate a value typed by the user for one correctable field.

    Returns the cleaned value or raises ValueError with a message safe to show.
    """
    text = (text or "").strip()
    if field in ("vendor", "item"):
        value = clean_text(text, 120)
        if not value:
            raise ValueError("Please send a non-empty text value.")
        return value
    if field == "expense_amount":
        amount = parse_amount(text)
        if amount is None or amount <= 0 or amount > 1_000_000_000:
            raise ValueError("Please send the amount as a positive number, e.g. 2450 or 2,450.50")
        return round(amount, 2)
    if field == "expense_date":
        parsed = parse_date_flexible(text)
        if parsed is None:
            raise ValueError("I couldn't read that date. Try 08/10/2026 or 2026-10-08 (day first).")
        return parsed
    if field == "category":
        cat = normalize_category(text)
        if cat is None:
            raise ValueError("Unknown category. Pick one of the buttons instead.")
        return cat.value
    if field == "goods_or_service":
        gos = normalize_goods_or_service(text)
        if gos is None:
            raise ValueError("Please answer Goods or Service.")
        return gos.value
    raise ValueError(f"Field '{field}' cannot be corrected.")
