"""Formatting helpers. Everything here is plain Python so it is easy to test."""
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable, Optional

CURRENCY_SYMBOLS = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£", "AED": "AED ", "SAR": "SAR "}

TELEGRAM_MAX_CHARS = 4000  # hard limit is 4096


def _group_indian(integer_part: str) -> str:
    """12345678 -> 1,23,45,678"""
    if len(integer_part) <= 3:
        return integer_part
    head, tail = integer_part[:-3], integer_part[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def format_number(amount, currency: Optional[str] = None) -> str:
    """2450 -> '2,450', 2450.5 -> '2,450.50'. Indian digit grouping for INR."""
    if amount is None:
        return "—"
    value = Decimal(str(amount)).quantize(Decimal("0.01"))
    negative = value < 0
    value = abs(value)
    whole, _, frac = f"{value:.2f}".partition(".")
    if (currency or "INR").upper() == "INR":
        whole = _group_indian(whole)
    else:
        whole = f"{int(whole):,}"
    text = whole if frac == "00" else f"{whole}.{frac}"
    return f"-{text}" if negative else text


def format_money(amount, currency: Optional[str] = None) -> str:
    """2450, 'INR' -> '₹2,450'"""
    if amount is None:
        return "Not found"
    cur = (currency or "INR").upper()
    symbol = CURRENCY_SYMBOLS.get(cur, f"{cur} ")
    return f"{symbol}{format_number(amount, cur)}"


def format_compact(value: float, currency: Optional[str] = None) -> str:
    """Short axis labels: 150000 -> '1.5L' (INR) or '150K' (others)."""
    v = abs(float(value))
    sign = "-" if value < 0 else ""
    if (currency or "INR").upper() == "INR":
        if v >= 1e7:
            return f"{sign}{v / 1e7:.1f}Cr".replace(".0Cr", "Cr")
        if v >= 1e5:
            return f"{sign}{v / 1e5:.1f}L".replace(".0L", "L")
        if v >= 1e3:
            return f"{sign}{v / 1e3:.1f}K".replace(".0K", "K")
    else:
        if v >= 1e6:
            return f"{sign}{v / 1e6:.1f}M".replace(".0M", "M")
        if v >= 1e3:
            return f"{sign}{v / 1e3:.1f}K".replace(".0K", "K")
    return f"{sign}{v:.0f}"


def format_date(d) -> str:
    """date(2026,10,8) -> '08 Oct 2026'"""
    if d is None:
        return "Not found"
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.strftime("%d %b %Y")
    return str(d)


def truncate_for_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n… (truncated)"


def or_unknown(value) -> str:
    return str(value) if value not in (None, "") else "Not found"


def format_expense_card(expense, warnings: Optional[Iterable[str]] = None, title: str = "🧾 Receipt processed") -> str:
    """The confirmation card shown to the user. `expense` is any object with the
    final-value attributes (an ORM row or a simple namespace)."""
    lines = [
        title,
        "",
        f"Vendor: {or_unknown(expense.vendor)}",
        f"Date: {format_date(expense.expense_date)}",
        f"Category: {or_unknown(expense.category)}",
        f"Item: {or_unknown(expense.item)}",
        f"Type: {or_unknown(expense.goods_or_service)}",
        f"Expense: {format_money(expense.expense_amount, expense.currency)}",
    ]
    if getattr(expense, "invoice_number", None):
        lines.append(f"Invoice #: {expense.invoice_number}")
    warnings = [w for w in (warnings or []) if w]
    if warnings:
        lines.append("")
        lines.extend(f"⚠️ {w}" for w in warnings)
    return "\n".join(lines)


def format_category_lines(rows, currency: str, with_percent: bool = True) -> str:
    """rows: [{'category': 'Travel', 'total': 42000.0, 'percent': 33.3}, ...]"""
    out = []
    for r in rows:
        line = f"{r['category']} — {format_money(r['total'], currency)}"
        if with_percent and r.get("percent") is not None:
            line += f" ({r['percent']:.0f}%)"
        out.append(line)
    return "\n".join(out)
