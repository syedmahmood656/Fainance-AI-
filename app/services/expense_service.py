"""Business logic for the human-in-the-loop expense lifecycle.

    receipt -> create_pending -> (apply_correction)* -> confirm        (or discard)

Nothing here calls an LLM. Validation and bookkeeping are plain Python.
"""
import csv
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from app.database.models import Expense, SessionLocal
from app.database.repository import ExpenseRepository
from app.schemas.enums import (
    CORRECTABLE_FIELDS,
    ExpenseCategory,
    ExpenseStatus,
    normalize_category,
    normalize_goods_or_service,
)
from app.schemas.expense import CategorizationResult, ReceiptExtraction
from app.services.feedback import compute_corrections, final_snapshot
from app.config.settings import settings
from app.utils.logging import logger
from app.utils.parsing import clean_text, normalize_currency, parse_amount, parse_correction, parse_date_flexible
from app.utils.tracing import send_feedback

REQUIRED_TO_CONFIRM = [("expense_amount", "Amount"), ("expense_date", "Date"), ("category", "Category")]
_FIELD_LABELS = dict(CORRECTABLE_FIELDS)


def _money(value) -> Optional[Decimal]:
    if value is None:
        return None
    return Decimal(str(round(float(value), 2)))


@dataclass
class ConfirmResult:
    status: str                      # ok | not_found | not_pending | missing_fields
    expense: Optional[Expense] = None
    missing: List[str] = field(default_factory=list)   # human labels


