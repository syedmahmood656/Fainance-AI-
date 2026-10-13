"""Pure helpers for the "expense x timeline" graph.

SQL returns one row per day that has expenses. This module (deterministic Python,
no LLM) picks a sensible bucket size, zero-fills the empty periods so the line is
continuous, and computes the running (cumulative) total.
"""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from app.reporting.periods import add_months

GRANULARITIES = ("day", "week", "month")


def choose_granularity(start: Optional[date], end: Optional[date], requested: Optional[str] = "auto") -> str:
    """'auto' -> day for <= 62 days, week for <= 200 days, otherwise month."""
    if requested in GRANULARITIES:
        return requested
    if start is None or end is None:
        return "month"
    days = (end - start).days + 1
    if days <= 62:
        return "day"
    return "week" if days <= 200 else "month"


def bucket_start(d: date, granularity: str) -> date:
    if granularity == "week":
        return d - timedelta(days=d.weekday())      # Monday
    if granularity == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: date, granularity: str) -> date:
    if granularity == "week":
        return d + timedelta(days=7)
    if granularity == "month":
        return add_months(d, 1)
    return d + timedelta(days=1)


def bucket_label(d: date, granularity: str) -> str:
    if granularity == "month":
        return d.strftime("%b %Y")
    if granularity == "week":
        return d.strftime("%d %b")
    return d.strftime("%d %b")


def bucket_series(daily_rows: List[Dict[str, Any]], start: Optional[date], end: Optional[date],
                  granularity: str, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """daily_rows: [{'date': date, 'total': float, 'count': int}] -> continuous bucket list.

    Each item: {bucket (ISO date), label, total, count, cumulative}.
    `today` caps the line for a still-running period (this month stops at today, not month-end).
    """
    if not daily_rows:
        return []
    first_data = min(r["date"] for r in daily_rows)
    last_data = max(r["date"] for r in daily_rows)
    first = start or first_data
    last = end or last_data
    if today is not None:
        last = min(last, max(today, last_data))
    if last < first:
        last = first

    sums: Dict[date, List[float]] = {}
    for row in daily_rows:
        slot = sums.setdefault(bucket_start(row["date"], granularity), [0.0, 0])
        slot[0] += float(row["total"])
        slot[1] += int(row["count"])

    out, running = [], 0.0
    cur, stop = bucket_start(first, granularity), bucket_start(last, granularity)
    while cur <= stop:
        total, count = sums.get(cur, (0.0, 0))
        running += total
        out.append({"bucket": cur.isoformat(), "label": bucket_label(cur, granularity),
                    "total": round(total, 2), "count": int(count), "cumulative": round(running, 2)})
        cur = _next_bucket(cur, granularity)
    return out
