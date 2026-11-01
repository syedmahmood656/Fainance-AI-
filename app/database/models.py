"""Database models.

expenses            one row per receipt. Plain columns hold the FINAL values (what
                    reports use). `*_ai` columns hold what the AI originally said,
                    `ai_extraction` the full raw AI result, `final_result` a snapshot
                    taken at confirmation.
expense_corrections one row per field a human changed (AI value -> final value).
                    This is the evaluation dataset.
pending_edits       "the user is about to type a new value for field X" (survives restarts).

Only rows with status='confirmed' are counted by reports.
"""
import time
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    create_engine,
    inspect,
    text,
)
from sqlalchemy import types as sqltypes
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config.settings import settings
from app.utils.clock import utcnow
from app.utils.logging import logger

Base = declarative_base()


def _utcnow() -> datetime:
    return utcnow()


class Expense(Base):
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    # Telegram IDs can exceed 32 bits -> BigInteger
    telegram_user_id = Column(BigInteger, nullable=False, index=True)
    # pending | confirmed | discarded | expired   (legacy rows become 'confirmed')
    status = Column(String(16), nullable=False, default="pending", server_default="confirmed", index=True)
    source = Column(String(16), nullable=False, default="receipt", server_default="receipt")  # receipt | csv

    # ---- FINAL values (what reports/Excel/charts use) ----
    vendor = Column(String(255), nullable=True, index=True)
    invoice_number = Column(String(100), nullable=True)
    expense_date = Column(Date, nullable=True, index=True)
    item = Column(String(255), nullable=True)
    goods_or_service = Column(String(16), nullable=True)
    category = Column(String(64), nullable=True, index=True)
    expense_amount = Column(Numeric(14, 2), nullable=True)
    currency = Column(String(8), nullable=True)
    subtotal = Column(Numeric(14, 2), nullable=True)
    tax = Column(Numeric(14, 2), nullable=True)
    description = Column(String(500), nullable=True)
    receipt_file_reference = Column(String(255), nullable=True)
    receipt_hash = Column(String(64), nullable=True, index=True)  # sha256 of the uploaded image (duplicate detection)

    # ---- AI-extracted values (never overwritten) ----
    vendor_ai = Column(String(255), nullable=True)
    expense_date_ai = Column(Date, nullable=True)
    category_ai = Column(String(64), nullable=True)
    item_ai = Column(String(255), nullable=True)
    goods_or_service_ai = Column(String(16), nullable=True)
    expense_amount_ai = Column(Numeric(14, 2), nullable=True)
    ai_extraction = Column(JSON, nullable=True)     # full raw AI result (+ model/provider info)
    final_result = Column(JSON, nullable=True)      # snapshot of final values at confirmation
    extraction_meta = Column(JSON, nullable=True)   # warnings, assumed fields, latency...

    # ---- human feedback ----
    corrected_by_user = Column(Boolean, nullable=False, default=False, server_default=text("false"))
    corrected_fields = Column(JSON, nullable=True)  # e.g. ["category", "expense_amount"]
    langsmith_run_id = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)
    confirmed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_expenses_user_status_date", "telegram_user_id", "status", "expense_date"),
    )


class ExpenseCorrection(Base):
    """AI value vs human-corrected value, one row per corrected field."""

    __tablename__ = "expense_corrections"

    id = Column(Integer, primary_key=True)
    expense_id = Column(Integer, ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_user_id = Column(BigInteger, nullable=False)
    field = Column(String(32), nullable=False, index=True)
    ai_value = Column(String(255), nullable=True)
    final_value = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=_utcnow)


class PendingEdit(Base):
    """Conversation state: which field is this user about to type a new value for."""

    __tablename__ = "pending_edits"

    telegram_user_id = Column(BigInteger, primary_key=True)
    expense_id = Column(Integer, ForeignKey("expenses.id", ondelete="CASCADE"), nullable=False)
    field = Column(String(32), nullable=False)
    created_at = Column(DateTime, default=_utcnow)


class ManualDraft(Base):
    """Conversation state for the /add wizard (amount -> category -> description).
    Kept in the database because each Telegram message is a separate request."""

    __tablename__ = "manual_drafts"

    telegram_user_id = Column(BigInteger, primary_key=True)
    step = Column(String(16), nullable=False, default="amount")   # amount | category | description
    amount = Column(Numeric(14, 2), nullable=True)
    category = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=_utcnow)


