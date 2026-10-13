"""Deterministic glue between a parsed ReportRequest and the MCP tools.

    ReportRequest --plan_tool_calls--> [(tool, args), ...]
    tool results  --build_reply------> text + chart files + excel file

No LLM here: choosing the tool is a plain lookup, and every number shown comes
straight from the tool results (i.e. from PostgreSQL).
"""
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from app.reporting.periods import resolve_period
from app.schemas.enums import ChartKind, PeriodKind, ReportIntent
from app.utils.formatting import format_category_lines, format_date, format_money

ToolCall = Tuple[str, Dict[str, Any]]

_SINGLE_MONTH_KINDS = {PeriodKind.THIS_MONTH, PeriodKind.LAST_MONTH, PeriodKind.MONTH}


def plan_tool_calls(req, scope_user_id: Optional[int], today: date) -> List[ToolCall]:
    """Pick the MCP tool(s) for a request. `scope_user_id=None` = whole organization."""
    p = req.period
    kind, months = p.kind, p.months
    intent, chart = req.intent, req.chart_type

    wants_months = intent == ReportIntent.MONTHLY_TREND or (
        intent == ReportIntent.CHART and chart == ChartKind.MONTHLY_BAR)
    if wants_months and kind in _SINGLE_MONTH_KINDS:
        kind, months = PeriodKind.LAST_N_MONTHS, 6

    start, end, label = resolve_period(kind, today, p.year, p.month, months, p.start_date, p.end_date)
    base: Dict[str, Any] = {
        "user_id": scope_user_id,
        "start_date": start.isoformat() if start else None,
        "end_date": end.isoformat() if end else None,
        "category": req.category.value if req.category else None,
        "period_label": label,
    }
    category_style = {ChartKind.CATEGORY_PIE: "pie", ChartKind.TOP_CATEGORIES: "top"}.get(chart, "bar")
    cat_chart_args = {**base, "style": category_style, "top_n": req.top_n}

    if intent == ReportIntent.SUMMARY:
        return [("get_expense_summary", base)]
    if intent == ReportIntent.CATEGORY_BREAKDOWN:
        return [("get_category_breakdown", base), ("generate_category_chart", cat_chart_args)]
    if intent == ReportIntent.MONTHLY_TREND:
        return [("get_monthly_expense_trend", base), ("generate_monthly_chart", base)]
    if intent == ReportIntent.YEARLY_SUMMARY or (intent == ReportIntent.CHART and chart == ChartKind.YEARLY_TREND):
        year = start.year if start else today.year
        year_args = {**base, "start_date": date(year, 1, 1).isoformat(), "end_date": date(year, 12, 31).isoformat(),
                     "period_label": str(year)}
        if intent == ReportIntent.YEARLY_SUMMARY:
            return [("get_yearly_expense_summary", {"user_id": scope_user_id, "year": year, "category": base["category"]}),
                    ("generate_monthly_chart", year_args)]
        return [("generate_monthly_chart", year_args)]
    if intent == ReportIntent.CHART:
        if chart == ChartKind.TIMELINE:
            return [("generate_timeline_chart", {**base, "granularity": getattr(req, "granularity", None) or "auto",
                                                 "cumulative": bool(getattr(req, "cumulative", False))})]
        if chart == ChartKind.MONTHLY_BAR:
            return [("generate_monthly_chart", base)]
        if chart == ChartKind.VENDOR_BAR:
            return [("generate_bar_chart", {**base, "group_by": "vendor", "top_n": req.top_n or 10})]
        return [("generate_category_chart", cat_chart_args)]
    if intent == ReportIntent.EXCEL_REPORT:
        return [("generate_expense_report", base)]
    if intent == ReportIntent.EXPENSE_LIST:
        return [("list_expenses", base)]
    return []


# ---------------------------------------------------------------------------
# Reply formatting (numbers come only from tool results)
# ---------------------------------------------------------------------------
def _scope_line(res: Dict[str, Any]) -> str:
    cat = res.get("category")
    return res["period_label"] + (f" · {cat}" if cat else "")


