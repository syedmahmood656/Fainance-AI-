"""Pydantic schemas.

Three groups:
1. LLM output schemas  - lenient: bad/missing values become None, never an error
                         and never an invented value.
2. Report request      - what the LLM understood from a user's text message.
3. API/DB view schemas - strict, used when reading stored expenses.
"""
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.enums import (
    ChartKind,
    ExpenseCategory,
    GoodsOrService,
    PeriodKind,
    ReportIntent,
    normalize_category,
    normalize_goods_or_service,
)
from app.utils.parsing import (
    clean_text,
    normalize_currency,
    parse_amount,
    parse_date_flexible,
)

# Re-exported so other modules can keep importing from one place.
__all__ = [
    "ExpenseCategory", "GoodsOrService", "ReportIntent", "ChartKind", "PeriodKind",
    "ReceiptExtraction", "CategorizationResult", "Period", "ReportRequest", "ExpenseOut",
]


# ---------------------------------------------------------------------------
# 1. LLM output
# ---------------------------------------------------------------------------
class ReceiptExtraction(BaseModel):
    """What the vision model read from the receipt. Every field may be None:
    "unknown" is a valid answer, an invented value is not."""

    vendor: Optional[str] = Field(None, description="Name of the vendor/store as printed")
    invoice_number: Optional[str] = Field(None, description="Invoice / receipt / bill number")
    expense_date: Optional[date] = Field(None, description="Date on the receipt")
    expense_amount: Optional[float] = Field(None, description="Final total paid, including tax")
    currency: Optional[str] = Field(None, description="3-letter currency code")
    subtotal: Optional[float] = Field(None, description="Amount before tax")
    tax: Optional[float] = Field(None, description="Total tax amount")
    description: Optional[str] = Field(None, description="Short summary of the items purchased")

    @field_validator("vendor", "invoice_number", mode="before")
    @classmethod
    def _text(cls, v):
        return clean_text(v, 255)

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v):
        return clean_text(v, 500)

    @field_validator("expense_amount", "subtotal", "tax", mode="before")
    @classmethod
    def _amount(cls, v):
        return parse_amount(v)

    @field_validator("expense_date", mode="before")
    @classmethod
    def _date(cls, v):
        return parse_date_flexible(v)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v):
        return normalize_currency(v)

    def is_empty(self) -> bool:
        return not any([self.vendor, self.expense_amount, self.expense_date, self.description])


class CategorizationResult(BaseModel):
    category: ExpenseCategory
    item: Optional[str] = Field(None, description="2-5 word name of what was bought, e.g. 'Team Dinner'")
    goods_or_service: Optional[GoodsOrService] = None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        cat = normalize_category(v)
        if cat is None:
            raise ValueError(f"unknown category: {v!r}")  # -> LLM layer tries the next model
        return cat

    @field_validator("item", mode="before")
    @classmethod
    def _item(cls, v):
        return clean_text(v, 120)

    @field_validator("goods_or_service", mode="before")
    @classmethod
    def _gos(cls, v):
        return normalize_goods_or_service(v)


# ---------------------------------------------------------------------------
# 2. Report request (LLM interprets, Python executes)
# ---------------------------------------------------------------------------
class Period(BaseModel):
    kind: PeriodKind = PeriodKind.ALL
    year: Optional[int] = Field(None, ge=2000, le=2100)
    month: Optional[int] = Field(None, ge=1, le=12)
    months: Optional[int] = Field(None, ge=1, le=36)
    start_date: Optional[date] = None
    end_date: Optional[date] = None

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v):
        try:
            return PeriodKind(str(v).strip().lower())
        except ValueError:
                return PeriodKind.ALL
    @field_validator("year", "month", "months", mode="before")
    @classmethod
    def _ints(cls, v):
        if v in (None, "", "null"):
            return None
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _dates(cls, v):
        return parse_date_flexible(v)


class ReportRequest(BaseModel):
    intent: ReportIntent = ReportIntent.UNKNOWN
    chart_type: Optional[ChartKind] = None
    period: Period = Field(default_factory=Period)
    category: Optional[ExpenseCategory] = None
    search_text: Optional[str] = Field(None, description="Vendor/item words, for receipt lookups")
    amount: Optional[float] = Field(None, description="Exact amount, for receipt lookups")
    top_n: Optional[int] = Field(None, ge=1, le=12)
    granularity: Optional[str] = Field(None, description="timeline bucket: auto, day, week or month")
    cumulative: bool = Field(False, description="timeline as a running total")

    @field_validator("granularity", mode="before")
    @classmethod
    def _granularity(cls, v):
        text = str(v).strip().lower() if v not in (None, "") else ""
        return text if text in ("auto", "day", "week", "month") else None

    @field_validator("cumulative", mode="before")
    @classmethod
    def _cumulative(cls, v):
        if isinstance(v, str):
            return v.strip().lower() in ("true", "yes", "1", "cumulative")
        return bool(v)

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        try:
            return ReportIntent(str(v).strip().lower())
        except ValueError:
            return ReportIntent.UNKNOWN

    @field_validator("chart_type", mode="before")
    @classmethod
    def _chart(cls, v):
        if v in (None, "", "null"):
            return None
        try:
            return ChartKind(str(v).strip().lower())
        except ValueError:
            return None

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        return normalize_category(v)

    @field_validator("search_text", mode="before")
    @classmethod
    def _search(cls, v):
        return clean_text(v, 80)

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, v):
        return parse_amount(v)

    @field_validator("top_n", mode="before")
    @classmethod
    def _top_n(cls, v):
        if v in (None, "", "null"):
            return None
        try:
            return max(1, min(int(float(v)), 12))
        except (TypeError, ValueError):
            return None

    @field_validator("period", mode="before")
    @classmethod
    def _period(cls, v):
        return v if isinstance(v, (dict, Period)) else {}


# ---------------------------------------------------------------------------
# 3. Stored expense (read model)
# ---------------------------------------------------------------------------
class ExpenseOut(BaseModel):
    """Everything we store about one expense, with AI vs final values side by side."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_user_id: int
    status: str

    # final values (what reports use)
    vendor: Optional[str] = None
    invoice_number: Optional[str] = None
    expense_date: Optional[date] = None
    category: Optional[str] = None
    item: Optional[str] = None
    goods_or_service: Optional[str] = None
    expense_amount: Optional[float] = None
    currency: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    description: Optional[str] = None
    receipt_file_reference: Optional[str] = None

    # what the AI originally said
    vendor_ai: Optional[str] = None
    expense_date_ai: Optional[date] = None
    category_ai: Optional[str] = None
    item_ai: Optional[str] = None
    goods_or_service_ai: Optional[str] = None
    expense_amount_ai: Optional[float] = None
    ai_extraction: Optional[Dict[str, Any]] = None
    final_result: Optional[Dict[str, Any]] = None

    # feedback
    corrected_by_user: bool = False
    corrected_fields: Optional[List[str]] = None

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    confirmed_at: Optional[datetime] = None
