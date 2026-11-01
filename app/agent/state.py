from typing import Any, Dict, List, Optional, Tuple, TypedDict

Button = Tuple[str, str]  # (label, callback_data)


class AgentState(TypedDict, total=False):
    """Shared state for the LangGraph workflow (nodes return partial updates)."""

    # ---- input (set by the Telegram layer) ----
    user_id: int
    input_type: str                    # receipt | csv | feedback | text | manual_add
    message_text: Optional[str]
    receipt_ref: Optional[str]         # storage reference of the uploaded receipt image
    csv_file_path: Optional[str]
    feedback: Optional[Dict[str, Any]]  # {"action": "confirm", "expense_id": 7, ...}

    # ---- manual entry (/add) ----
    manual_event: Optional[Dict[str, Any]]   # {"kind": "start" | "text" | "category" | "cancel", ...}

    # ---- receipt workflow ----
    extraction: Any                    # ReceiptExtraction
    categorization: Any                # CategorizationResult | None
    extraction_meta: Dict[str, Any]
    expense_id: Optional[int]

    # ---- report workflow ----
    report_request: Any                # ReportRequest
    parsed_by: str                     # llm | rules
    tool_calls: List[Any]

    # ---- output (consumed by the Telegram layer) ----
    response_text: str
    buttons: Optional[List[List[Button]]]
    edit_message: bool                 # edit the message the button was on, instead of sending a new one
    toast: Optional[str]               # small popup for button presses
    chart_paths: List[str]
    excel_file_path: Optional[str]
    receipt_images: List[Dict[str, str]]  # [{"path": ..., "caption": ...}]
    error: Optional[str]
