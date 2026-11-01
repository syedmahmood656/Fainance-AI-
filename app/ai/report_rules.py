"""Deterministic fallback for understanding a report request.

The LLM is the primary interpreter (it handles unusual wording). When every
model is rate-limited or down, this keyword parser still answers the common
requests, so the bot degrades gracefully instead of going silent.

Returns a plain dict that ReportRequest(**result) accepts.
"""
import re
from datetime import date
from typing import Any, Dict, Optional

from app.schemas.enums import ExpenseCategory, normalize_category

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
    "oct": 10, "nov": 11, "dec": 12,
}
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                 "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}

_C = ExpenseCategory
# single words a user might say -> category (full category names are matched separately, first)
_CATEGORY_WORDS = {
    "printing": _C.PRINTING_STATIONARY, "stationary": _C.PRINTING_STATIONARY, "stationery": _C.PRINTING_STATIONARY,
    "xerox": _C.PRINTING_STATIONARY,
    "conveyance": _C.CONVEYANCE, "taxi": _C.CONVEYANCE, "cab": _C.CONVEYANCE, "fuel": _C.CONVEYANCE,
    "transport": _C.CONVEYANCE, "transportation": _C.CONVEYANCE,
    "travel": _C.TRAVELLING_EXPENSES, "travelling": _C.TRAVELLING_EXPENSES, "traveling": _C.TRAVELLING_EXPENSES,
    "recharge": _C.MOBILE_RECHARGE,
    "internet": _C.INTERNET, "wifi": _C.INTERNET, "broadband": _C.INTERNET,
    "books": _C.BOOKS_LIBRARY, "library": _C.BOOKS_LIBRARY,
    "hospitality": _C.GUEST_HOSPITALITY, "guest": _C.GUEST_HOSPITALITY, "guests": _C.GUEST_HOSPITALITY,
    "camp": _C.SUMMER_ISLAMIC_CAMP,
    "vocational": _C.ISLAMIC_VOCATIONAL_CLASSES,
    "infaq": _C.INFAQ, "infak": _C.INFAQ,
    "zakath": _C.ZAKATH, "zakat": _C.ZAKATH, "zakah": _C.ZAKATH,
    "zonal": _C.ZONAL_SHARE,
    "miscellaneous": _C.MISCELLANEOUS,
}
del _C


def _has(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}", text) for w in words)


def _find_category(text: str) -> Optional[str]:
    for cat in ExpenseCategory:  # full names first ("office supplies", "food & meals")
        if cat.value.lower() in text or cat.value.lower().replace("&", "and") in text:
            return cat.value
    for word, cat in _CATEGORY_WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return cat.value
    return None


def _find_last_n_months(text: str) -> Optional[int]:
    m = re.search(r"\b(?:last|past|previous)\s+(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+months?\b", text)
    if not m:
        return None
    raw = m.group(1)
    return int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]


def _find_period(text: str) -> Optional[Dict[str, Any]]:
    n = _find_last_n_months(text)
    if n:
        return {"kind": "last_n_months", "months": n}
    if re.search(r"\b(this|current)\s+month\b", text):
        return {"kind": "this_month"}
    if re.search(r"\b(last|previous)\s+month\b", text):
        return {"kind": "last_month"}
    if re.search(r"\b(last|previous)\s+year\b", text):
        return {"kind": "last_year"}
    if re.search(r"\b(this|current)\s+year\b", text):
        return {"kind": "this_year"}
    if re.search(r"\b(all\s+time|overall|so\s+far|till\s+date|to\s+date|ever)\b", text):
        return {"kind": "all"}

    year_match = re.search(r"\b(20\d{2})\b", text)
    for name, number in _MONTHS.items():
        if re.search(rf"\b{name}\b", text):
            period: Dict[str, Any] = {"kind": "month", "month": number}
            if year_match:
                period["year"] = int(year_match.group(1))
            return period
    if year_match:
        return {"kind": "year", "year": int(year_match.group(1))}
    return None


def _find_amount(text: str) -> Optional[float]:
    m = re.search(r"(?:₹|rs\.?|inr|\$)\s*([\d,]+(?:\.\d+)?)", text)
    if not m:
        # a bare number such as "2,450" or "6500" (skip things that look like years)
        for cand in re.findall(r"\b\d{1,3}(?:,\d{2,3})+(?:\.\d+)?\b|\b\d{3,}(?:\.\d+)?\b", text):
            if not re.fullmatch(r"(19|20)\d{2}", cand):
                m = re.match(r"(.*)", cand)
                break
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", "").rstrip("."))
    except ValueError:
        return None