class ExpenseService:
    # ------------------------------------------------------------------
    # Receipt -> pending row
    # ------------------------------------------------------------------
    def create_pending_receipt(
        self,
        user_id: int,
        receipt_ref: Optional[str],
        extraction: ReceiptExtraction,
        categorization: Optional[CategorizationResult],
        meta: Dict[str, Any],
        langsmith_run_id: Optional[str] = None,
    ) -> Expense:
        category = categorization.category.value if categorization else None
        item = categorization.item if categorization else None
        gos = categorization.goods_or_service.value if (categorization and categorization.goods_or_service) else None
        amount = _money(extraction.expense_amount)

        ai_blob = {
            "extraction": extraction.model_dump(mode="json"),
            "categorization": categorization.model_dump(mode="json") if categorization else None,
        }
        with SessionLocal() as session:
            return ExpenseRepository(session).create(
                telegram_user_id=user_id,
                status=ExpenseStatus.PENDING.value,
                source="receipt",
                # final values start equal to the AI values ...
                vendor=extraction.vendor,
                invoice_number=extraction.invoice_number,
                expense_date=extraction.expense_date,
                item=item,
                goods_or_service=gos,
                category=category,
                expense_amount=amount,
                currency=extraction.currency,
                subtotal=_money(extraction.subtotal),
                tax=_money(extraction.tax),
                description=extraction.description,
                receipt_file_reference=receipt_ref,
                # ... and the AI snapshot is frozen here, never edited afterwards
                vendor_ai=extraction.vendor,
                expense_date_ai=extraction.expense_date,
                category_ai=category,
                item_ai=item,
                goods_or_service_ai=gos,
                expense_amount_ai=amount,
                ai_extraction=ai_blob,
                extraction_meta=meta,
                langsmith_run_id=langsmith_run_id,
            )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def get(self, expense_id: int, user_id: int) -> Optional[Expense]:
        with SessionLocal() as session:
            return ExpenseRepository(session).get(expense_id, user_id)

    # ------------------------------------------------------------------
    # Human correction
    # ------------------------------------------------------------------
    def apply_correction(self, expense_id: int, user_id: int, field_name: str, raw_value: Any) -> Optional[Expense]:
        """Validate and store a corrected FINAL value. Raises ValueError with a
        user-friendly message when the value is invalid."""
        if field_name not in _FIELD_LABELS:
            raise ValueError("That field cannot be edited.")
        value = raw_value if not isinstance(raw_value, str) else parse_correction(field_name, raw_value)
        if field_name == "expense_amount":
            value = _money(value)
        with SessionLocal() as session:
            return ExpenseRepository(session).update_pending(expense_id, user_id, {field_name: value})

    def confirm(self, expense_id: int, user_id: int) -> ConfirmResult:
        with SessionLocal() as session:
            repo = ExpenseRepository(session)
            expense = repo.get(expense_id, user_id)
            if expense is None:
                return ConfirmResult("not_found")
            if expense.status != ExpenseStatus.PENDING.value:
                return ConfirmResult("not_pending", expense)

            missing = [label for attr, label in REQUIRED_TO_CONFIRM if getattr(expense, attr) in (None, "")]
            if missing:
                return ConfirmResult("missing_fields", expense, missing)

            if not expense.currency:  # the validate step already defaults this; belt and braces
                expense = repo.update_pending(expense_id, user_id, {"currency": settings.DEFAULT_CURRENCY}) or expense

            corrections = compute_corrections(expense)
            ok = repo.confirm(
                expense_id, user_id,
                final_result=final_snapshot(expense),
                corrected_fields=[c[0] for c in corrections],
                corrections=corrections,
            )
            if not ok:
                return ConfirmResult("not_pending", expense)
            confirmed = repo.get(expense_id, user_id)
            session.refresh(confirmed)  # the bulk UPDATE bypassed the identity map

        self._report_feedback(confirmed, corrections)
        return ConfirmResult("ok", confirmed)

    def discard(self, expense_id: int, user_id: int) -> bool:
        with SessionLocal() as session:
            repo = ExpenseRepository(session)
            repo.clear_pending_edit(user_id)
            return repo.discard(expense_id, user_id)

    @staticmethod
    def _report_feedback(expense: Expense, corrections) -> None:
        """Attach per-field correctness to the LangSmith run that produced the extraction.
        Only scores/field names are sent - never the receipt values themselves."""
        if not expense or not expense.langsmith_run_id:
            return
        wrong = {c[0] for c in corrections}
        for attr, _label in CORRECTABLE_FIELDS:
            send_feedback(expense.langsmith_run_id, key=f"{attr}_correct", score=0.0 if attr in wrong else 1.0)
        send_feedback(
            expense.langsmith_run_id, key="human_corrected", score=1.0 if wrong else 0.0,
            comment=", ".join(sorted(wrong)) or "no corrections",
        )

    # ------------------------------------------------------------------
    # "Waiting for typed value" state
    # ------------------------------------------------------------------
    def begin_edit(self, user_id: int, expense_id: int, field_name: str) -> None:
        with SessionLocal() as session:
            ExpenseRepository(session).set_pending_edit(user_id, expense_id, field_name)

    def get_edit(self, user_id: int):
        with SessionLocal() as session:
            row = ExpenseRepository(session).get_pending_edit(user_id, settings.PENDING_EDIT_TTL_MINUTES)
            return (row.expense_id, row.field) if row else None

    def clear_edit(self, user_id: int) -> None:
        with SessionLocal() as session:
            ExpenseRepository(session).clear_pending_edit(user_id)

    def expire_stale(self) -> int:
        with SessionLocal() as session:
            return ExpenseRepository(session).expire_stale_pending(settings.PENDING_EXPENSE_TTL_MINUTES)

    # ------------------------------------------------------------------
    # CSV bulk import (rows are the user's own data -> saved as confirmed)
    # ------------------------------------------------------------------
    def import_csv(self, user_id: int, csv_path: str, max_rows: int = 1000) -> Dict[str, Any]:
        """Expected columns: date, vendor, amount [, currency, category, description,
        invoice_number, item, goods_or_service]. Bad rows are skipped and counted."""
        imported, skipped, reasons = 0, 0, []
        with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as fh, SessionLocal() as session:
            repo = ExpenseRepository(session)
            for line_no, row in enumerate(csv.DictReader(fh), start=2):
                if imported + skipped >= max_rows:
                    reasons.append(f"stopped after {max_rows} rows")
                    break
                row = {(k or "").strip().lower(): v for k, v in row.items()}
                amount = parse_amount(row.get("amount"))
                the_date = parse_date_flexible(row.get("date"))
                if amount is None or amount <= 0 or the_date is None:
                    skipped += 1
                    if len(reasons) < 5:
                        reasons.append(f"line {line_no}: needs a valid date and a positive amount")
                    continue
                category = normalize_category(row.get("category")) or ExpenseCategory.MISCELLANEOUS
                gos = normalize_goods_or_service(row.get("goods_or_service"))
                expense = repo.create(
                    telegram_user_id=user_id,
                    status=ExpenseStatus.CONFIRMED.value,
                    source="csv",
                    vendor=clean_text(row.get("vendor"), 255),
                    invoice_number=clean_text(row.get("invoice_number"), 100),
                    expense_date=the_date,
                    item=clean_text(row.get("item"), 255),
                    goods_or_service=gos.value if gos else None,
                    category=category.value,
                    expense_amount=_money(amount),
                    currency=normalize_currency(row.get("currency")) or settings.DEFAULT_CURRENCY,
                    description=clean_text(row.get("description"), 500),
                    confirmed_at=datetime.utcnow(),
                )
                imported += 1
        try:
            os.remove(csv_path)
        except OSError:
            pass
        logger.info("CSV import: %d imported, %d skipped", imported, skipped)
        return {"imported": imported, "skipped": skipped, "reasons": reasons}


expense_service = ExpenseService()
