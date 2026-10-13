"""The reporting tool implementations.

Every function takes plain JSON-friendly arguments and returns a plain dict.
Numbers come from PostgreSQL aggregations (SUM/COUNT/GROUP BY); charts and Excel
files are drawn from those results. No LLM is involved in any of this.

These functions are exposed to the app through the MCP server
(app/mcp_server/server.py), but they can also be called directly (USE_MCP=false).

`user_id=None` means "the whole organization".
"""
import os
import re
import uuid
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from app.config.settings import settings
from app.database.models import SessionLocal
from app.database.repository import ExpenseFilter, ExpenseRepository
from app.reporting import charts
from app.reporting.excel import build_workbook
from app.reporting.periods import month_range, parse_iso_date
from app.reporting.timeline import bucket_series, choose_granularity
from app.schemas.enums import normalize_category
from app.utils.formatting import format_money


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _out_dir() -> str:
    os.makedirs(settings.REPORTS_TMP_DIR, exist_ok=True)
    return settings.REPORTS_TMP_DIR


def _new_path(prefix: str, ext: str) -> str:
    return os.path.join(_out_dir(), f"{prefix}_{uuid.uuid4().hex[:10]}.{ext}")


def _label(start: Optional[date], end: Optional[date], given: Optional[str]) -> str:
    if given:
        return given
    if start and end:
        return f"{start.isoformat()} to {end.isoformat()}"
    return "All time"


