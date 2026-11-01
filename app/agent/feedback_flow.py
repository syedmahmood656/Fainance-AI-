"""Human-in-the-loop: confirm / correct / discard.

Telegram button presses and typed corrections arrive as separate webhook calls,
so the pending expense lives in PostgreSQL (status='pending') between them. That
is our "checkpoint" - simpler and more reliable than keeping graph state in memory.
"""
from datetime import date
from typing import Any, Dict, List

from app.schemas.enums import ALL_CATEGORIES, CORRECTABLE_FIELDS, GoodsOrService
from app.services.expense_service import expense_service
from app.services.validation import card_warnings
from app.utils.formatting import format_expense_card
from app.utils.tracing import set_trace_metadata

PROMPTS = {
    "vendor": "Send the correct vendor name.",
    "expense_date": "Send the correct date, day first (e.g. 08/10/2026) or like 2026-10-08.",
    "item": "Send the item name (e.g. Team Dinner).",
    "expense_amount": "Send the correct amount as a number (e.g. 2450).",
}


def _rows(buttons: List[Any], per_row: int = 2) -> List[List[Any]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def main_buttons(eid: int):
    return [[("✅ Confirm", f"cf:{eid}"), ("✏️ Correct", f"co:{eid}")]]


def render_card(expense, title: str = "🧾 Receipt processed"):
    text = format_expense_card(expense, card_warnings(expense, date.today(), expense.extraction_meta), title)
    return text, main_buttons(expense.id)


def _correct_menu(eid: int):
    fields = [(label, f"cp:{eid}:{key}") for key, label in CORRECTABLE_FIELDS]
    rows = _rows(fields)
    rows.append([("🗑 Discard", f"cd:{eid}"), ("⬅️ Back", f"cb:{eid}")])
    return rows


def _category_menu(eid: int):
    rows = _rows([(c.value, f"cc:{eid}:{i}") for i, c in enumerate(ALL_CATEGORIES)])
    rows.append([("⬅️ Back", f"cb:{eid}")])
    return rows


def _type_menu(eid: int):
    return [[("Goods", f"ct:{eid}:G"), ("Service", f"ct:{eid}:S")], [("⬅️ Back", f"cb:{eid}")]]


def handle_feedback(user_id: int, fb: Dict[str, Any]) -> Dict[str, Any]:
    """Returns a partial AgentState update."""
    action = fb.get("action")
    set_trace_metadata(feedback_action=action)

    # ---- typed value for a field the user chose earlier ----
    if action == "text":
        edit = expense_service.get_edit(user_id)
        if not edit:
            return {"response_text": "Nothing to edit right now. Send a receipt photo or ask for a report."}
        eid, field = edit
        try:
            updated = expense_service.apply_correction(eid, user_id, field, fb.get("text", ""))
        except ValueError as exc:
            return {"response_text": f"⚠️ {exc}\n\nTry again, or type 'cancel'."}
        expense_service.clear_edit(user_id)
        if updated is None:
            return {"response_text": "That expense can no longer be edited."}
        set_trace_metadata(human_correction=True, corrected_field=field)
        text, buttons = render_card(updated, title="✏️ Updated — please review")
        return {"response_text": text, "buttons": buttons, "edit_message": False}

    if action == "cancel_edit":
        expense_service.clear_edit(user_id)
        expense_service.clear_manual(user_id)
        return {"response_text": "Okay, cancelled."}

    # ---- button presses (always tied to one expense) ----
    eid = fb.get("expense_id")
    expense = expense_service.get(eid, user_id) if eid else None
    if expense is None:
        return {"toast": "I can't find that expense.", "response_text": ""}
    if expense.status != "pending" and action != "confirm":
        return {"toast": "That expense was already handled.", "response_text": ""}

    if action == "confirm":
        result = expense_service.confirm(eid, user_id)
        if result.status == "ok":
            e = result.expense
            set_trace_metadata(human_confirmed=True, human_correction=bool(e.corrected_by_user),
                               category_final=e.category, category_ai=e.category_ai,
                               corrected_fields=e.corrected_fields or [])
            text = format_expense_card(e, None, title="✅ Expense saved successfully.")
            return {"response_text": text, "buttons": None, "edit_message": True, "toast": "Saved"}
        if result.status == "missing_fields":
            text, buttons = render_card(result.expense)
            return {"response_text": text, "buttons": buttons, "edit_message": True,
                    "toast": "Missing: " + ", ".join(result.missing)}
        return {"toast": "That expense was already handled.", "response_text": ""}

    if action == "correct":
        text, _ = render_card(expense)
        return {"response_text": text + "\n\nWhich field should I correct?", "buttons": _correct_menu(eid),
                "edit_message": True}

    if action == "field":
        field = fb.get("value")
        if field == "category":
            return {"response_text": "Choose the correct category:", "buttons": _category_menu(eid),
                    "edit_message": True}
        if field == "goods_or_service":
            return {"response_text": "Is this Goods or a Service?", "buttons": _type_menu(eid), "edit_message": True}
        if field in PROMPTS:
            expense_service.begin_edit(user_id, eid, field)
            return {"response_text": PROMPTS[field] + "\n(Type 'cancel' to go back.)",
                    "buttons": [[("⬅️ Back", f"cb:{eid}")]], "edit_message": True}
        return {"toast": "Unknown field."}

    if action in ("set_category", "set_type"):
        try:
            if action == "set_category":
                value = ALL_CATEGORIES[int(fb["value"])].value
                field = "category"
            else:
                value = GoodsOrService.GOODS.value if fb["value"] == "G" else GoodsOrService.SERVICE.value
                field = "goods_or_service"
            updated = expense_service.apply_correction(eid, user_id, field, value)
        except (ValueError, IndexError, KeyError):
            return {"toast": "Invalid choice."}
        if updated is None:
            return {"toast": "That expense was already handled.", "response_text": ""}
        set_trace_metadata(human_correction=True, corrected_field=field)
        text, buttons = render_card(updated, title="✏️ Updated — please review")
        return {"response_text": text, "buttons": buttons, "edit_message": True, "toast": "Updated"}

    if action == "back":
        expense_service.clear_edit(user_id)
        text, buttons = render_card(expense)
        return {"response_text": text, "buttons": buttons, "edit_message": True}

    if action == "discard":
        expense_service.discard(eid, user_id)
        set_trace_metadata(human_discarded=True)
        return {"response_text": "🗑 Receipt discarded. Send it again if you want to retry.", "buttons": None,
                "edit_message": True, "toast": "Discarded"}

    return {"toast": "Unknown action."}
