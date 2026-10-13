"""MCP server exposing the reporting / visualization tools.

Run by the app as a subprocess over stdio:   python -m app.mcp_server.server
(you can also run it by hand and inspect it with:  npx @modelcontextprotocol/inspector python -m app.mcp_server.server)

Only *reporting* lives here on purpose. Receipt extraction, confirmation and
receipt-image retrieval are normal Python in the LangGraph workflow.

IMPORTANT: stdout is the MCP protocol channel. Never print() in this process;
logging is configured to stderr (see app/utils/logging.py).
"""
import json
from typing import Optional

from mcp.server.fastmcp import FastMCP

from app.reporting import tools as t
from app.utils.logging import logger

mcp = FastMCP("expense-reporting")


def _json(result) -> str:
    # default=str makes dates/Decimals safe; callers parse this JSON string
    return json.dumps(result, default=str)


@mcp.tool()
def get_expense_summary(user_id: Optional[int] = None, start_date: Optional[str] = None,
                        end_date: Optional[str] = None, category: Optional[str] = None,
                        period_label: Optional[str] = None) -> str:
    """Total, count and average of confirmed expenses. Dates are YYYY-MM-DD (inclusive).
    user_id omitted = whole organization."""
    return _json(t.get_expense_summary(user_id, start_date, end_date, category, period_label))


@mcp.tool()
def get_category_breakdown(user_id: Optional[int] = None, start_date: Optional[str] = None,
                           end_date: Optional[str] = None, category: Optional[str] = None,
                           period_label: Optional[str] = None) -> str:
    """Spending per category (largest first) with percentage share."""
    return _json(t.get_category_breakdown(user_id, start_date, end_date, category, period_label))


@mcp.tool()
def get_monthly_expense_trend(user_id: Optional[int] = None, start_date: Optional[str] = None,
                              end_date: Optional[str] = None, category: Optional[str] = None,
                              period_label: Optional[str] = None) -> str:
    """Total spending per month over a date range (empty months are 0)."""
    return _json(t.get_monthly_expense_trend(user_id, start_date, end_date, category, period_label))


@mcp.tool()
def get_yearly_expense_summary(user_id: Optional[int] = None, year: Optional[int] = None,
                               category: Optional[str] = None) -> str:
    """One year: total, per-month totals and per-category totals."""
    return _json(t.get_yearly_expense_summary(user_id, year, category))


@mcp.tool()
def list_expenses(user_id: Optional[int] = None, start_date: Optional[str] = None,
                  end_date: Optional[str] = None, category: Optional[str] = None,
                  period_label: Optional[str] = None, limit: int = 25) -> str:
    """The individual confirmed expenses (newest first)."""
    return _json(t.list_expenses(user_id, start_date, end_date, category, period_label, limit))


@mcp.tool()
def generate_bar_chart(user_id: Optional[int] = None, group_by: str = "category",
                       start_date: Optional[str] = None, end_date: Optional[str] = None,
                       category: Optional[str] = None, top_n: int = 10,
                       period_label: Optional[str] = None) -> str:
    """Bar chart PNG grouped by 'category', 'month' or 'vendor'. Returns chart_path."""
    return _json(t.generate_bar_chart(user_id, group_by, start_date, end_date, category, top_n, period_label))


@mcp.tool()
def generate_category_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            style: str = "bar", top_n: Optional[int] = None,
                            period_label: Optional[str] = None) -> str:
    """Category chart PNG. style: 'bar', 'pie' (distribution) or 'top' (top N). Returns chart_path."""
    return _json(t.generate_category_chart(user_id, start_date, end_date, category, style, top_n, period_label))


@mcp.tool()
def generate_monthly_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                           end_date: Optional[str] = None, category: Optional[str] = None,
                           period_label: Optional[str] = None) -> str:
    """Monthly totals bar chart PNG (use a full-year range for a yearly trend). Returns chart_path."""
    return _json(t.generate_monthly_chart(user_id, start_date, end_date, category, period_label))


@mcp.tool()
def generate_timeline_chart(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            granularity: str = "auto", cumulative: bool = False,
                            period_label: Optional[str] = None) -> str:
    """Line graph PNG of expense amount over time. granularity: auto, day, week or month;
    cumulative=true plots the running total. Returns chart_path."""
    return _json(t.generate_timeline_chart(user_id, start_date, end_date, category, granularity,
                                           cumulative, period_label))


@mcp.tool()
def generate_expense_report(user_id: Optional[int] = None, start_date: Optional[str] = None,
                            end_date: Optional[str] = None, category: Optional[str] = None,
                            period_label: Optional[str] = None) -> str:
    """Excel report (.xlsx) with Summary, Expenses, Category Summary and Monthly Summary sheets.
    Returns file_path."""
    return _json(t.generate_expense_report(user_id, start_date, end_date, category, period_label))


if __name__ == "__main__":
    logger.info("Starting MCP reporting server (stdio)")
    mcp.run(transport="stdio")
