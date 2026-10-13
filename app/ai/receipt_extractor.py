"""The three LLM-powered steps. Each asks for JSON, validates it with Pydantic,
and never lets the model decide numbers that Python or SQL should compute.

    ReceiptExtractor  - vision model reads the receipt          (image -> fields)
    ExpenseCategorizer- text model picks category/item/type     (fields -> label)
    ReportParser      - text model understands a user request   (text -> ReportRequest)
"""
import asyncio
from datetime import date
from typing import Tuple

from app.ai.llm_provider import LLMCallInfo, LLMUnavailableError, llm
from app.ai.llm_utils import BadModelOutput, prepare_image_for_llm
from app.ai.report_rules import rule_based_request
from app.config.settings import settings
from app.schemas.enums import ALL_CATEGORIES, CATEGORY_DESCRIPTIONS, ReportIntent
from app.schemas.expense import CategorizationResult, ReceiptExtraction, ReportRequest
from app.utils.formatting import format_money
from app.utils.logging import logger
from app.utils.tracing import set_trace_metadata, traced

EXTRACTION_SYSTEM = """You read receipts and invoices from photos and return structured data.
Rules:
- Reply with ONE JSON object and nothing else (no markdown, no explanation).
- Use null for anything you cannot clearly read. NEVER guess or invent values.
- expense_date: YYYY-MM-DD. Indian receipts are day-first (08/10/2026 means 8 October 2026).
- expense_amount: the final total paid (grand total including tax) as a plain number.
- subtotal and tax: plain numbers if printed, otherwise null.
- currency: 3-letter code (a rupee sign or Rs means INR) only if shown on the receipt, otherwise null.
- vendor: the shop/company name as printed. invoice_number: the bill/invoice/receipt number if printed.
- description: short summary of what was bought (max 25 words).
JSON template:
{"vendor": null, "invoice_number": null, "expense_date": null, "expense_amount": null, "currency": null, "subtotal": null, "tax": null, "description": null}"""


def _reject_empty(result: ReceiptExtraction) -> None:
    if result.is_empty():
        raise BadModelOutput("model returned an empty extraction")


class ReceiptExtractor:
    @traced("receipt_extraction", run_type="chain")
    async def extract(self, image_bytes: bytes) -> Tuple[ReceiptExtraction, LLMCallInfo]:
        # Shrink first: big phone photos are the main cause of token/size errors.
        jpeg = await asyncio.to_thread(
            prepare_image_for_llm, image_bytes, settings.IMAGE_MAX_SIDE_PX, settings.IMAGE_JPEG_QUALITY)
        result, info = await llm.ainvoke_json(
            task="receipt_extraction", system=EXTRACTION_SYSTEM,
            user_text="Extract the receipt details as JSON.",
            schema=ReceiptExtraction, image_jpeg=jpeg, accept=_reject_empty,
        )
        set_trace_metadata(extraction_success=True, image_kb=len(jpeg) // 1024,
                           fields_found=[k for k, v in result.model_dump().items() if v is not None])
        return result, info


def _categorizer_system() -> str:
    lines = "\n".join(f"- {c.value}: {CATEGORY_DESCRIPTIONS[c]}" for c in ALL_CATEGORIES)
    return (
        "You are an accountant for a small organization. Classify ONE expense.\n"
        "Allowed categories (use the exact name):\n" + lines + "\n\n"
        "Reply with ONE JSON object and nothing else:\n"
        '{"category": "<one allowed name>", "item": "<2-5 word name of what was bought>", '
        '"goods_or_service": "Goods" or "Service" or null}\n'
        "- item: what was purchased, e.g. Team Dinner, Hotel Room, Printer Paper - not the shop name.\n"
        "- goods_or_service: Goods = physical products; Service = meals, stays, travel, subscriptions, "
        "repairs, fees. Use null if unclear.\n"
        "- If nothing fits, use Miscellaneous. Do not invent details."
    )


