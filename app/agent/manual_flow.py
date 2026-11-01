"""The /add wizard: add an expense without a receipt.

    /add  ->  "send the amount"  ->  category buttons  ->  "send a description (or skip)"  ->  saved

Each Telegram message is a separate request, so the progress lives in the
`manual_drafts` table (see ExpenseService.begin_manual & co). This module is the
state machine only: it receives an *event* and returns a partial AgentState.

events:  {"kind": "start"}
         {"kind": "text", "text": "..."}
         {"kind": "category", "value": "<index into ALL_CATEGORIES>"}   (button press)
         {"kind": "cancel"}
"""
from typing import Any, Dict, List, Optional

from app.schemas.enums import ALL_CATEGORIES, normalize_category
from app.utils.formatting import format_date, format_money
from app.utils.parsing import clean_text, parse_amount

CANCEL_WORDS = ("cancel", "/cancel", "stop", "exit")
MAX_AMOUNT = 1_000_000_000


def _default_service():
    from app.services.expense_service import expense_service  # lazy: keeps this module light to import
    return expense_service


def category_buttons() -> List[List[tuple]]:
    """Two buttons per row; callback data 'ma:<index>' (the category list may change, indexes are resolved at press time)."""
    buttons = [(c.value, f"ma:{i}") for i, c in enumerate(ALL_CATEGORIES)]
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


def _ask_category(amount: float) -> Dict[str, Any]:
    return {"response_text": f"Amount: {format_money(amount)}\nNow choose a category (or type its name):",
            "buttons": category_buttons()}


def _ask_description(category: str) -> Dict[str, Any]:
    return {"response_text": f"Category: {category}\nSend a short description, or type 'skip'.", "buttons": None}


def handle_manual(user_id: int, event: Dict[str, Any], svc: Optional[Any] = None) -> Dict[str, Any]:
    svc = svc or _default_service()
    kind = event.get("kind")

    if kind == "start":
        svc.begin_manual(user_id)
        return {"response_text": "✍️ Adding an expense manually.\nSend the amount (e.g. 500). Type 'cancel' to stop."}

    if kind == "cancel":
        svc.clear_manual(user_id)
        return {"response_text": "Okay, cancelled."}

    draft = svc.get_manual(user_id)
    if draft is None:
        return {"toast": "That entry expired. Send /add to start again.",
                "response_text": "" if kind == "category" else "No manual entry in progress. Send /add to start one."}
    step, amount, _category = draft

    # ---------------------------------------------------------------- category via button
    if kind == "category":
        if step != "category":
            return {"toast": "That button is no longer active.", "response_text": ""}
        try:
            category = ALL_CATEGORIES[int(event.get("value"))].value
        except (ValueError, TypeError, IndexError):
            return {"toast": "Invalid choice.", "response_text": ""}
        svc.set_manual_category(user_id, category)
        return {**_ask_description(category), "edit_message": True, "toast": category}

    # ---------------------------------------------------------------- typed text
    text = (event.get("text") or "").strip()
    if text.lower() in CANCEL_WORDS:
        svc.clear_manual(user_id)
        return {"response_text": "Okay, cancelled."}

    if step == "amount":
        value = parse_amount(text)
        if value is None or value <= 0 or value > MAX_AMOUNT:
            return {"response_text": "❌ Please send the amount as a positive number, e.g. 500 or 1,250.50"}
        svc.set_manual_amount(user_id, value)
        return _ask_category(value)

    if step == "category":
        category = normalize_category(text)
        if category is None:
            return {"response_text": "❌ I don't know that category. Tap one of the buttons or type its exact name.",
                    "buttons": category_buttons()}
        svc.set_manual_category(user_id, category.value)
        return _ask_description(category.value)

    if step == "description":
        description = None if text.lower() in ("skip", "-", "none") else clean_text(text, 500)
        expense = svc.finish_manual(user_id, description)
        if expense is None:
            return {"error": "I couldn't save the expense. Please send /add and try again."}
        lines = ["✅ Expense added successfully!", "",
                 f"Amount: {format_money(expense.expense_amount, expense.currency)}",
                 f"Category: {expense.category}",
                 f"Date: {format_date(expense.expense_date)}",
                 f"Description: {expense.description or '—'}"]
        return {"response_text": "\n".join(lines), "buttons": None}

    svc.clear_manual(user_id)  # unknown step: never leave the user stuck
    return {"response_text": "Something went wrong with that entry. Send /add to start again."}