def rule_based_request(text: str, today: Optional[date] = None) -> Dict[str, Any]:
    t = " ".join((text or "").lower().split())
    result: Dict[str, Any] = {"intent": "unknown", "period": {"kind": "all"}}   # no period named -> all time
    if not t:
        return result

    asks_receipt = _has(t, "receipt", "receipts", "bill image", "invoice image") or (
        _has(t, "invoice", "bill") and _has(t, "show", "send", "see", "view", "photo", "image", "picture"))
    asks_show = _has(t, "show", "send", "get", "find", "see", "view", "give", "display", "retrieve", "photo",
                     "image", "picture", "where")
    wants_excel = _has(t, "excel", "xlsx", "spreadsheet", "workbook") or (_has(t, "download") and _has(t, "report"))
    wants_chart = _has(t, "chart", "graph", "plot", "visual", "pie", "histogram", "diagram")
    wants_timeline = _has(t, "timeline", "time series", "daily", "weekly", "day by day", "day-wise", "daywise",
                          "cumulative", "running total") or bool(
        re.search(r"\bover\s+time\b|\b(vs|versus|against)\s+(time|date|days)\b|\bx\s+(time|timeline)\b", t))
    trend_words = _has(t, "trend", "monthly", "month by month", "month-wise", "monthwise", "by month", "per month",
                       "each month") or _find_last_n_months(t)

    if asks_receipt and asks_show and not wants_excel:
        result["intent"] = "receipt_lookup"
        result["amount"] = _find_amount(t)
    elif wants_excel:
        result["intent"] = "excel_report"
    elif wants_timeline:
        result["intent"] = "chart"
        result["chart_type"] = "timeline"
        if _has(t, "daily", "day by day", "day-wise", "daywise"):
            result["granularity"] = "day"
        elif _has(t, "weekly"):
            result["granularity"] = "week"
        elif _has(t, "monthly"):
            result["granularity"] = "month"
        result["cumulative"] = _has(t, "cumulative", "running total")
    elif wants_chart:
        result["intent"] = "chart"
        if _has(t, "pie", "distribution", "share", "split"):
            result["chart_type"] = "category_pie"
        elif _has(t, "top"):
            result["chart_type"] = "top_categories"
        elif _has(t, "vendor", "vendors", "supplier"):
            result["chart_type"] = "vendor_bar"
        elif _has(t, "category", "categories"):
            result["chart_type"] = "category_bar"
        elif trend_words or _has(t, "months"):
            result["chart_type"] = "monthly_bar"
        elif _has(t, "yearly", "annual", "year"):
            result["chart_type"] = "yearly_trend"
        else:
            result["chart_type"] = "category_bar"
    elif trend_words:
        result["intent"] = "monthly_trend"
    elif _has(t, "yearly", "annual") or re.search(r"\b(year|annual)\s+(summary|report|overview)\b", t):
        result["intent"] = "yearly_summary"
    elif _has(t, "category", "categories", "breakdown", "break down"):
        result["intent"] = "category_breakdown"
    elif _has(t, "list", "detailed", "details", "transactions") or "all expenses" in t:
        result["intent"] = "expense_list"
    elif _has(t, "how much", "total", "spent", "spend", "spending", "expense", "expenses", "cost"):
        result["intent"] = "summary"

    top = re.search(r"\btop\s+(\d{1,2})\b", t)
    if top:
        result["top_n"] = int(top.group(1))

    category = _find_category(t)
    if category:
        result["category"] = category

    period = _find_period(t)
    if period:
        result["period"] = period
    else:  # sensible defaults per intent
        if result["intent"] in ("yearly_summary",) or (result["intent"] in ("excel_report", "chart")
                                                       and _has(t, "yearly", "annual")):
            result["period"] = {"kind": "this_year"}
        elif result.get("chart_type") == "timeline":
            result["period"] = {"kind": "last_n_months", "months": 6}
        elif result["intent"] == "monthly_trend" or result.get("chart_type") == "monthly_bar":
            result["period"] = {"kind": "last_n_months", "months": 6}
        elif result["intent"] == "yearly_summary" or result.get("chart_type") == "yearly_trend":
            result["period"] = {"kind": "this_year"}
        elif result["intent"] == "receipt_lookup" and not period:
            result["period"] = {"kind": "all"}
    return result