def _category(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cat = normalize_category(value)
    if cat is None:
        raise ValueError(f"Unknown category: {value}")
    return cat.value


def _context(session, user_id, start_date, end_date, category):
    """Build the filter and choose ONE currency so amounts are never added across currencies."""
    repo = ExpenseRepository(session)
    f = ExpenseFilter(user_id=user_id, start=parse_iso_date(start_date), end=parse_iso_date(end_date),
                      category=_category(category))
    counts = repo.currency_counts(f)
    if not counts:
        currency = settings.DEFAULT_CURRENCY
    elif settings.DEFAULT_CURRENCY in counts:
        currency = settings.DEFAULT_CURRENCY
    else:
        currency = max(counts, key=counts.get)
    other = sum(n for cur, n in counts.items() if cur != currency)
    f.currency = currency
    return repo, f, currency, other


def _base(label, currency, other, **extra) -> Dict[str, Any]:
    return {"period_label": label, "currency": currency, "other_currency_count": other, **extra}


def expense_to_dict(e) -> Dict[str, Any]:
    num = lambda v: float(v) if v is not None else None  # noqa: E731
    return {
        "id": e.id, "expense_date": e.expense_date, "vendor": e.vendor, "invoice_number": e.invoice_number,
        "category": e.category, "item": e.item, "goods_or_service": e.goods_or_service,
        "expense_amount": num(e.expense_amount), "currency": e.currency, "subtotal": num(e.subtotal),
        "tax": num(e.tax), "description": e.description, "corrected_by_user": bool(e.corrected_by_user),
    }


def _with_percent(rows: List[dict], total: float) -> List[dict]:
    for r in rows:
        r["percent"] = round(r["total"] / total * 100, 1) if total else 0.0
    return rows


def _month_rows(repo, f, start: Optional[date], end: Optional[date]) -> List[dict]:
    """Per-month totals from SQL, with zero-filled gaps so charts show empty months."""
    data = {(r["year"], r["month"]): r for r in repo.by_month(f)}
    if not data and not (start and end):
        return []
    first = start or date(min(data)[0], min(data)[1], 1)
    last = end or date(max(data)[0], max(data)[1], 1)
    rows = []
    for y, m in month_range(first, last):
        hit = data.get((y, m))
        rows.append({"month": f"{y}-{m:02d}", "label": date(y, m, 1).strftime("%b %Y"),
                     "total": hit["total"] if hit else 0.0, "count": hit["count"] if hit else 0})
    return rows


# ---------------------------------------------------------------------------
# data tools
# ---------------------------------------------------------------------------
def get_expense_summary(user_id: Optional[int] = None, start_date: Optional[str] = None,
                        end_date: Optional[str] = None, category: Optional[str] = None,
                        period_label: Optional[str] = None) -> Dict[str, Any]:
    """Total, count and average of confirmed expenses, plus the biggest category."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        count, total = repo.totals(f)
        top = repo.by_category(f)[:1]
        return _base(_label(f.start, f.end, period_label), currency, other,
                     empty=count == 0, total=total, count=count,
                     average=round(total / count, 2) if count else 0.0,
                     category=f.category, top_category=top[0] if top else None)


def get_category_breakdown(user_id: Optional[int] = None, start_date: Optional[str] = None,
                           end_date: Optional[str] = None, category: Optional[str] = None,
                           period_label: Optional[str] = None) -> Dict[str, Any]:
    """Totals per category, largest first, with share of the total."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        rows = repo.by_category(f)
        total = round(sum(r["total"] for r in rows), 2)
        return _base(_label(f.start, f.end, period_label), currency, other,
                     empty=not rows, total=total, count=sum(r["count"] for r in rows),
                     rows=_with_percent(rows, total))


def get_monthly_expense_trend(user_id: Optional[int] = None, start_date: Optional[str] = None,
                              end_date: Optional[str] = None, category: Optional[str] = None,
                              period_label: Optional[str] = None) -> Dict[str, Any]:
    """Total per month between start_date and end_date (empty months are 0)."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        rows = _month_rows(repo, f, f.start, f.end)
        total = round(sum(r["total"] for r in rows), 2)
        return _base(_label(f.start, f.end, period_label), currency, other,
                     empty=not any(r["count"] for r in rows), total=total, rows=rows)


def get_yearly_expense_summary(user_id: Optional[int] = None, year: Optional[int] = None,
                               category: Optional[str] = None) -> Dict[str, Any]:
    """A whole year: total, per-month totals and per-category totals."""
    year = int(year or date.today().year)
    start, end = date(year, 1, 1), date(year, 12, 31)
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start.isoformat(), end.isoformat(), category)
        count, total = repo.totals(f)
        categories = _with_percent(repo.by_category(f), total)
        return _base(str(year), currency, other, empty=count == 0, year=year, total=total, count=count,
                     months=_month_rows(repo, f, start, end), categories=categories)


def list_expenses(user_id: Optional[int] = None, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, category: Optional[str] = None,
                  period_label: Optional[str] = None, limit: int = 25) -> Dict[str, Any]:
    """The individual expenses (newest first), capped at `limit`."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        total_count = repo.count_expenses(f)
        rows = [expense_to_dict(e) for e in repo.list_expenses(f, limit=max(1, min(limit, 100)))]
        for r in rows:
            r["expense_date"] = r["expense_date"].isoformat() if r["expense_date"] else None
        return _base(_label(f.start, f.end, period_label), currency, other,
                     empty=total_count == 0, total_count=total_count, rows=rows)


# ---------------------------------------------------------------------------
# chart tools (data from SQL -> PNG file)
# ---------------------------------------------------------------------------
def generate_category_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            style: str = "bar", top_n: Optional[int] = None,
                            period_label: Optional[str] = None) -> Dict[str, Any]:
    """style: 'bar' (all categories), 'pie' (distribution) or 'top' (top N categories)."""
    if style not in ("bar", "pie", "top"):
        raise ValueError("style must be bar, pie or top")
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        rows = repo.by_category(f)
        label = _label(f.start, f.end, period_label)
        total = round(sum(r["total"] for r in rows), 2)
        if not rows:
            return _base(label, currency, other, empty=True)
        shown = rows[: (top_n or 5)] if style == "top" else rows[:12]
        names, values = [r["category"] for r in shown], [r["total"] for r in shown]
        subtitle = f"{label} · Total {format_money(total, currency)}"
        path = _new_path("chart_category", "png")
        if style == "pie":
            charts.pie(names, values, currency, "Category distribution", subtitle, path)
        else:
            title = f"Top {len(shown)} spending categories" if style == "top" else "Expenses by category"
            charts.horizontal_bars(names, values, currency, title, subtitle, path)
        return _base(label, currency, other, empty=False, chart_path=path, total=total,
                     rows=_with_percent(rows, total))