def _normalize_db_url(url: str) -> str:
    # Heroku/Supabase style "postgres://" is not understood by SQLAlchemy 2.x
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def describe_db_url(url: str) -> str:
    """The database target with the password hidden - safe for logs and error messages."""
    from sqlalchemy.engine import make_url

    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001
        return "<unparseable DATABASE_URL>"


def connect_args_for(url: str) -> dict:
    """Without a timeout, an unreachable Postgres host blocks for MINUTES (the OS default).
    libpq understands connect_timeout for both psycopg2 and psycopg 3; SQLite has no network."""
    if url.startswith("sqlite"):
        return {}
    return {"connect_timeout": settings.DB_CONNECT_TIMEOUT_SECONDS}


_DB_URL = _normalize_db_url(settings.DATABASE_URL)
engine = create_engine(_DB_URL, pool_pre_ping=True, connect_args=connect_args_for(_DB_URL))
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)


# ---------------------------------------------------------------------------
# Schema creation / lightweight migration
# ---------------------------------------------------------------------------
def _default_sql(column) -> str | None:
    default = column.server_default
    if default is None:
        return None
    arg = getattr(default, "arg", None)
    if arg is None:
        return None
    if hasattr(arg, "text"):          # text("false")
        return arg.text
    return "'" + str(arg).replace("'", "''") + "'"


def ensure_schema() -> None:
    """Create missing tables; on PostgreSQL also upgrade an older `expenses` table in place.

    This is intentionally small (not Alembic). It is idempotent: running it on an
    already-upgraded database does nothing.
    """
    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "postgresql":
        return
    if "expenses" not in inspect(engine).get_table_names():
        return

    def columns():
        return {c["name"]: c for c in inspect(engine).get_columns("expenses")}

    existing = columns()
    with engine.begin() as conn:
        if "amount" in existing and "expense_amount" not in existing:
            conn.execute(text("ALTER TABLE expenses RENAME COLUMN amount TO expense_amount"))
            logger.info("migration: renamed expenses.amount -> expense_amount")
        if "tax_amount" in existing and "tax" not in existing:
            conn.execute(text("ALTER TABLE expenses RENAME COLUMN tax_amount TO tax"))
            logger.info("migration: renamed expenses.tax_amount -> tax")

    existing = columns()
    with engine.begin() as conn:
        for col in Expense.__table__.columns:
            if col.name in existing:
                continue
            ddl = (f'ALTER TABLE expenses ADD COLUMN IF NOT EXISTS "{col.name}" '
                   f"{col.type.compile(dialect=engine.dialect)}")
            default = _default_sql(col)
            if default is not None:
                ddl += f" DEFAULT {default}"
            conn.execute(text(ddl))
            logger.info("migration: added expenses.%s", col.name)

        # Older versions stored dates as timestamps and money as floats.
        if isinstance(existing.get("expense_date", {}).get("type"), sqltypes.DateTime):
            conn.execute(text("ALTER TABLE expenses ALTER COLUMN expense_date TYPE DATE USING expense_date::date"))
        for name in ("expense_amount", "subtotal", "tax"):
            if isinstance(existing.get(name, {}).get("type"), sqltypes.Float):
                conn.execute(text(
                    f"ALTER TABLE expenses ALTER COLUMN {name} TYPE NUMERIC(14,2) USING round({name}::numeric, 2)"))
        # Pending receipts may legitimately have unknown values.
        for name in ("vendor", "expense_date", "expense_amount", "currency", "category"):
            if name in existing:
                conn.execute(text(f"ALTER TABLE expenses ALTER COLUMN {name} DROP NOT NULL"))


def init_db(retries: int | None = None, delay: float = 2.0) -> None:
    """Wait briefly for the database (it may still be starting in docker-compose), then create/upgrade
    tables. Fails fast with an actionable message instead of hanging."""
    retries = retries or settings.DB_CONNECT_RETRIES
    target = describe_db_url(_DB_URL)
    logger.info("Connecting to database: %s", target)
    for attempt in range(1, retries + 1):
        try:
            ensure_schema()
            return
        except OperationalError as exc:
            reason = str(exc).splitlines()[0][:140]
            if attempt == retries:
                raise RuntimeError(
                    f"Cannot reach the database at {target} after {retries} attempts ({reason}).\n"
                    "  - Docker:  run  docker compose up -d db  and wait until  docker compose ps  says healthy\n"
                    "  - Check DATABASE_URL in .env (host/port/user/password)\n"
                    "  - Quick test without Postgres:  DATABASE_URL=sqlite:///./expenses.db") from exc
            logger.warning("Database not ready (attempt %d/%d): %s", attempt, retries, reason)
            time.sleep(delay)
