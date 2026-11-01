"""The LangGraph workflow.

START ──route──┬─ receipt  → extract → validate → categorize → save_pending (shows Confirm/Correct buttons)
               ├─ feedback → handle_feedback (confirm / correct / discard / typed value)
               ├─ csv      → process_csv
               └─ text     → parse_report ─┬─ receipt_lookup → lookup_receipts   (PostgreSQL + storage)
                                           ├─ unknown        → help_reply
                                           └─ reports        → run_report        (MCP tools → PostgreSQL)

Routing is deterministic. The LLM is only used inside extract, categorize and parse_report.
"""
import hashlib
import time
from datetime import date
from typing import Any, Dict

from langgraph.graph import END, START, StateGraph

from app.agent.feedback_flow import handle_feedback, render_card
from app.agent.manual_flow import handle_manual
from app.agent.reporting_flow import build_reply, plan_tool_calls
from app.agent.state import AgentState
from app.ai.llm_provider import LLMUnavailableError
from app.ai.receipt_extractor import ExpenseCategorizer, ReceiptExtractor, ReportParser
from app.config.settings import settings
from app.database.models import SessionLocal
from app.database.repository import ExpenseFilter, ExpenseRepository
from app.mcp_server.client import ReportingToolError, call_reporting_tools
from app.reporting.periods import resolve_period
from app.schemas.enums import ReportIntent
from app.schemas.expense import ReceiptExtraction
from app.services.expense_service import expense_service
from app.services.storage_service import storage_service
from app.services.validation import sanitize_extraction
from app.utils.formatting import format_date, format_money
from app.utils.logging import logger
from app.utils.tracing import current_run_id, hash_user_id, set_trace_metadata, traced

extractor = ReceiptExtractor()
categorizer = ExpenseCategorizer()
parser = ReportParser()

MAX_RECEIPTS_SENT = 10

HELP_TEXT = (
    "I can help with:\n"
    "📸 Send a receipt photo — I'll read it and ask you to confirm.\n"
    "📊 \"Show me September expenses by category\"\n"
    "📈 \"Show me a bar graph of this month's expenses\" · \"Last 6 months of expenses\"\n"
    "📁 \"Download the September Excel report\"\n"
    "🧾 \"Show me the receipt for the ₹2,450 restaurant expense\" · \"Show me Travel receipts\"\n"
    "✍️ /add — add an expense without a receipt (amount → category → description)\n"
    "📤 Send a CSV (date, vendor, amount, currency, category, description) to import expenses."
)


def scope_user_id(user_id: int):
    """None = organization-wide reports; otherwise only the user's own expenses."""
    return None if settings.REPORTS_SCOPE.lower() == "organization" else user_id


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
def route_input(state: AgentState) -> str:
    kind = state.get("input_type")
    if kind == "receipt":
        return "extract_receipt"
    if kind == "csv":
        return "process_csv"
    if kind == "feedback":
        return "handle_feedback"
    if kind == "manual_add":
        return "manual_add"
    # Plain text: if we are waiting for a typed answer, it is NOT a report request.
    if expense_service.get_edit(state["user_id"]):
        return "text_feedback"
    if expense_service.get_manual(state["user_id"]):
        return "manual_add"
    return "parse_report"


def after_extract(state: AgentState) -> str:
    return END if state.get("error") else "validate_receipt"


def after_parse(state: AgentState) -> str:
    if state.get("error"):
        return END
    intent = state["report_request"].intent
    if intent == ReportIntent.RECEIPT_LOOKUP:
        return "lookup_receipts"
    if intent == ReportIntent.UNKNOWN:
        return "help_reply"
    return "run_report"


# ---------------------------------------------------------------------------
# Receipt workflow
# ---------------------------------------------------------------------------
async def extract_receipt_node(state: AgentState) -> Dict[str, Any]:
    ref = state.get("receipt_ref")
    image = await storage_service.get_file(ref) if ref else None
    if not image:
        return {"error": "I couldn't read the uploaded image. Please send it again."}
    started = time.perf_counter()
    image_hash = hashlib.sha256(image).hexdigest()   # byte-identical re-uploads are flagged as duplicates
    try:
        extraction, info = await extractor.extract(image)
        set_trace_metadata(extraction_success=True, latency_ms=int((time.perf_counter() - started) * 1000))
        return {"extraction": extraction,
                "extraction_meta": {"extraction_llm": info.as_dict(), "receipt_hash": image_hash}}
    except ValueError as exc:  # not an image Pillow can open
        logger.warning("Unreadable image: %s", exc)
        await storage_service.delete_file(ref)
        return {"error": "That file doesn't look like a readable image. Please send a clear photo of the receipt."}
    except LLMUnavailableError as exc:
        # Still create the pending expense so the user can type the details in manually.
        logger.warning("Extraction failed, falling back to manual entry: %s", exc)
        set_trace_metadata(extraction_success=False, error=str(exc)[:150])
        return {"extraction": ReceiptExtraction(),
                "extraction_meta": {"extraction_failed": True, "failure_reason": str(exc),
                                    "receipt_hash": image_hash}}


