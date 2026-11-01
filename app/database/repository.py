"""All database access for expenses. PostgreSQL does the filtering and the maths.

Reports only ever see rows with status='confirmed'.
"""
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import extract, func, or_
from sqlalchemy.orm import Session

from app.database.models import Expense, ExpenseCorrection, ManualDraft, PendingEdit
from app.schemas.enums import ExpenseStatus
from app.utils.clock import utcnow

UNCATEGORIZED = "Uncategorized"


@dataclass
class ExpenseFilter:
    """One filter object used by every report/lookup so they can never disagree.

    user_id=None means "whole organization" (all authorized users)."""

    user_id: Optional[int] = None
    start: Optional[date] = None
    end: Optional[date] = None
    category: Optional[str] = None
    search_text: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    only_with_receipt: bool = False


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _num(value) -> float:
    return round(float(value or 0), 2)


class ExpenseRepository:
    def __init__(self, session: Session):
        self.session = session

    # ------------------------------------------------------------------
    # Writes: pending -> confirmed flow
    # ------------------------------------------------------------------
    def create(self, **fields) -> Expense:
        expense = Expense(**fields)
        self.session.add(expense)
        self.session.commit()
        self.session.refresh(expense)
        return expense

    def get(self, expense_id: int, user_id: Optional[int] = None) -> Optional[Expense]:
        query = self.session.query(Expense).filter(Expense.id == expense_id)
        if user_id is not None:
            query = query.filter(Expense.telegram_user_id == user_id)
        return query.first()

    def update_pending(self, expense_id: int, user_id: int, fields: dict) -> Optional[Expense]:
        """Change FINAL values of a still-pending expense. AI values are never touched."""
        expense = self.get(expense_id, user_id)
        if expense is None or expense.status != ExpenseStatus.PENDING.value:
            return None
        for key, value in fields.items():
            setattr(expense, key, value)
        expense.updated_at = utcnow()
        self.session.commit()
        self.session.refresh(expense)
        return expense

    def confirm(self, expense_id: int, user_id: int, final_result: dict, corrected_fields: List[str],
                corrections: List[Tuple[str, Optional[str], Optional[str]]]) -> bool:
        """Atomically flip pending -> confirmed. Returns False if it was not pending
        (e.g. the user double-tapped Confirm), so nothing is written twice."""
        now = utcnow()
        updated = (
            self.session.query(Expense)
            .filter(Expense.id == expense_id,
                    Expense.telegram_user_id == user_id,
                    Expense.status == ExpenseStatus.PENDING.value)
            .update(
                {
                    Expense.status: ExpenseStatus.CONFIRMED.value,
                    Expense.confirmed_at: now,
                    Expense.updated_at: now,
                    Expense.final_result: final_result,
                    Expense.corrected_by_user: bool(corrected_fields),
                    Expense.corrected_fields: corrected_fields,
                },
                synchronize_session=False,
            )
        )
        if updated != 1:
            self.session.rollback()
            return False
        for field, ai_value, final_value in corrections:
            self.session.add(ExpenseCorrection(
                expense_id=expense_id, telegram_user_id=user_id,
                field=field, ai_value=ai_value, final_value=final_value,
            ))
        self.session.commit()
        return True

    def discard(self, expense_id: int, user_id: int) -> bool:
        updated = (
            self.session.query(Expense)
            .filter(Expense.id == expense_id, Expense.telegram_user_id == user_id,
                    Expense.status == ExpenseStatus.PENDING.value)
            .update({Expense.status: ExpenseStatus.DISCARDED.value, Expense.updated_at: utcnow()},
                    synchronize_session=False)
        )
        self.session.commit()
        return updated == 1

    def expire_stale_pending(self, ttl_minutes: int) -> int:
        cutoff = utcnow() - timedelta(minutes=ttl_minutes)
        updated = (
            self.session.query(Expense)
            .filter(Expense.status == ExpenseStatus.PENDING.value, Expense.created_at < cutoff)
            .update({Expense.status: ExpenseStatus.EXPIRED.value}, synchronize_session=False)
        )
        self.session.commit()
        return updated

    # ------------------------------------------------------------------
    # "Waiting for the user to type a value" state
    # ------------------------------------------------------------------
    def set_pending_edit(self, user_id: int, expense_id: int, field: str) -> None:
        self.session.query(PendingEdit).filter(PendingEdit.telegram_user_id == user_id).delete()
        self.session.add(PendingEdit(telegram_user_id=user_id, expense_id=expense_id, field=field))
        self.session.commit()

    def get_pending_edit(self, user_id: int, ttl_minutes: int) -> Optional[PendingEdit]:
        row = self.session.query(PendingEdit).filter(PendingEdit.telegram_user_id == user_id).first()
        if row is None:
            return None
        if row.created_at and row.created_at < utcnow() - timedelta(minutes=ttl_minutes):
            self.clear_pending_edit(user_id)
            return None
        return row

    def clear_pending_edit(self, user_id: int) -> None:
        self.session.query(PendingEdit).filter(PendingEdit.telegram_user_id == user_id).delete()
        self.session.commit()

    # ------------------------------------------------------------------
    # /add wizard state
    # ------------------------------------------------------------------
    def start_manual(self, user_id: int) -> None:
        self.session.query(ManualDraft).filter(ManualDraft.telegram_user_id == user_id).delete()
        self.session.add(ManualDraft(telegram_user_id=user_id, step="amount"))
        self.session.commit()

    def get_manual(self, user_id: int, ttl_minutes: int) -> Optional[ManualDraft]:
        row = self.session.query(ManualDraft).filter(ManualDraft.telegram_user_id == user_id).first()
        if row is None:
            return None
        if row.created_at and row.created_at < utcnow() - timedelta(minutes=ttl_minutes):
            self.clear_manual(user_id)
            return None
        return row

    def update_manual(self, user_id: int, **fields) -> None:
        self.session.query(ManualDraft).filter(ManualDraft.telegram_user_id == user_id).update(fields)
        self.session.commit()

    def clear_manual(self, user_id: int) -> None:
        self.session.query(ManualDraft).filter(ManualDraft.telegram_user_id == user_id).delete()
        self.session.commit()

    # ------------------------------------------------------------------
    # Duplicate detection
    # ------------------------------------------------------------------
    def find_duplicate_by_hash(self, receipt_hash: str, exclude_id: Optional[int] = None) -> Optional[Expense]:
        """An earlier pending/confirmed expense created from the byte-identical image."""
        if not receipt_hash:
            return None
        query = self.session.query(Expense).filter(
            Expense.receipt_hash == receipt_hash,
            Expense.status.in_([ExpenseStatus.PENDING.value, ExpenseStatus.CONFIRMED.value]))
        if exclude_id is not None:
            query = query.filter(Expense.id != exclude_id)
        return query.order_by(Expense.id).first()

    # ------------------------------------------------------------------
    # Reads (confirmed expenses only)
    # ------------------------------------------------------------------
    def _filtered(self, query, f: ExpenseFilter):
        query = query.filter(Expense.status == ExpenseStatus.CONFIRMED.value)
        if f.user_id is not None:
            query = query.filter(Expense.telegram_user_id == f.user_id)
        if f.start:
            query = query.filter(Expense.expense_date >= f.start)
        if f.end:
            query = query.filter(Expense.expense_date <= f.end)
        if f.category:
            query = query.filter(Expense.category == f.category)
        if f.currency:
            query = query.filter(Expense.currency == f.currency)
        if f.only_with_receipt:
            query = query.filter(Expense.receipt_file_reference.isnot(None))
        if f.amount is not None:
            query = query.filter(func.abs(Expense.expense_amount - Decimal(str(f.amount))) < Decimal("0.01"))
        if f.search_text:
            like = f"%{_like_escape(f.search_text)}%"
            query = query.filter(or_(
                Expense.vendor.ilike(like, escape="\\"),
                Expense.item.ilike(like, escape="\\"),
                Expense.description.ilike(like, escape="\\"),
                Expense.category.ilike(like, escape="\\"),
            ))
        return query

    def list_expenses(self, f: ExpenseFilter, limit: Optional[int] = None, newest_first: bool = True) -> List[Expense]:
        query = self._filtered(self.session.query(Expense), f)
        order = (Expense.expense_date.desc(), Expense.id.desc()) if newest_first else (Expense.expense_date, Expense.id)
        query = query.order_by(*order)
        if limit:
            query = query.limit(limit)
        return query.all()

    def count_expenses(self, f: ExpenseFilter) -> int:
        return self._filtered(self.session.query(func.count(Expense.id)), f).scalar() or 0

    def currency_counts(self, f: ExpenseFilter) -> Dict[str, int]:
        """{'INR': 12, 'USD': 1} for the filter (ignores f.currency)."""
        g = ExpenseFilter(**{**f.__dict__, "currency": None})
        rows = (self._filtered(self.session.query(Expense.currency, func.count(Expense.id)), g)
                .group_by(Expense.currency).all())
        return {(cur or "?"): int(n) for cur, n in rows}

    def totals(self, f: ExpenseFilter) -> Tuple[int, float]:
        count, total = self._filtered(
            self.session.query(func.count(Expense.id), func.coalesce(func.sum(Expense.expense_amount), 0)), f
        ).one()
        return int(count or 0), _num(total)

    def by_category(self, f: ExpenseFilter) -> List[dict]:
        total = func.sum(Expense.expense_amount)
        rows = (self._filtered(self.session.query(Expense.category, total, func.count(Expense.id)), f)
                .group_by(Expense.category).order_by(total.desc()).all())
        return [{"category": cat or UNCATEGORIZED, "total": _num(amt), "count": int(n)} for cat, amt, n in rows]

    def by_month(self, f: ExpenseFilter) -> List[dict]:
        year, month = extract("year", Expense.expense_date), extract("month", Expense.expense_date)
        rows = (self._filtered(self.session.query(year, month, func.sum(Expense.expense_amount),
                                                  func.count(Expense.id)), f)
                .group_by(year, month).order_by(year, month).all())
        return [{"year": int(y), "month": int(m), "total": _num(amt), "count": int(n)} for y, m, amt, n in rows]

    def by_day(self, f: ExpenseFilter) -> List[dict]:
        """One row per calendar day that has expenses (SQL GROUP BY); gaps are filled in Python."""
        total = func.sum(Expense.expense_amount)
        rows = (self._filtered(self.session.query(Expense.expense_date, total, func.count(Expense.id)), f)
                .group_by(Expense.expense_date).order_by(Expense.expense_date).all())
        return [{"date": d, "total": _num(amt), "count": int(n)} for d, amt, n in rows if d is not None]

    def by_vendor(self, f: ExpenseFilter, limit: int = 10) -> List[dict]:
        total = func.sum(Expense.expense_amount)
        rows = (self._filtered(self.session.query(Expense.vendor, total, func.count(Expense.id)), f)
                .group_by(Expense.vendor).order_by(total.desc()).limit(limit).all())
        return [{"vendor": v or "Unknown", "total": _num(amt), "count": int(n)} for v, amt, n in rows]

    # ------------------------------------------------------------------
    # Feedback / evaluation reads
    # ------------------------------------------------------------------
    def receipt_status_counts(self, since: Optional[datetime] = None) -> Dict[str, int]:
        """{'confirmed': 62, 'discarded': 3, ...} for receipts created on/after `since`."""
        query = self.session.query(Expense.status, func.count(Expense.id)).filter(Expense.source == "receipt")
        if since:
            query = query.filter(Expense.created_at >= since)
        return {status: int(n) for status, n in query.group_by(Expense.status).all()}

    def list_confirmed_for_eval(self, since: Optional[datetime] = None) -> List[Expense]:
        query = self.session.query(Expense).filter(
            Expense.status == ExpenseStatus.CONFIRMED.value, Expense.source == "receipt")
        if since:
            query = query.filter(Expense.confirmed_at >= since)
        return query.order_by(Expense.id).all()