def generate_monthly_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                           end_date: Optional[str] = None, category: Optional[str] = None,
                           period_label: Optional[str] = None) -> Dict[str, Any]:
    """Bar chart of total spending per month."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        rows = _month_rows(repo, f, f.start, f.end)
        label = _label(f.start, f.end, period_label)
        if not any(r["count"] for r in rows):
            return _base(label, currency, other, empty=True)
        total = round(sum(r["total"] for r in rows), 2)
        path = _new_path("chart_monthly", "png")
        charts.vertical_bars([r["label"] for r in rows], [r["total"] for r in rows], currency,
                             "Monthly expenses" + (f" — {f.category}" if f.category else ""),
                             f"{label} · Total {format_money(total, currency)}", path)
        return _base(label, currency, other, empty=False, chart_path=path, total=total, rows=rows)


def generate_timeline_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            granularity: str = "auto", cumulative: bool = False,
                            period_label: Optional[str] = None) -> Dict[str, Any]:
    """Line graph of expense amount over time. granularity: auto/day/week/month;
    cumulative=True plots the running total instead of per-period totals."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        daily = repo.by_day(f)
        label = _label(f.start, f.end, period_label)
        if not daily:
            return _base(label, currency, other, empty=True)
        unit = choose_granularity(f.start, f.end, granularity)
        points = bucket_series(daily, f.start, f.end, unit, today=date.today())
        values = [p["cumulative" if cumulative else "total"] for p in points]
        total = round(sum(p["total"] for p in points), 2)
        path = _new_path("chart_timeline", "png")
        charts.line_timeline(
            [p["label"] for p in points], values, currency,
            ("Cumulative expenses over time" if cumulative else "Expenses over time")
            + (f" — {f.category}" if f.category else ""),
            f"{label} · per {unit} · Total {format_money(total, currency)}", path, bool(cumulative))
        return _base(label, currency, other, empty=False, chart_path=path, total=total,
                     granularity=unit, cumulative=bool(cumulative), rows=points)


def generate_bar_chart(user_id: Optional[int] = None, group_by: str = "category",
                       start_date: Optional[str] = None, end_date: Optional[str] = None,
                       category: Optional[str] = None, top_n: int = 10,
                       period_label: Optional[str] = None) -> Dict[str, Any]:
    """Generic bar chart. group_by: 'category', 'month' or 'vendor'."""
    if group_by == "category":
        return generate_category_chart(user_id, start_date, end_date, category, "bar", top_n, period_label)
    if group_by == "month":
        return generate_monthly_chart(user_id, start_date, end_date, category, period_label)
    if group_by != "vendor":
        raise ValueError("group_by must be category, month or vendor")
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        rows = repo.by_vendor(f, limit=max(1, min(top_n, 12)))
        label = _label(f.start, f.end, period_label)
        if not rows:
            return _base(label, currency, other, empty=True)
        total = round(sum(r["total"] for r in rows), 2)
        path = _new_path("chart_vendor", "png")
        charts.horizontal_bars([r["vendor"] for r in rows], [r["total"] for r in rows], currency,
                               f"Top {len(rows)} vendors", f"{label} · Total {format_money(total, currency)}", path)
        return _base(label, currency, other, empty=False, chart_path=path, total=total, rows=rows)


# ---------------------------------------------------------------------------
# Excel report
# ---------------------------------------------------------------------------
def generate_expense_report(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            period_label: Optional[str] = None) -> Dict[str, Any]:
    """Excel workbook (Summary, Expenses, Category Summary, Monthly Summary) with the filtered rows."""
    with SessionLocal() as session:
        repo, f, currency, other = _context(session, user_id, start_date, end_date, category)
        label = _label(f.start, f.end, period_label)
        expenses = [expense_to_dict(e) for e in repo.list_expenses(f, newest_first=False)]
        if not expenses:
            return _base(label, currency, other, empty=True)
        wb = build_workbook(expenses, label, currency, f.category, other)
        slug = re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_")[:40] or "report"
        path = _new_path(f"expense_report_{slug}", "xlsx")
        wb.save(path)
        total = round(sum(e["expense_amount"] or 0 for e in expenses), 2)
        return _base(label, currency, other, empty=False, file_path=path, count=len(expenses), total=total)


# name -> function. The MCP server registers exactly these; the direct mode uses the same table.
TOOLS: Dict[str, Callable[..., Dict[str, Any]]] = {
    "get_expense_summary": get_expense_summary,
    "get_category_breakdown": get_category_breakdown,
    "get_monthly_expense_trend": get_monthly_expense_trend,
    "get_yearly_expense_summary": get_yearly_expense_summary,
    "list_expenses": list_expenses,
    "generate_bar_chart": generate_bar_chart,
    "generate_category_chart": generate_category_chart,
    "generate_monthly_chart": generate_monthly_chart,
    "generate_timeline_chart": generate_timeline_chart,
    "generate_expense_report": generate_expense_report,
}