async def validate_receipt_node(state: AgentState) -> Dict[str, Any]:
    meta = dict(state.get("extraction_meta") or {})
    if meta.get("extraction_failed"):
        return {}
    cleaned, assumed = sanitize_extraction(state["extraction"].model_dump(), date.today())
    meta["assumed_fields"] = assumed
    return {"extraction": ReceiptExtraction(**cleaned), "extraction_meta": meta}


async def categorize_node(state: AgentState) -> Dict[str, Any]:
    meta = dict(state.get("extraction_meta") or {})
    extraction = state["extraction"]
    if meta.get("extraction_failed") or extraction.is_empty():
        return {"categorization": None}
    try:
        result, info = await categorizer.categorize(extraction)
        meta["categorization_llm"] = info.as_dict()
        return {"categorization": result, "extraction_meta": meta}
    except LLMUnavailableError as exc:
        logger.warning("Categorization failed: %s", exc)
        meta["categorization_failed"] = True
        return {"categorization": None, "extraction_meta": meta}


async def save_pending_node(state: AgentState) -> Dict[str, Any]:
    try:
        expense = expense_service.create_pending_receipt(
            user_id=state["user_id"],
            receipt_ref=state.get("receipt_ref"),
            extraction=state["extraction"],
            categorization=state.get("categorization"),
            meta=state.get("extraction_meta") or {},
            langsmith_run_id=current_run_id(),
            receipt_hash=(state.get("extraction_meta") or {}).get("receipt_hash"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not save pending expense: %s", exc, exc_info=True)
        return {"error": "I read the receipt but couldn't save it. Please try again."}
    cat = state.get("categorization")
    set_trace_metadata(category_predicted=cat.category.value if cat else None, expense_id=expense.id)
    text, buttons = render_card(expense)
    return {"expense_id": expense.id, "response_text": text, "buttons": buttons}


# ---------------------------------------------------------------------------
# Feedback workflow
# ---------------------------------------------------------------------------
async def handle_feedback_node(state: AgentState) -> Dict[str, Any]:
    return handle_feedback(state["user_id"], state.get("feedback") or {})


async def text_feedback_node(state: AgentState) -> Dict[str, Any]:
    text = (state.get("message_text") or "").strip()
    if text.lower() in ("cancel", "/cancel", "back"):
        return handle_feedback(state["user_id"], {"action": "cancel_edit"})
    return handle_feedback(state["user_id"], {"action": "text", "text": text})


# ---------------------------------------------------------------------------
# CSV workflow
# ---------------------------------------------------------------------------
async def process_csv_node(state: AgentState) -> Dict[str, Any]:
    try:
        result = expense_service.import_csv(state["user_id"], state["csv_file_path"])
    except Exception as exc:  # noqa: BLE001
        logger.error("CSV import failed: %s", exc, exc_info=True)
        return {"error": "I couldn't read that CSV file. Expected columns: date, vendor, amount [, currency, category, description]."}
    text = f"✅ Imported {result['imported']} expense(s) from CSV."
    if result["skipped"]:
        text += f"\n⚠️ Skipped {result['skipped']} row(s):\n" + "\n".join(f"• {r}" for r in result["reasons"])
    return {"response_text": text}


# ---------------------------------------------------------------------------
# Report workflow
# ---------------------------------------------------------------------------
async def parse_report_node(state: AgentState) -> Dict[str, Any]:
    try:
        request, how = await parser.parse(state.get("message_text") or "", date.today())
    except Exception as exc:  # noqa: BLE001
        logger.error("Report parsing failed: %s", exc, exc_info=True)
        return {"error": "I couldn't understand that request. Try: \"Show me September expenses by category\"."}
    set_trace_metadata(report_type=request.intent.value, parsed_by=how)
    return {"report_request": request, "parsed_by": how}


async def run_report_node(state: AgentState) -> Dict[str, Any]:
    request = state["report_request"]
    calls = plan_tool_calls(request, scope_user_id(state["user_id"]), date.today())
    if not calls:
        return {"response_text": HELP_TEXT}
    set_trace_metadata(report_type=request.intent.value, tools_selected=[name for name, _ in calls])
    try:
        results = await call_reporting_tools(calls)
    except ReportingToolError as exc:
        logger.error("Reporting failed: %s", exc)
        return {"error": "The reporting service had a problem. Please try again in a moment."}
    reply = build_reply(calls, results)
    return {"response_text": reply["text"], "chart_paths": reply["chart_paths"],
            "excel_file_path": reply["excel_path"], "tool_calls": [name for name, _ in calls]}


async def manual_add_node(state: AgentState) -> Dict[str, Any]:
    """/add wizard (amount -> category -> description). Logic lives in agent/manual_flow.py."""
    event = state.get("manual_event") or {"kind": "text", "text": state.get("message_text") or ""}
    return handle_manual(state["user_id"], event)


async def lookup_receipts_node(state: AgentState) -> Dict[str, Any]:
    """Receipt image retrieval: the LLM only supplied filters; PostgreSQL finds the rows."""
    req = state["report_request"]
    p = req.period
    start, end, label = resolve_period(p.kind, date.today(), p.year, p.month, p.months, p.start_date, p.end_date)
    f = ExpenseFilter(user_id=scope_user_id(state["user_id"]), start=start, end=end,
                      category=req.category.value if req.category else None,
                      search_text=req.search_text, amount=req.amount, only_with_receipt=True)

    with SessionLocal() as session:
        repo = ExpenseRepository(session)
        expenses = repo.list_expenses(f, limit=MAX_RECEIPTS_SENT + 1)
        relaxed = False
        if not expenses and req.search_text:           # "restaurant" may be in none of the text fields
            f.search_text, relaxed = None, True
            expenses = repo.list_expenses(f, limit=MAX_RECEIPTS_SENT + 1)
        if not expenses and req.amount is not None:    # amount typed slightly wrong? widen the period
            f.start = f.end = None
            relaxed = True
            expenses = repo.list_expenses(f, limit=MAX_RECEIPTS_SENT + 1)
        rows = [(e.vendor, e.expense_date, e.expense_amount, e.currency, e.category, e.receipt_file_reference)
                for e in expenses]

    if not rows:
        return {"response_text": "❌ I couldn't find any receipts matching your request."}

    more = len(rows) > MAX_RECEIPTS_SENT
    images, missing = [], 0
    for vendor, the_date, amount, currency, category, ref in rows[:MAX_RECEIPTS_SENT]:
        path = await storage_service.get_file_path(ref)
        if not path:
            missing += 1
            continue
        images.append({"path": path, "caption": f"{vendor or 'Unknown vendor'} · {format_date(the_date)} · "
                                                f"{format_money(amount, currency)} · {category or '—'}"})
    if not images:
        return {"response_text": "❌ I found the expense records, but the receipt images are missing from storage."}

    text = f"🧾 Found {len(images)} receipt(s) — {label}"
    if relaxed:
        text += "\n(No exact match for every filter, so I loosened them.)"
    if more:
        text += f"\nShowing the first {MAX_RECEIPTS_SENT}; narrow your request to see others."
    if missing:
        text += f"\n⚠️ {missing} image file(s) could not be found."
    set_trace_metadata(receipts_found=len(images))
    return {"response_text": text, "receipt_images": images}


async def help_reply_node(state: AgentState) -> Dict[str, Any]:
    return {"response_text": HELP_TEXT}


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------
def create_graph():
    wf = StateGraph(AgentState)
    wf.add_node("extract_receipt", extract_receipt_node)
    wf.add_node("validate_receipt", validate_receipt_node)
    wf.add_node("categorize_expense", categorize_node)
    wf.add_node("save_pending", save_pending_node)
    wf.add_node("handle_feedback", handle_feedback_node)
    wf.add_node("text_feedback", text_feedback_node)
    wf.add_node("process_csv", process_csv_node)
    wf.add_node("parse_report", parse_report_node)
    wf.add_node("run_report", run_report_node)
    wf.add_node("lookup_receipts", lookup_receipts_node)
    wf.add_node("help_reply", help_reply_node)
    wf.add_node("manual_add", manual_add_node)

    wf.add_conditional_edges(START, route_input, {
        "extract_receipt": "extract_receipt",
        "process_csv": "process_csv",
        "handle_feedback": "handle_feedback",
        "text_feedback": "text_feedback",
        "parse_report": "parse_report",
        "manual_add": "manual_add",
    })

    wf.add_conditional_edges("extract_receipt", after_extract, {"validate_receipt": "validate_receipt", END: END})
    wf.add_edge("validate_receipt", "categorize_expense")
    wf.add_edge("categorize_expense", "save_pending")
    wf.add_edge("save_pending", END)

    wf.add_edge("handle_feedback", END)
    wf.add_edge("text_feedback", END)
    wf.add_edge("process_csv", END)
    wf.add_edge("manual_add", END)

    wf.add_conditional_edges("parse_report", after_parse, {
        "lookup_receipts": "lookup_receipts", "help_reply": "help_reply", "run_report": "run_report", END: END,
    })
    wf.add_edge("run_report", END)
    wf.add_edge("lookup_receipts", END)
    wf.add_edge("help_reply", END)
    return wf.compile()


graph = create_graph()


@traced("telegram_request", run_type="chain")
async def run_agent(state: AgentState) -> AgentState:
    """Entry point used by the Telegram layer. One LangSmith trace per user request."""
    started = time.perf_counter()
    set_trace_metadata(request_type=state.get("input_type"), user=hash_user_id(state["user_id"]))
    try:
        final = await graph.ainvoke(state)
    except Exception as exc:  # noqa: BLE001
        set_trace_metadata(execution_error=type(exc).__name__,
                           latency_ms=int((time.perf_counter() - started) * 1000))
        raise
    set_trace_metadata(latency_ms=int((time.perf_counter() - started) * 1000), error=final.get("error"),
                       parsed_by=final.get("parsed_by"))
    return final