class ExpenseCategorizer:
    def __init__(self):
        self.system = _categorizer_system()

    @traced("expense_categorization", run_type="chain")
    async def categorize(self, data: ReceiptExtraction) -> Tuple[CategorizationResult, LLMCallInfo]:
        text = (
            f"Vendor: {data.vendor or 'unknown'}\n"
            f"Items: {data.description or 'unknown'}\n"
            f"Amount: {format_money(data.expense_amount, data.currency) if data.expense_amount else 'unknown'}"
        )
        result, info = await llm.ainvoke_json(
            task="categorization", system=self.system, user_text=text, schema=CategorizationResult)
        set_trace_metadata(category_predicted=result.category.value,
                           goods_or_service=result.goods_or_service.value if result.goods_or_service else None)
        return result, info


def _parser_system(today: date) -> str:
    cats = ", ".join(c.value for c in ALL_CATEGORIES)
    return f"""You convert a user's message about their expenses into a JSON request. Today is {today.isoformat()}.
Reply with ONE JSON object and nothing else. Never calculate totals or dates yourself.

intent (pick one):
- summary: total spent / number of expenses
- category_breakdown: spending split by category
- monthly_trend: spending per month over several months
- yearly_summary: a whole year overview
- chart: the user explicitly asks for a graph/chart/plot
- excel_report: the user wants an Excel/spreadsheet report or download
- expense_list: list the individual expenses
- receipt_lookup: the user wants to SEE the stored receipt image(s)
- unknown: not about expenses

chart_type (only for intent chart, else null): category_bar, category_pie, top_categories, monthly_bar, yearly_trend, vendor_bar, timeline
(timeline = line graph of expenses over time / expense vs time / daily or weekly spending)

period.kind: this_month, last_month, this_year, last_year, month (set month 1-12, and year only if said), year (set year), last_n_months (set months), all
category: one of [{cats}] or null (only when the user names a category)
search_text: vendor/item words for receipt_lookup (e.g. "restaurant", "hotel"), else null
amount: exact amount for receipt_lookup, plain number, else null
top_n: number if the user says "top N", else null
granularity: for timeline only - "day", "week" or "month" if the user says daily/weekly/monthly, else null
cumulative: true only if the user asks for a cumulative / running total timeline, else false

Template:
{{"intent": "unknown", "chart_type": null, "period": {{"kind": "this_month", "year": null, "month": null, "months": null}}, "category": null, "search_text": null, "amount": null, "top_n": null, "granularity": null, "cumulative": false}}

Examples:
"expenses by category for September" -> {{"intent": "category_breakdown", "chart_type": null, "period": {{"kind": "month", "month": 9}}, "category": null, "search_text": null, "amount": null, "top_n": null}}
"Excel report for Travel in September" -> {{"intent": "excel_report", "chart_type": null, "period": {{"kind": "month", "month": 9}}, "category": "Travel", "search_text": null, "amount": null, "top_n": null}}
"graph of daily expenses this month" -> {{"intent": "chart", "chart_type": "timeline", "period": {{"kind": "this_month"}}, "category": null, "search_text": null, "amount": null, "top_n": null, "granularity": "day", "cumulative": false}}
"show the receipt for the 2,450 restaurant expense" -> {{"intent": "receipt_lookup", "chart_type": null, "period": {{"kind": "all"}}, "category": null, "search_text": "restaurant", "amount": 2450, "top_n": null}}"""


class ReportParser:
    @traced("report_request_parsing", run_type="chain")
    async def parse(self, text: str, today: date) -> Tuple[ReportRequest, str]:
        """Returns (request, how) where how is 'llm' or 'rules'."""
        try:
            request, _info = await llm.ainvoke_json(
                task="report_parsing", system=_parser_system(today), user_text=text, schema=ReportRequest)
            if request.intent != ReportIntent.UNKNOWN:
                set_trace_metadata(parsed_by="llm", intent=request.intent.value)
                return request, "llm"
        except LLMUnavailableError as exc:
            logger.warning("LLM unavailable for report parsing, using rules: %s", exc)

        request = ReportRequest(**rule_based_request(text, today))
        set_trace_metadata(parsed_by="rules", intent=request.intent.value)
        return request, "rules"
