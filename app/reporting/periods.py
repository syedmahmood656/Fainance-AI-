"""Turn a *period description* into real dates.

The LLM only says things like {"kind": "month", "month": 9}. Python does the
date arithmetic, so "last month" can never be off by a day because a model
miscounted.
"""
import calendar
from datetime import date
from typing import Optional, Tuple

from app.schemas.enums import PeriodKind


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, delta: int) -> date:
    """Return the first day of the month `delta` months away from d's month."""
    index = d.year * 12 + (d.month - 1) + delta
    return date(index // 12, index % 12 + 1, 1)


def _label_month(d: date) -> str:
    return d.strftime("%B %Y")


def resolve_period(
    kind,
    today: date,
    year: Optional[int] = None,
    month: Optional[int] = None,
    months: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
) -> Tuple[Optional[date], Optional[date], str]:
    """Return (start, end, label). start/end are inclusive; None = unbounded."""
    kind = PeriodKind(kind) if kind else PeriodKind.THIS_MONTH

    if kind == PeriodKind.THIS_MONTH:
        return month_start(today), month_end(today), _label_month(today)

    if kind == PeriodKind.LAST_MONTH:
        first = add_months(today, -1)
        return first, month_end(first), _label_month(first)

    if kind == PeriodKind.THIS_YEAR:
        return date(today.year, 1, 1), date(today.year, 12, 31), str(today.year)

    if kind == PeriodKind.LAST_YEAR:
        y = today.year - 1
        return date(y, 1, 1), date(y, 12, 31), str(y)

    if kind == PeriodKind.YEAR:
        y = year or today.year
        return date(y, 1, 1), date(y, 12, 31), str(y)

    if kind == PeriodKind.MONTH:
        if not month or not 1 <= month <= 12:
            # Unusable month -> fall back to the current month rather than guessing.
            return month_start(today), month_end(today), _label_month(today)
        if year is None:
            # "September" asked in October -> this year; "December" asked in October -> last year.
            year = today.year if month <= today.month else today.year - 1
        first = date(year, month, 1)
        return first, month_end(first), _label_month(first)

    if kind == PeriodKind.LAST_N_MONTHS:
        n = max(1, min(int(months or 6), 36))
        first = add_months(today, -(n - 1))
        last = month_end(today)
        label = f"Last {n} months ({first.strftime('%b %Y')} – {last.strftime('%b %Y')})"
        return first, last, label

    if kind == PeriodKind.CUSTOM:
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        if start_date and end_date:
            label = f"{start_date.strftime('%d %b %Y')} – {end_date.strftime('%d %b %Y')}"
        elif start_date:
            label = f"From {start_date.strftime('%d %b %Y')}"
        elif end_date:
            label = f"Until {end_date.strftime('%d %b %Y')}"
        else:
            label = "All time"
        return start_date, end_date, label

    return None, None, "All time"


def month_range(start: date, end: date):
    """Yield (year, month) for every month between start and end inclusive."""
    cur = month_start(start)
    last = month_start(end)
    while cur <= last:
        yield cur.year, cur.month
        cur = add_months(cur, 1)


def parse_iso_date(value) -> Optional[date]:
    if value in (None, "", "null"):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


__all__ = [
    "resolve_period", "month_range", "month_start", "month_end",
    "add_months", "parse_iso_date",
]