def _text_for(tool: str, res: Dict[str, Any]) -> str:
    cur = res["currency"]
    if tool == "get_expense_summary":
        lines = [f"📊 Expense summary — {_scope_line(res)}", "",
                 f"Total: {format_money(res['total'], cur)}",
                 f"Expenses: {res['count']}",
                 f"Average: {format_money(res['average'], cur)}"]
        top = res.get("top_category")
        if top:
            lines.append(f"Top category: {top['category']} — {format_money(top['total'], cur)}")
        return "\n".join(lines)

    if tool == "generate_timeline_chart":
        rows = res["rows"]
        peak = max(rows, key=lambda r: r["total"])
        unit = res["granularity"]
        what = "Cumulative expenses" if res.get("cumulative") else "Expenses"
        lines = [f"📈 {what} over time — {_scope_line(res)} (per {unit})", "",
                 f"Total: {format_money(res['total'], cur)}",
                 f"Average per {unit}: {format_money(res['total'] / len(rows), cur)}",
                 f"Peak {unit}: {peak['label']} — {format_money(peak['total'], cur)}"]
        return "\n".join(lines)

    if tool in ("get_category_breakdown", "generate_category_chart", "generate_bar_chart") and res.get("rows") \
            and "category" in res["rows"][0]:
        return (f"📊 Expenses by category — {_scope_line(res)}\n\n"
                f"{format_category_lines(res['rows'], cur)}\n\nTotal: {format_money(res['total'], cur)}")

    if tool in ("get_monthly_expense_trend", "generate_monthly_chart"):
        lines = [f"{r['label']} — {format_money(r['total'], cur)}" for r in res["rows"]]
        return (f"📅 Monthly expenses — {_scope_line(res)}\n\n" + "\n".join(lines) +
                f"\n\nTotal: {format_money(res['total'], cur)}")

    if tool == "get_yearly_expense_summary":
        lines = [f"📆 Yearly summary — {res['period_label']}", "", f"Total: {format_money(res['total'], cur)}",
                 f"Expenses: {res['count']}", "", "By month:"]
        lines += [f"  {m['label']} — {format_money(m['total'], cur)}" for m in res["months"] if m["count"]]
        lines += ["", "Top categories:"]
        lines += [f"  {c['category']} — {format_money(c['total'], cur)} ({c['percent']:.0f}%)"
                  for c in res["categories"][:5]]
        return "\n".join(lines)

    if tool == "generate_bar_chart" and res.get("rows"):  # vendors
        lines = [f"{r['vendor']} — {format_money(r['total'], cur)}" for r in res["rows"]]
        return f"🏷️ Top vendors — {_scope_line(res)}\n\n" + "\n".join(lines)

    if tool == "list_expenses":
        lines = []
        for r in res["rows"]:
            lines.append(f"{format_date(date.fromisoformat(r['expense_date'])) if r['expense_date'] else '—'} · "
                         f"{r['vendor'] or 'Unknown'} · {format_money(r['expense_amount'], r['currency'])} · "
                         f"{r['category'] or '—'}")
        more = res["total_count"] - len(res["rows"])
        tail = f"\n… and {more} more (ask for an Excel report to get them all)" if more > 0 else ""
        return f"🧾 Expenses — {_scope_line(res)}\n\n" + "\n".join(lines) + tail

    if tool == "generate_expense_report":
        return (f"📁 Excel report ready — {_scope_line(res)}\n"
                f"{res['count']} expenses · Total {format_money(res['total'], cur)}")
    return ""


def build_reply(calls: List[ToolCall], results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """-> {"text": str, "chart_paths": [...], "excel_path": str|None}"""
    chart_paths: List[str] = []
    excel_path: Optional[str] = None
    parts: List[str] = []
    notes: List[str] = []
    chart_text_fallback: Optional[str] = None

    for (tool, _args), res in zip(calls, results):
        if res.get("empty"):
            cat = f" for {res['category']}" if res.get("category") else ""
            parts.append(f"No confirmed expenses found for {res['period_label']}{cat}.")
            continue
        if res.get("other_currency_count"):
            notes.append(f"ℹ️ {res['other_currency_count']} expense(s) in other currencies are not included "
                         f"(totals are in {res['currency']}).")
        if res.get("chart_path"):
            chart_paths.append(res["chart_path"])
        if res.get("file_path"):
            excel_path = res["file_path"]
        text = _text_for(tool, res)
        if tool.startswith("generate_") and tool != "generate_expense_report":
            chart_text_fallback = chart_text_fallback or text   # only used if no data tool printed text
        elif text:
            parts.append(text)

    if not parts and chart_text_fallback:
        parts.append(chart_text_fallback)
    if excel_path and not parts:
        parts.append("📁 Excel report ready.")
    # de-duplicate repeated "no expenses" lines when several tools came back empty
    seen, unique = set(), []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    text = "\n\n".join(unique + sorted(set(notes)))
    return {"text": text or "No data found.", "chart_paths": chart_paths, "excel_path": excel_path}
