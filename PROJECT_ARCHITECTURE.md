# PROJECT_ARCHITECTURE.md — Ledger Finance AI

> Definitive onboarding guide. Every function name, signature and behaviour below was taken from the code in this repository. Section numbers match the sections of the documentation brief.
>
> **Verification status:** the pure-logic modules are covered by tests that were executed (41 passing). The database layer, LangGraph wiring, MCP subprocess, Telegram layer and live provider calls are covered by integration tests (`tests/test_integration.py`, `tests/test_llm_fallback.py`) that must be run in an environment with the dependencies installed (`pytest -q`).

---

## Table of Contents
1. [High-Level System Overview](#1-high-level-system-overview)
2. [Directory & File Structure](#2-directory--file-structure-tree)
3. [Core Modules & File Breakdown](#3-core-modules--file-breakdown)
4. [Data Models & State Management](#4-data-models--state-management)
5. [Execution Flow & Lifecycle](#5-execution-flow--lifecycle)
6. [Cross-Cutting Concerns, Limits & Extension Points](#6-cross-cutting-concerns-limits--extension-points)

---

# 1. High-Level System Overview

## 1.1 Purpose
Small organizations collect paper/phone receipts and need structured, auditable expense data without manual entry. **Ledger Finance AI** is a Telegram bot that:

1. Accepts a **receipt photo**, reads it with a vision-capable LLM, and categorizes it into one of the organization's predefined categories (defined in `app/schemas/enums.py`).
2. Puts a **human in the loop**: the user must *Confirm* or *Correct* the AI's result before it counts. AI values and human values are stored side by side, producing an evaluation dataset as a by-product.
3. Answers **natural-language report requests** (totals, category breakdowns, monthly trends, charts, Excel workbooks) and **retrieves original receipt images** — with all numbers computed by SQL/Python, never by the LLM.

## 1.2 Architectural Style
**Modular monolith** with one optional short-lived child process.

| Aspect | Decision |
|---|---|
| Deployment unit | One FastAPI/ASGI process (`app.main:app`) + one PostgreSQL container. |
| Child process | The **MCP reporting server** (`python -m app.mcp_server.server`) is spawned over **stdio** *per report request* by `app/mcp_server/client.py`, then terminated. |
| Concurrency model | Single-threaded `asyncio` event loop. LLM calls, Telegram I/O and file I/O (via thread offload where CPU-heavy) are non-blocking; SQLAlchemy calls are **synchronous** (short, local, indexed queries). |
| Interaction pattern | Event-driven at the edge (Telegram updates → background tasks), request/response inside (LangGraph workflow per update). |
| State | **PostgreSQL is the only durable state store.** In-process state is limited to disposable caches (see §4.5). |
| Orchestration | **LangGraph** `StateGraph` with deterministic conditional routing; the LLM is a *tool inside nodes*, never the router. |

### Responsibility separation (enforced in code)

| Component | Allowed to do | Forbidden from doing |
|---|---|---|
| **LLM** | Read receipt images; pick category/item/type; interpret report wording into a structured `ReportRequest` | Calculate totals, resolve dates, choose tools, choose user IDs |
| **LangGraph** | Route, hold per-request state, call services | Contain SQL or maths |
| **MCP server** | Expose 10 controlled reporting tools | Be used for receipt flow / feedback / image retrieval |
| **PostgreSQL** | Filter, `SUM/COUNT/GROUP BY`, source of truth | — |
| **Python** | Validation, date maths, charts (Matplotlib), Excel (OpenPyXL), business rules | — |
| **Local FS (Docker volume)** | Receipt images (`RECEIPTS_DIR`) and temp artifacts (`REPORTS_TMP_DIR`) | — |
| **LangSmith** | Optional tracing/feedback scores | Be required to run |
| **Telegram** | UI (messages, inline buttons, files) | Business logic |

## 1.3 System Context Diagram

```
                         ┌──────────────────────────────── Docker Compose ───────────────────────────────┐
┌────────────┐  HTTPS    │  ┌───────────────────────── FastAPI process (app.main) ──────────────────────┐ │
│  Telegram  │◄─────────►│  │  Webhook / Long-poll ──► telegram/bot.py ──► LangGraph (agent/graph.py)   │ │
│  user      │  updates  │  │                                   │            │        │          │       │ │
└────────────┘           │  │                                   │            ▼        ▼          ▼       │ │
                         │  │                         StorageService   ai/ (LLM)  services/   mcp_server/ │ │
                         │  │                         (receipts vol.)  │          │ repository   client.py │ │
                         │  └───────────────────────────────────────────┼──────────┼──────────────┼───────┘ │
                         │                                              │          ▼              │ stdio   │
                         │                                              │     ┌─────────┐   ┌─────▼──────┐  │
                         │                                              │     │Postgres │◄──│ MCP server │  │
                         │                                              │     └─────────┘   │ (subprocess)│ │
                         └──────────────────────────────────────────────┼───────────────────┴────────────┘──┘
                                                                         ▼
                                        OpenRouter │ Google Gemini │ Hugging Face   (+ optional LangSmith)
```

## 1.4 Tech Stack

| Concern | Technology | Where used |
|---|---|---|
| Language / runtime | Python 3.11+ (Docker: `python:3.11-slim`) | everywhere |
| Web framework | **FastAPI** + **Uvicorn** | `app/main.py` |
| Telegram | **python-telegram-bot** (`Bot`, `HTTPXRequest`, inline keyboards) | `app/telegram/*` |
| Workflow engine | **LangGraph** (`StateGraph`, `START`, `END`) | `app/agent/graph.py` |
| LLM abstraction | **langchain-core** messages; **langchain-openai** `ChatOpenAI` (OpenRouter + Hugging Face router, both OpenAI-compatible); **langchain-google-genai** `ChatGoogleGenerativeAI` | `app/ai/llm_provider.py` |
| Tool protocol | **MCP** Python SDK (`mcp.server.fastmcp.FastMCP`, `ClientSession`, `stdio_client`) | `app/mcp_server/*` |
| Observability | **LangSmith** (`traceable`, `Client.create_feedback`) — optional | `app/utils/tracing.py` |
| Database | **PostgreSQL 15** via **SQLAlchemy 2.x** (sync engine, `psycopg2-binary`); SQLite in tests | `app/database/*` |
| Validation / config | **Pydantic v2**, **pydantic-settings** | `app/schemas/*`, `app/config/settings.py` |
| Charts | **Matplotlib** (`Agg` backend) | `app/reporting/charts.py` |
| Excel | **OpenPyXL** | `app/reporting/excel.py` |
| Images | **Pillow** (resize/EXIF/JPEG) | `app/ai/llm_utils.py` |
| HTTP client | **httpx** (OpenRouter model discovery; PTB transport) | `app/ai/llm_provider.py` |
| Tests | **pytest** | `tests/` |
| Packaging | Docker, docker-compose (app + db + 2 named volumes) | repo root |

### External dependencies at runtime
| Service | Required? | Purpose |
|---|---|---|
| Telegram Bot API | **Yes** | UI |
| PostgreSQL | **Yes** | Persistence |
| ≥1 LLM provider key (OpenRouter / Gemini / Hugging Face) | **Yes** for receipt reading & free-text parsing (reports have a keyword fallback) | Intelligence |
| LangSmith | No | Tracing, feedback scores, datasets |

---

# 2. Directory & File Structure Tree

```
Ledger-Finance-AI/
├── PROJECT_ARCHITECTURE.md        # this document
├── README.md                      # operator/user quick-start
├── requirements.txt               # dependency ranges
├── Dockerfile                     # app image
├── docker-compose.yml             # app + postgres + volumes
├── .env.example                   # every configuration key, no secrets
├── .dockerignore / .gitignore
├── pytest.ini                     # test discovery, warning filter
├── docs/
│   ├── doc.txt                    # (legacy notes, untouched)
│   └── prompt.txt                 # (legacy notes, untouched)
├── scripts/
│   ├── __init__.py
│   ├── export_eval_dataset.py     # live feedback → N, X%, Y%, CIs, JSONL, LangSmith dataset
│   ├── eval_receipts.py           # accuracy on hand-labelled receipts (ground-truth CSV)
│   ├── check_llm.py               # per-model health check
│   └── remap_categories.py        # re-label old category names after a category change
├── app/
│   ├── main.py                    # FastAPI app, lifespan, webhook endpoint
│   ├── config/
│   │   └── settings.py            # Settings (pydantic-settings) singleton
│   ├── schemas/
│   │   ├── enums.py               # categories, statuses, intents, periods (no pydantic)
│   │   └── expense.py             # Pydantic models: LLM output, ReportRequest, ExpenseOut
│   ├── database/
│   │   ├── models.py              # ORM tables, engine, SessionLocal, ensure_schema/init_db
│   │   ├── repository.py          # ExpenseRepository + ExpenseFilter (all SQL)
│   │   └── session.py             # get_db() dependency (re-exports SessionLocal)
│   ├── services/
│   │   ├── expense_service.py     # pending→confirm lifecycle, corrections, CSV import
│   │   ├── feedback.py            # pure AI-vs-human diff + snapshot
│   │   ├── evaluation.py          # pure scoring + Wilson confidence intervals
│   │   ├── validation.py          # pure sanitize + confirmation-card warnings
│   │   └── storage_service.py     # StorageProvider ABC + local-disk implementation
│   ├── ai/
│   │   ├── llm_provider.py        # multi-provider/model fallback engine (ResilientLLM)
│   │   ├── llm_utils.py           # JSON extraction, error taxonomy, free-model filter, image prep
│   │   ├── receipt_extractor.py   # ReceiptExtractor, ExpenseCategorizer, ReportParser + prompts
│   │   └── report_rules.py        # deterministic keyword fallback for report requests
│   ├── agent/
│   │   ├── state.py               # AgentState TypedDict
│   │   ├── graph.py               # LangGraph nodes, routers, compile, run_agent
│   │   ├── feedback_flow.py       # confirm/correct/discard state machine + keyboards
│   │   ├── manual_flow.py         # /add wizard state machine (amount → category → description)
│   │   └── reporting_flow.py      # ReportRequest → tool calls; tool results → reply
│   ├── mcp_server/
│   │   ├── server.py              # FastMCP server exposing 10 tools
│   │   └── client.py              # spawns server over stdio, calls tools (or direct mode)
│   ├── reporting/
│   │   ├── tools.py               # tool implementations (SQL aggregation → chart/Excel/dict)
│   │   ├── charts.py              # Matplotlib renderers
│   │   ├── excel.py               # OpenPyXL workbook builder
│   │   └── periods.py             # deterministic date-range resolution
│   ├── telegram/
│   │   ├── bot.py                 # TelegramBot wrapper, update dispatch, delivery
│   │   └── polling.py             # long-polling loop (no public URL needed)
│   └── utils/
│       ├── logging.py             # stderr logging config
│       ├── tracing.py             # optional LangSmith integration + scrubbing
│       ├── clock.py               # utcnow() without the deprecated datetime.utcnow()
│       ├── formatting.py          # money/date formatting, confirmation card
│       └── parsing.py             # forgiving amount/date/currency/correction parsers
└── tests/
    ├── conftest.py                # hermetic env (SQLite, temp dirs, fake keys)
    ├── test_pure_logic.py         # no DB/LLM/network
    ├── test_llm_fallback.py       # provider fallback, cooldowns, vision payload
    └── test_integration.py        # full flow on SQLite with fake LLM
```

### Folder responsibilities
- **`app/config/`** — one immutable-by-convention `settings` singleton; the only place environment variables are read (plus `LOG_LEVEL` in `utils/logging.py`).
- **`app/schemas/`** — the *vocabulary* of the system: enums and Pydantic models. `enums.py` deliberately has no pydantic dependency so pure modules can import it cheaply.
- **`app/database/`** — persistence. `models.py` declares tables; `repository.py` is the **only** module containing queries (single choke point for tenancy/status filters).
- **`app/services/`** — business rules that are neither HTTP, LLM nor SQL: lifecycle, validation, feedback computation, storage abstraction.
- **`app/ai/`** — everything that talks to an LLM, plus its deterministic helpers and fallback.
- **`app/agent/`** — orchestration: LangGraph graph, state, and the two deterministic "flow" helpers (feedback, reporting).
- **`app/mcp_server/`** — the MCP boundary (server + client).
- **`app/reporting/`** — read-side analytics implementations used by MCP tools.
- **`app/telegram/`** — the UI adapter; translates Telegram updates ⇄ `AgentState`.
- **`app/utils/`** — stateless helpers (pure functions) and cross-cutting setup (logging, tracing).
- **`scripts/`** — operator CLIs, run as `python -m scripts.<name>`.
- **`tests/`** — see §3.10.

---

# 3. Core Modules & File Breakdown

Conventions: `async` marks coroutines. "Pure" = no I/O, deterministic, no hidden state.

## 3.1 Configuration & Entry Point

### `app/config/settings.py` — Central configuration
**Role:** Declares every tunable as a typed field on `Settings(BaseSettings)`, loads `.env`, and exposes the module-level singleton `settings`. Environment variables take precedence over `.env`.

| Item | Signature | Logic |
|---|---|---|
| `_split_csv` | `(value) -> List[str]` | Splits comma-separated strings, trims, drops empties. |
| `Settings.clean_env_strings` | `@field_validator("*", mode="before") (cls, v, info)` | Strips whitespace and wrapping quotes (Docker `env_file` keeps literal quotes); converts an **empty string to the field's default**, so `KEY=` means "use default" instead of failing validation. |
| `allowed_user_ids` | property `-> List[int]` | Parses `ALLOWED_TELEGRAM_USER_IDS` (string on purpose — pydantic-settings would JSON-decode a `List` field and break `"1,2"`). |
| `gemini_key` / `hf_token` | property `-> str \| None` | Resolve aliases (`GEMINI_API_KEY`/`GOOGLE_API_KEY`, `HUGGINGFACEHUB_API_TOKEN`/`HF_TOKEN`). |
| `provider_order` | property `-> List[str]` | Lower-cased `LLM_PROVIDER_ORDER`. |
| `langsmith_enabled` / `langsmith_project` | properties | Enabled iff a key **and** the tracing flag (new or legacy name) are set. |
| `models_for` | `(provider, vision) -> List[str]` | Maps `(provider, vision)` to the matching `*_TEXT_MODELS`/`*_VISION_MODELS` CSV field. |

**Key setting groups:** Telegram (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_MODE`, `TELEGRAM_WEBHOOK_URL/SECRET`), LLM (`LLM_PROVIDER_ORDER`, provider keys, model lists, `LLM_MAX_OUTPUT_TOKENS`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_MODEL_ATTEMPTS`, `LLM_MAX_MODELS_PER_PROVIDER`, `LLM_MODEL_COOLDOWN_SECONDS`, `IMAGE_MAX_SIDE_PX`, `IMAGE_JPEG_QUALITY`), LangSmith, storage (`DATABASE_URL`, `RECEIPTS_DIR`, `REPORTS_TMP_DIR`, `DEFAULT_CURRENCY`), reporting (`REPORTS_SCOPE`, `USE_MCP`, `MCP_CALL_TIMEOUT_SECONDS`), state TTLs (`PENDING_EXPENSE_TTL_MINUTES`=1440, `PENDING_EDIT_TTL_MINUTES`=15).

**Data flow:** env/.env → validators → `settings` → imported by nearly every module (read-only).

### `app/main.py` — Application entry point
**Role:** Builds the FastAPI app, runs startup/shutdown in `lifespan`, exposes `/health` and `/telegram/webhook`.

| Function | Signature | Logic |
|---|---|---|
| `_cleanup_old_report_files` | `(max_age_hours=24) -> None` | Deletes files in `REPORTS_TMP_DIR` older than the cutoff (crash leftovers). |
| `_use_webhook` | `() -> bool` | `mode == "webhook"` or (`mode == "auto"` and a webhook URL is set). |
| `lifespan` | `async (app)` | **Startup:** `init_db` (thread-offloaded) → `expense_service.expire_stale` → temp-file cleanup → either `bot.set_webhook(...)` or `asyncio.create_task(run_polling())`. **Shutdown:** cancel polling task. |
| `health_check` | `async () -> dict` | `GET /health` → `{"status": "healthy"}`. |
| `telegram_webhook` | `async (request)` | `POST /telegram/webhook`: verifies optional secret header, parses JSON, calls `schedule_update(data)` and **returns 200 immediately**. |

**Ordering constraint:** `setup_tracing()` is called right after importing `settings` and *before* the rest of the app imports, because LangSmith caches its env flags on first use.

**Data flow:** Telegram → HTTP body → `schedule_update` (fire-and-forget). Nothing else is done in the request cycle, which prevents Telegram's retry storms.

---

## 3.2 Schemas & Vocabulary

### `app/schemas/enums.py` — Enums and category knowledge (no third-party imports)
**Role:** Single source of truth for categories, statuses, intents and period kinds.

| Symbol | Description |
|---|---|
| `ExpenseCategory` (`str, Enum`) | The organization's categories (currently 13: printing and stationary, conveyance, travelling expenses, mobile recharge, internet, books and library, guest hospitality, summer islamic camp, islamic vocational classes, infaq, zakath, zonal share, Miscellaneous). **Edit this enum + `CATEGORY_DESCRIPTIONS` to change them; nothing else is hard-coded.** |
| `CATEGORY_DESCRIPTIONS` | Per-category hint text injected into the categorization prompt. |
| `ALL_CATEGORIES` | Ordered list; **index order is the contract** for the `cc:<id>:<idx>` Telegram callback. |
| `normalize_category(value) -> ExpenseCategory \| None` | Case/`&`/alias-tolerant mapping (e.g. "Conveyance" → Transportation); `None` if unmatchable (never guesses). |
| `GoodsOrService`, `normalize_goods_or_service` | `Goods`/`Service` with synonym handling. |
| `ExpenseStatus` | `pending → confirmed` or `discarded`/`expired`. |
| `ReportIntent` | summary, category_breakdown, monthly_trend, yearly_summary, chart, excel_report, expense_list, receipt_lookup, unknown. |
| `ChartKind` | category_bar, category_pie, top_categories, monthly_bar, yearly_trend, vendor_bar, **timeline**. |
| `PeriodKind` | this_month, last_month, this_year, last_year, month, year, last_n_months, custom, all. |
| `CORRECTABLE_FIELDS` / `CORRECTABLE_FIELD_KEYS` | `(key, label)` pairs: vendor, expense_date, category, item, goods_or_service, expense_amount — drives the Correct menu **and** the AI-vs-human diff. |

### `app/schemas/expense.py` — Pydantic models
**Role:** Validation boundary between untrusted LLM/user text and typed data.

| Class | Purpose & behaviour |
|---|---|
| `ReceiptExtraction` | Vision-model output. **All fields optional** (`vendor, invoice_number, expense_date, expense_amount, currency, subtotal, tax, description`). `mode="before"` validators coerce `"₹2,450.00"`, `"08/10/2026"`, `"Rs."` via `utils/parsing.py`; **unparseable → `None`, never an error, never invented.** `is_empty()` → True if vendor, amount, date and description are all missing. |
| `CategorizationResult` | `category: ExpenseCategory` (required), `item`, `goods_or_service`. An unknown category **raises**, which the LLM layer treats as a bad answer and retries on the next model. |
| `Period` | `kind, year, month, months, start_date, end_date` with lenient coercion (bad `kind` → `this_month`; non-numeric ints → `None`). |
| `ReportRequest` | `intent, chart_type, period, category, search_text, amount, top_n, granularity, cumulative`. Coerces unknown intent → `UNKNOWN`, bad chart → `None`, `top_n` clamped to 1..12, `granularity` limited to auto/day/week/month. Contains **no user_id** by design. |
| `ExpenseOut` | `from_attributes=True` read model of a stored expense including `*_ai` fields, `ai_extraction`, `final_result`, feedback flags and timestamps. |

---

## 3.3 Persistence Layer

### `app/database/models.py` — ORM, engine, migration
**Role:** Declares tables, builds the engine/session factory, and guarantees the schema exists/upgrades.

| Symbol | Description |
|---|---|
| `Expense`, `ExpenseCorrection`, `PendingEdit` | Tables (full column documentation in §4.1). |
| `_normalize_db_url(url)` | Rewrites legacy `postgres://` → `postgresql://`. |
| `engine`, `SessionLocal` | `create_engine(..., pool_pre_ping=True)`; `sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False)` — the last flag lets ORM objects be read after their session closes. |
| `_default_sql(column) -> str \| None` | Renders a column's `server_default` as a SQL literal for `ALTER TABLE`. |
| `ensure_schema() -> None` | `create_all`; then, **on PostgreSQL only**, upgrades a legacy `expenses` table in place: renames `amount→expense_amount`, `tax_amount→tax`; `ADD COLUMN IF NOT EXISTS` for every missing model column (legacy rows get `status='confirmed'` via default); converts timestamp→`DATE` and float→`NUMERIC(14,2)`; drops `NOT NULL` on columns that may now be unknown. Idempotent. |
| `init_db(retries=30, delay=2.0)` | Retries `ensure_schema` on `OperationalError` (DB container still booting). |

### `app/database/repository.py` — The only SQL in the system
**Role:** `ExpenseRepository(session)` encapsulates every read/write; `ExpenseFilter` is the single filter object shared by reports, Excel and receipt lookup so they cannot disagree.

`ExpenseFilter` (dataclass): `user_id` (None = organization), `start`, `end` (inclusive dates), `category`, `search_text`, `amount`, `currency`, `only_with_receipt`.

| Method | Signature → Return | Logic |
|---|---|---|
| `create` | `(**fields) -> Expense` | add, commit, refresh. |
| `get` | `(expense_id, user_id=None) -> Expense \| None` | Optional ownership filter. |
| `update_pending` | `(expense_id, user_id, fields: dict) -> Expense \| None` | Sets **final** columns only if the row is the user's and `status == pending`. AI columns are never touched. |
| `confirm` | `(expense_id, user_id, final_result, corrected_fields, corrections) -> bool` | **Atomic compare-and-set**: `UPDATE … WHERE id AND user AND status='pending'`; returns `False` if 0 rows (double-tap safe); on success inserts `ExpenseCorrection` rows and commits. |
| `discard` | `(expense_id, user_id) -> bool` | Conditional update to `discarded`. |
| `expire_stale_pending` | `(ttl_minutes) -> int` | Bulk-marks old pending rows `expired`. |
| `set_pending_edit` / `get_pending_edit` / `clear_pending_edit` | | One row per user (replace semantics); `get` auto-deletes entries older than the TTL. |
| `_filtered` | `(query, f) -> Query` | Always adds `status == 'confirmed'`; applies user/date/category/currency/receipt/amount (±0.01 via `abs()`)/`ILIKE` (escaped `%`,`_`,`\`) over vendor, item, description, category. |
| `list_expenses` | `(f, limit=None, newest_first=True)` | Ordered by date, id. |
| `count_expenses`, `currency_counts`, `totals` | | `COUNT`, per-currency counts (ignores `f.currency`), `(count, SUM)`. |
| `by_category`, `by_month`, `by_day`, `by_vendor` | `-> List[dict]` | `GROUP BY` aggregations (`by_day` = one row per calendar day with expenses); month uses portable `extract('year'/'month')`; null category → `"Uncategorized"`. |
| `list_confirmed_for_eval` | `(since=None)` | Confirmed `source='receipt'` rows for the dataset export. |

**Data flow:** callers build an `ExpenseFilter` → repository adds the mandatory `confirmed` predicate → SQL aggregation → plain `dict`/ORM rows (floats rounded to 2 dp by `_num`).

### Additions after the first release
- **`agent/manual_flow.py`** — `handle_manual(user_id, event, svc=None) -> dict`: the `/add` wizard (`start` → amount → category buttons `ma:<idx>` or typed name → description/`skip`). Progress is stored in the `manual_drafts` table (one row per user, TTL = `PENDING_EDIT_TTL_MINUTES`); starting a field edit or `/cancel` closes it. Saved via `ExpenseService.finish_manual` → `create_manual_expense` (status `confirmed`, `source='manual'`, excluded from AI-accuracy statistics).
- **Duplicate detection** — the SHA-256 of each uploaded image is stored in `expenses.receipt_hash`; `create_pending_receipt` looks for an earlier pending/confirmed expense with the same hash and records it in `extraction_meta["duplicate_of"]`, which `card_warnings` shows. It warns, never blocks. (Re-compressed copies of the same receipt have different bytes and are not caught.)
- **`services/evaluation.py`** — `wilson_interval`, `proportion`, `vendor_matches` (forgiving fuzzy match), `score_receipt`, `summarize`, `live_summary`, `resume_line`.
- **`utils/clock.py`** — `utcnow()`.

### `app/database/session.py`
Re-exports `SessionLocal`; `get_db()` is a FastAPI-style generator dependency (kept for API routes; the agent uses `with SessionLocal() as s:`).

---

## 3.4 Domain Services

### `app/services/expense_service.py` — Lifecycle orchestration
**Role:** The human-in-the-loop state machine `receipt → pending → (correct)* → confirmed | discarded`. No LLM calls. Module singleton: `expense_service`.

| Method | Signature → Return | Logic |
|---|---|---|
| `create_pending_receipt` | `(user_id, receipt_ref, extraction, categorization, meta, langsmith_run_id=None) -> Expense` | Inserts a `pending` row: **final columns = AI values**, `*_ai` columns + `ai_extraction` JSON freeze the AI snapshot, `extraction_meta` stores warnings/model info. Amounts via `_money()` → `Decimal`. |
| `get` | `(expense_id, user_id)` | Ownership-scoped read. |
| `apply_correction` | `(expense_id, user_id, field_name, raw_value) -> Expense \| None` | Validates via `parse_correction` (raises `ValueError` with a user-safe message), stores to the **final** column of a pending row. |
| `confirm` | `(expense_id, user_id) -> ConfirmResult` | Checks required fields (`expense_amount`, `expense_date`, `category`) → defaults missing currency to `DEFAULT_CURRENCY` → `compute_corrections` → atomic `repo.confirm` → refresh → send LangSmith feedback. Returns `ConfirmResult(status ∈ ok/not_found/not_pending/missing_fields, expense, missing)`. |
| `discard` | `(expense_id, user_id) -> bool` | Clears the user's edit state, then discards. |
| `_report_feedback` | static `(expense, corrections)` | Sends per-field `*_correct` scores and `human_corrected` to the LangSmith run — **scores only, no receipt values**. |
| `begin_edit` / `get_edit` / `clear_edit` | | Thin wrappers over pending-edit rows; `get_edit` → `(expense_id, field) \| None`. |
| `expire_stale` | `() -> int` | Uses `PENDING_EXPENSE_TTL_MINUTES`. |
| `import_csv` | `(user_id, csv_path, max_rows=1000) -> dict` | Reads `date, vendor, amount [, currency, category, description, invoice_number, item, goods_or_service]`; invalid rows skipped with reasons; valid rows inserted directly as `confirmed`, `source='csv'`; file removed afterwards. Returns `{imported, skipped, reasons}`. |

### `app/services/feedback.py` — Pure AI-vs-human diff
| Function | Logic |
|---|---|
| `normalize_value(value) -> str \| None` | Comparable form: money → 2-dp string, dates → ISO, text whitespace-collapsed, blank → `None`. |
| `compute_corrections(expense) -> List[(field, ai_str, final_str)]` | For each correctable field compares `<field>_ai` vs `<field>`. A field the AI left empty and the human filled **is** a correction; both empty is not. |
| `final_snapshot(expense) -> dict` | JSON-safe snapshot of final values stored in `final_result`. |

### `app/services/validation.py` — The "validate" step (pure + `settings.DEFAULT_CURRENCY`)
| Function | Logic |
|---|---|
| `sanitize_extraction(data: dict, today: date) -> (dict, assumed_fields)` | Nulls impossible values (amount ≤ 0, negative subtotal/tax, dates >1 day in the future or >5 years old); if currency missing, sets `DEFAULT_CURRENCY` and records `"currency"` in `assumed_fields` (transparent assumption). **Never invents other values.** |
| `card_warnings(expense, today, meta) -> List[str]` | Computes warnings from **current** values each time the card renders, so they vanish once fixed: extraction failed, missing amount/date/category, categorization unavailable, assumed currency, subtotal+tax ≠ total (tolerance max(1, 2 %)). |

### `app/services/storage_service.py` — Receipt image storage
`StorageProvider` (ABC: `store, retrieve, get_path, delete`) → `LocalStorageProvider(base_dir=None)`; `StorageService` exposes async wrappers `save_file, get_file, get_file_path, delete_file`; singleton `storage_service`.

- `store(content, ext)` writes `<uuid4hex><ext>` and returns **only the file name** as the reference.
- `_resolve(reference)` uses `Path(reference).name`: backward compatible with legacy `receipts/<uuid>.jpg` values and **immune to path traversal** (`../../etc/passwd`).
- Swapping to S3/GCS means implementing four methods; nothing else changes.

---

## 3.5 AI / LLM Layer

### `app/ai/llm_utils.py` — Pure LLM helpers
| Function | Signature | Logic |
|---|---|---|
| `BadModelOutput` | exception (`ValueError`) | Model answered but not with usable JSON. |
| `message_text` | `(response) -> str` | Normalizes LangChain content (string or list of blocks). |
| `extract_json` | `(text) -> dict` | Strips `<think>…</think>` and code fences, tries `json.loads`, else a **string-aware balanced-brace scan** for the first valid object. Raises `BadModelOutput`. |
| `classify_error` | `(exc) -> str` | Taxonomy: `rate_limit, not_found, auth, credits, context, timeout, bad_output, other` from HTTP status and message patterns (e.g. OpenRouter 402 "fewer max_tokens" ⇒ `credits`, not `context`). |
| `cooldown_seconds` | `(kind, base) -> int` | rate_limit=base; credits=6×; not_found/auth=12×; timeout/other=max(30, base/5); **context/bad_output=0** (input-specific, not the model's fault). |
| `select_free_models` | `(payload, vision, limit=6) -> List[str]` | From OpenRouter `/models`: keeps `:free` or zero-price models, drops embedding/guard/audio/etc., requires image input for vision, sorts by context length desc. |
| `prepare_image_for_llm` | `(raw, max_side=1280, quality=80) -> bytes` | Pillow: decode, EXIF-transpose, flatten alpha to white, downscale (LANCZOS), re-encode JPEG. **Primary fix for token/size errors.** Raises `ValueError` if not an image. |
| `to_data_url`, `detect_image_extension` | | Base64 data URL; extension from real bytes (not filename). |

### `app/ai/llm_provider.py` — Multi-provider fallback engine
**Role:** One call surface — `await llm.ainvoke_json(...)` — that hides provider choice, failures and JSON validation.

| Symbol | Description |
|---|---|
| `Candidate(provider, model)` (frozen dataclass), `.key` | A model attempt. |
| `LLMCallInfo` | `provider, model, attempts, latency_ms, input_tokens, output_tokens, failures`; `as_dict()`. |
| `LLMUnavailableError` | All candidates failed; message is user-safe (`_friendly_failure`). |
| Cooldown registry | Module dict `_cooldown_until`; `_is_cooling`, `_mark_failed` (an `auth` error cools the **whole provider**, key `provider:*`), `reset_cooldowns`. |
| `_discover_openrouter_free(vision)` | `async`; GET `/models` (10 s), cached 30 min per vision/text; on failure retries in ~2 min. |
| `_models_for_provider(provider, vision)` | OpenRouter order — text: *configured → `openrouter/free` → discovered*; vision: *configured → discovered → `openrouter/free`*. Others: configured list. |
| `get_candidates(vision)` | Iterates `provider_order`, skips providers without keys, caps `LLM_MAX_MODELS_PER_PROVIDER` per provider (so one provider cannot consume every attempt), returns **healthy first, cooling last**. |
| `build_chat_model(candidate)` | Lazy-imports provider packages (a missing optional package breaks only that provider). OpenRouter/HF → `ChatOpenAI(base_url=…)`, Gemini → `ChatGoogleGenerativeAI`. Always sets `temperature=0`, **explicit `max_tokens`/`max_output_tokens`**, `timeout`, `max_retries=0` (fallback is ours). Cached per candidate. |
| `_build_messages(system, user_text, image_jpeg)` | `SystemMessage` + `HumanMessage` (text, or text + `image_url` data-URL parts). |
| `_no_tracing()` | Context manager disabling LangSmith around the raw model call (it contains the image). |
| `ResilientLLM.ainvoke_json` | `async (*, task, system, user_text, schema, image_jpeg=None, accept=None) -> (BaseModel, LLMCallInfo)` — see data flow below. Decorated `@traced("llm_json_call", run_type="llm")`. |
| `llm` | Module singleton. |

**`ainvoke_json` data flow:** build candidates → for each (≤ `LLM_MAX_MODEL_ATTEMPTS`): `ainvoke` with `asyncio.wait_for` → `message_text` → `extract_json` → `schema.model_validate` → optional `accept()` (may raise `BadModelOutput`, used to reject empty extractions) → on success return `(parsed, info)` with token usage; on any exception classify, set cooldown, record failure, continue. After exhaustion raise `LLMUnavailableError`. Pydantic `ValidationError` is mapped to `bad_output`.

### `app/ai/receipt_extractor.py` — The three LLM tasks + prompts
| Class / function | Signature → Return | Logic |
|---|---|---|
| `EXTRACTION_SYSTEM` | prompt | "Return ONE JSON object; **null for anything not clearly visible; never guess**"; day-first dates; `expense_amount` = grand total; compact JSON template instead of a verbose schema (saves tokens). |
| `ReceiptExtractor.extract` | `async (image_bytes) -> (ReceiptExtraction, LLMCallInfo)` | Offloads `prepare_image_for_llm` to a thread, calls vision LLM with `accept=_reject_empty`. `@traced("receipt_extraction")`. |
| `_categorizer_system()` | `-> str` | Builds the prompt from `ALL_CATEGORIES` + descriptions. |
| `ExpenseCategorizer.categorize` | `async (data: ReceiptExtraction) -> (CategorizationResult, LLMCallInfo)` | Text-only call with vendor/items/amount; returns **only** category, item, type (LLM never re-emits vendor/amount/date, so it cannot alter them). |
| `_parser_system(today)` | `-> str` | Intent/chart/period vocabulary + 3 few-shot examples; instructs the model not to compute dates or totals. |
| `ReportParser.parse` | `async (text, today) -> (ReportRequest, "llm" \| "rules")` | LLM first; on `LLMUnavailableError` **or** `unknown` intent, falls back to `rule_based_request`. |

### `app/ai/report_rules.py` — Deterministic fallback parser (pure)
`rule_based_request(text, today=None) -> dict` (accepted by `ReportRequest(**d)`). Helpers: `_has` (word-prefix regex), `_find_category` (full names then single-word aliases), `_find_last_n_months` (digits or "six"), `_find_period` (last N months, this/last month/year, "all time", month names with optional year, bare years), `_find_amount` (currency-prefixed or bare ≥3-digit/comma numbers, ignoring years). Intent precedence: receipt_lookup → excel → chart (kind: pie/top/vendor/category/monthly/yearly) → monthly_trend → yearly_summary → category_breakdown → expense_list → summary → unknown. Applies per-intent default periods.

---

## 3.6 Orchestration (LangGraph)

### `app/agent/state.py`
`AgentState(TypedDict, total=False)` — shared state. **Inputs:** `user_id, input_type (receipt|csv|feedback|text), message_text, receipt_ref, csv_file_path, feedback`. **Receipt:** `extraction, categorization, extraction_meta, expense_id`. **Report:** `report_request, parsed_by, tool_calls`. **Outputs:** `response_text, buttons, edit_message, toast, chart_paths, excel_file_path, receipt_images, error`. Nodes return **partial updates**; LangGraph merges them (last-write-wins per key). `Button = Tuple[label, callback_data]`.

### `app/agent/graph.py` — The workflow
Module-level singletons: `extractor`, `categorizer`, `parser`; constants `MAX_RECEIPTS_SENT=10`, `HELP_TEXT`.

```
START ──route_input──┬─ extract_receipt ─(error?)─► END
                     │        └► validate_receipt ► categorize_expense ► save_pending ► END
                     ├─ handle_feedback ► END
                     ├─ text_feedback   ► END        (text while an edit is pending)
                     ├─ process_csv     ► END
                     └─ parse_report ─┬─ lookup_receipts ► END
                                      ├─ help_reply      ► END
                                      ├─ run_report      ► END
                                      └─ (error)         ► END
```

| Function | Signature | Logic |
|---|---|---|
| `scope_user_id` | `(user_id) -> int \| None` | `None` when `REPORTS_SCOPE == organization`. |
| `route_input` | `(state) -> str` | Deterministic by `input_type`; for `text`, checks `expense_service.get_edit(user_id)` — a pending typed correction wins over report parsing. |
| `after_extract` / `after_parse` | `(state) -> str` | Conditional-edge routers (error → END; intent → lookup/help/report). |
| `extract_receipt_node` | `async` | Loads image via storage; `extractor.extract`. `ValueError` (not an image) → deletes file, sets `error`. `LLMUnavailableError` → **graceful degradation**: empty `ReceiptExtraction` + `extraction_failed` meta so the user can enter data manually. |
| `validate_receipt_node` | `async` | `sanitize_extraction` on the dumped model; stores `assumed_fields`. Skipped if extraction failed. |
| `categorize_node` | `async` | Skips when nothing extracted; on `LLMUnavailableError` sets `categorization_failed` (category left `None`, user must choose — no guessed category). |
| `save_pending_node` | `async` | `expense_service.create_pending_receipt(..., langsmith_run_id=current_run_id())`; renders card + Confirm/Correct buttons. |
| `handle_feedback_node` / `text_feedback_node` | `async` | Delegate to `feedback_flow.handle_feedback`; text node maps `cancel/back` → `cancel_edit`. |
| `process_csv_node` | `async` | `expense_service.import_csv`; summarizes imported/skipped. |
| `parse_report_node` | `async` | `parser.parse(text, date.today())`; records `report_type`, `parsed_by`. |
| `run_report_node` | `async` | `plan_tool_calls` → `call_reporting_tools` → `build_reply`; `ReportingToolError` → friendly error. |
| `lookup_receipts_node` | `async` | Builds an `ExpenseFilter(only_with_receipt=True)`; **progressively relaxes** (drop `search_text`, then drop period when amount given); resolves image paths via storage; caps at 10; reports missing files. |
| `help_reply_node` | `async` | Returns `HELP_TEXT`. |
| `create_graph` | `() -> CompiledGraph` | Registers nodes/edges; compiled once into module singleton `graph`. |
| `run_agent` | `async (state) -> AgentState` | Telegram-facing entry; `@traced("telegram_request")`; attaches request type, hashed user, latency, error metadata around `graph.ainvoke`. |

### `app/agent/feedback_flow.py` — Confirm/Correct state machine
Pure-ish module (calls `expense_service`). **Callback protocol** (≤ 64 bytes): `cf:<id>` confirm · `co:<id>` open correct menu · `cp:<id>:<field>` pick field · `cc:<id>:<idx>` set category by `ALL_CATEGORIES` index · `ct:<id>:G|S` set type · `cd:<id>` discard · `cb:<id>` back.

| Function | Logic |
|---|---|
| `main_buttons(eid)` | `[[✅ Confirm, ✏️ Correct]]`. |
| `render_card(expense, title)` | `format_expense_card` + live `card_warnings` + main buttons. |
| `_correct_menu`, `_category_menu`, `_type_menu`, `_rows` | Keyboard builders (2 per row). |
| `handle_feedback(user_id, fb) -> dict` | Dispatch on `fb["action"]`: `text` (apply typed value to the pending field; invalid → warning, state kept), `cancel_edit`, `confirm` (ok / missing_fields / already handled), `correct`, `field` (category/type → buttons; others → `begin_edit` + prompt), `set_category`/`set_type`, `back`, `discard`. Returns a partial `AgentState` including `edit_message` and `toast`. Ownership enforced by `expense_service.get(eid, user_id)`; non-pending expenses reject everything except a (harmless) repeated confirm. |

### `app/agent/reporting_flow.py` — Deterministic tool planner & formatter
| Function | Signature → Return | Logic |
|---|---|---|
| `plan_tool_calls` | `(req, scope_user_id, today) -> List[(tool, args)]` | Resolves the period with `resolve_period` and maps intent → tools via explicit branches: summary → `get_expense_summary`; category_breakdown → `get_category_breakdown` + `generate_category_chart`; monthly_trend → trend + `generate_monthly_chart` (a single-month period is widened to **last 6 months**); yearly_summary → yearly + monthly chart; chart → by `ChartKind`; excel → `generate_expense_report`; list → `list_expenses`. `user_id` comes from the caller, never from the LLM. |
| `_text_for` / `_scope_line` | `(tool, result) -> str` | Per-tool text rendering using only tool-returned numbers. |
| `build_reply` | `(calls, results) -> {"text", "chart_paths", "excel_path"}` | Prefers data-tool text; chart-tool text only as fallback; de-duplicates "no data" lines; appends "N expenses in other currencies excluded" notes. |

---

## 3.7 MCP Boundary & Reporting Implementation

### `app/mcp_server/server.py` — MCP server (stdio)
`FastMCP("expense-reporting")` registers 10 tools; each is a thin wrapper that calls the same-named function in `app/reporting/tools.py` and returns `json.dumps(result, default=str)` (**returning a JSON string** makes the wire format independent of SDK structured-output conventions).

| Tool | Parameters | Result keys (JSON) |
|---|---|---|
| `get_expense_summary` | user_id?, start_date?, end_date?, category?, period_label? | total, count, average, top_category, currency |
| `get_category_breakdown` | same | rows[{category,total,count,percent}], total |
| `get_monthly_expense_trend` | same | rows[{month,label,total,count}] (zero-filled) |
| `get_yearly_expense_summary` | user_id?, year?, category? | months[], categories[], total |
| `list_expenses` | …, limit=25 | rows[], total_count |
| `generate_bar_chart` | …, group_by (category/month/vendor), top_n | chart_path, rows |
| `generate_category_chart` | …, style (bar/pie/top), top_n | chart_path, rows |
| `generate_monthly_chart` | … | chart_path, rows |
| `generate_timeline_chart` | …, granularity (auto/day/week/month), cumulative | chart_path, granularity, rows[{bucket,label,total,count,cumulative}] |
| `generate_expense_report` | … | file_path (.xlsx), count, total |

**Critical:** stdout is the protocol channel — the process must never print; `utils/logging.py` therefore logs to **stderr**.

### `app/mcp_server/client.py` — MCP client
| Function | Logic |
|---|---|
| `ReportingToolError` | Failure of a tool/transport. |
| `_call_mcp(calls)` | `async`: builds `StdioServerParameters(command=sys.executable, args=["-m","app.mcp_server.server"], env={**os.environ,…}, cwd=PROJECT_ROOT)` — **the full environment is passed explicitly** because the SDK otherwise forwards only a minimal one (so `DATABASE_URL` would be lost). One `ClientSession` per request: `initialize` → `list_tools` (verify names) → `call_tool` sequentially → parse JSON text; `isError` → raise. |
| `_call_direct(calls)` | `async`: same tool table executed in-process via `asyncio.to_thread` (when `USE_MCP=false`). |
| `call_reporting_tools(calls)` | `async`, `@traced("mcp_tool_calls", run_type="tool")`; chooses mode, applies `MCP_CALL_TIMEOUT_SECONDS`, records tools/mode/latency/error in trace metadata. |

**Trade-off:** spawning per request costs ~1–2 s but removes connection lifecycle/stale-session bugs (appropriate for low report volume).

### `app/reporting/tools.py` — Tool implementations
Shared helpers: `_out_dir`, `_new_path(prefix, ext)` (UUID-suffixed temp path), `_label`, `_category` (validates, raises `ValueError`), **`_context(session, user_id, start, end, category)`** (builds the filter, picks **one currency** — `DEFAULT_CURRENCY` if present else the most frequent — and counts other-currency rows), `_base(...)`, `expense_to_dict`, `_with_percent`, `_month_rows` (zero-fills gaps via `month_range`). Public functions mirror the MCP tools (table in §3.7). Registry `TOOLS: Dict[str, Callable]` is used by both the MCP server and direct mode. Empty results return `{"empty": True, ...}` rather than errors.

### `app/reporting/periods.py` — Date arithmetic (pure)
`resolve_period(kind, today, year, month, months, start_date, end_date) -> (start, end, label)`; helpers `month_start`, `month_end`, `add_months`, `month_range`, `parse_iso_date`. Rules: `month=9` asked in October → current year; `month=12` asked in October → previous year; `last_n_months` is *inclusive of the current month*; custom ranges auto-swap if reversed.

### `app/reporting/timeline.py` — Expense × time bucketing (pure)
`choose_granularity(start, end, requested="auto") -> "day"|"week"|"month"` (≤62 days → day, ≤200 → week, else month); `bucket_start`, `bucket_label`; `bucket_series(daily_rows, start, end, granularity, today=None) -> List[{bucket, label, total, count, cumulative}]` zero-fills empty periods so the line is continuous, computes the running total, groups weeks from Monday, and caps a still-running period at `today`. The `generate_timeline_chart` tool feeds it the SQL daily rows.

### `app/reporting/charts.py`
`horizontal_bars`, `vertical_bars`, `pie`, `line_timeline` (filled line, annotated peak) — all `(labels, values, currency, title, subtitle, out_path) -> out_path`. Uses the `Agg` backend (headless), Indian-style compact axis labels (`L`, `Cr`) for INR, closes figures to avoid memory growth. **Only draws pre-aggregated data.**

### `app/reporting/excel.py`
`build_workbook(expenses, period_label, currency, category=None, other_currency_count=0) -> Workbook` with sheets **Summary, Expenses (filtered rows + total), Category Summary, Monthly Summary**; `safe_cell(value)` neutralizes formula injection (`=`, `+`, `-`, `@` prefixes); `_header`, `_autosize` are formatting helpers. All totals are computed in Python from the supplied rows.

---

## 3.8 Telegram Adapter

### `app/telegram/bot.py`
`TelegramBot` wraps a PTB `Bot` (`HTTPXRequest` with 60 s read/write timeouts): `send_message`, `edit_message` (ignores "not modified"), `answer_callback`, `send_photo` (falls back to `send_document` on `BadRequest`), `send_document`, `download(file_id)` (20 MB cap, via Bot API — **no token-bearing URLs enter state/logs/traces**), `typing`. Singleton `bot_instance`.

| Function | Logic |
|---|---|
| `schedule_update(update_data)` | `asyncio.create_task(handle_update_safely(...))`, strong reference kept in `_background_tasks`. |
| `handle_update_safely` | try/except wrapper — one bad update never kills the loop. |
| `_already_seen(update_id)` | LRU-bounded (2000) in-memory dedupe of Telegram retries. |
| `handle_telegram_update(update_data)` | Dedupe → dispatch to `_handle_callback` or `_handle_message`. |
| `_handle_message(message)` | Authorization (`allowed_user_ids`) → `/start`, `/help` → photo/image-document: send "Reading…", download, `detect_image_extension`, `storage.save_file`, state `receipt` → `.csv` document → temp file, state `csv` → `/cancel` → feedback → text → state `text`. |
| `_handle_callback(query)` | Authorization, parse `CALLBACK_ACTIONS` + integer id, build `feedback` state, run. |
| `_run_and_deliver(chat_id, state, receipt_ref, callback_id, message_id)` | Under `_llm_slots` (`Semaphore(2)`) calls `run_agent`; always answers the callback in `finally`; deletes an orphaned receipt file if no expense was created; cleans temp files. |
| `_deliver(chat_id, final, message_id)` | Error → message; text (edit-in-place or new) with keyboard; charts; Excel; receipt images (0.4 s spacing for rate limits). |
| `_is_report_file`, `_cleanup` | Defence-in-depth: only files inside `REPORTS_TMP_DIR` are sent/deleted (stored receipts are never deleted here). |

### `app/telegram/polling.py`
`run_polling()` — deletes any webhook, loops `get_updates(timeout=25, read_timeout=40)`, converts `update.to_dict()` → `schedule_update`; backs off 3 s on errors; re-raises `CancelledError`. Used when `TELEGRAM_WEBHOOK_URL` is empty.

---

## 3.9 Utilities & Cross-Cutting

| File | Contents |
|---|---|
| `utils/logging.py` | `basicConfig` to **stderr**; silences `httpx/httpcore/telegram/urllib3` (URLs contain the bot token); exports `logger`. |
| `utils/tracing.py` | `setup_tracing()` sets both new and legacy LangSmith env vars **explicitly true/false** (prevents 401 spam from an empty key); `traced(name, run_type, tags)` decorator (no-op if langsmith missing/old; always uses `process_inputs/outputs=_scrub`); `_scrub` removes data-URL images, bytes, secrets (regex), hashes `user_id`, optionally redacts text (`LANGSMITH_REDACT_CONTENT`); `set_trace_metadata(**kv)`, `current_run_id()`, `hash_user_id`, `send_feedback(run_id, key, score, …)`. |
| `utils/formatting.py` | `format_money` (₹ with Indian grouping `1,23,45,678`), `format_number`, `format_compact`, `format_date` (`08 Oct 2026`), `format_expense_card`, `format_category_lines`, `truncate_for_telegram` (4000 chars), `or_unknown`. |
| `utils/parsing.py` | `clean_text`, `parse_amount` (handles `₹2,450.00`, `1.234,56`, `2450/-`), `normalize_currency` (known ISO codes only), `parse_date_flexible` (ISO and **day-first** formats), `parse_correction(field, text)` (per-field validation raising user-safe `ValueError`). |

## 3.10 Tooling, Config Files & Tests

| File | Purpose |
|---|---|
| `requirements.txt` | Dependency ranges (FastAPI, PTB, SQLAlchemy, pydantic, LangGraph, langchain-core/openai/google-genai, langsmith, mcp, openpyxl, matplotlib, Pillow, httpx, pytest). |
| `Dockerfile` | `python:3.11-slim`, installs `libpq-dev`, sets `MPLBACKEND=Agg`, runs `uvicorn app.main:app`. |
| `docker-compose.yml` | `app` (env from `.env`, overrides `DATABASE_URL`, mounts `receipts_data`), `db` (postgres:15-alpine with healthcheck; `app` waits for `service_healthy`), volumes `postgres_data`, `receipts_data`. |
| `.env.example` | Every configuration key with comments; no secrets. |
| `pytest.ini` | `testpaths = tests`; silences SQLite Decimal warning. |
| `scripts/export_eval_dataset.py` | `build_records(since)` → records (`inputs` = AI extraction, `ai_outputs`, `reference_outputs` = human-final, `corrected_fields`); `print_stats` (per-field accuracy, top category corrections); `upload_to_langsmith(records, dataset_name)`; `main()` CLI (`--stats`, `--out`, `--since`, `--langsmith`). Images are never exported. |
| `tests/conftest.py` | Sets a temp SQLite DB, temp dirs, `USE_MCP=false`, fake keys, and blanks model/key variables so a developer's real `.env` cannot leak in. |
| `tests/test_pure_logic.py` | Parsing, periods, error taxonomy, rule parser, planner, formatting, validation, feedback diff. |
| `tests/test_llm_fallback.py` | Fake chat models: fallthrough on 429/bad JSON, cooldown ordering, friendly total-failure error, provider order, missing key, image payload shape, unknown-category rejection. |
| `tests/test_integration.py` | End-to-end on SQLite with fake extractor/categorizer: pending card, corrections persisted as AI vs final, double-tap, ownership, manual-entry fallback, reports/charts/Excel/receipt retrieval, mixed currency, Telegram authorization/dedupe/webhook ACK. |

---

# 4. Data Models & State Management

## 4.1 Relational Schema (PostgreSQL)

```
expenses (1) ───< expense_corrections (N)       expenses (1) ───< pending_edits (≤1 per user)
```

### `expenses`
| Group | Columns | Notes |
|---|---|---|
| Identity | `id` PK, `telegram_user_id` **BIGINT** (IDs exceed 32 bits), `status`, `source` | `status ∈ pending/confirmed/discarded/expired`; `source ∈ receipt/csv/manual`. `receipt_hash` = SHA-256 of the uploaded image. Server defaults make legacy rows `confirmed`/`receipt`. |
| **Final values** (used by all reports) | `vendor`, `invoice_number`, `expense_date` **DATE**, `item`, `goods_or_service`, `category`, `expense_amount` **NUMERIC(14,2)**, `currency`, `subtotal`, `tax`, `description`, `receipt_file_reference` | Nullable so a pending row may have unknown values; required-at-confirm enforced in service code. |
| **AI values** (immutable after creation) | `vendor_ai`, `expense_date_ai`, `category_ai`, `item_ai`, `goods_or_service_ai`, `expense_amount_ai`, `ai_extraction` JSON | `ai_extraction` = `{"extraction": {...}, "categorization": {...}}`. |
| Snapshots/meta | `final_result` JSON (at confirmation), `extraction_meta` JSON (warnings, assumed fields, provider/model/latency, failure flags) | |
| Feedback | `corrected_by_user` BOOL, `corrected_fields` JSON list, `langsmith_run_id` | |
| Timestamps | `created_at`, `updated_at` (auto-updated), `confirmed_at` | UTC (`datetime.utcnow`). |
| Indexes | `telegram_user_id`, `status`, `vendor`, `expense_date`, `category`, composite `(telegram_user_id, status, expense_date)` | |

### `expense_corrections` (the evaluation dataset)
`id`, `expense_id` FK (ON DELETE CASCADE), `telegram_user_id`, `field`, `ai_value`, `final_value`, `created_at`. Written **only at confirmation**, derived from the diff, so edit-then-revert produces no noise.

### `manual_drafts` (conversation state for `/add`)
`telegram_user_id` PK, `step` (amount/category/description), `amount`, `category`, `created_at`.

### `pending_edits` (conversation state)
`telegram_user_id` **PK** (one outstanding prompt per user), `expense_id` FK, `field`, `created_at`. Expires after `PENDING_EDIT_TTL_MINUTES`.

## 4.2 Naming map: spec ↔ columns
`category_final` ≡ `category`; `category_ai` ≡ `category_ai`; "AI extraction result" ≡ `ai_extraction`; "final corrected result" ≡ `final_result`; "confirmation status" ≡ `status`; "receipt image reference" ≡ `receipt_file_reference`.

## 4.3 Expense State Machine

```
              create_pending_receipt
  (receipt) ────────────────────────► PENDING ──confirm (atomic CAS)──► CONFIRMED  (counted in reports)
                                        │  ▲                               
                          apply_correction │ └─ (final columns change; AI columns frozen)
                                        ├──discard──► DISCARDED
                                        └──TTL (24h, at startup)──► EXPIRED
  (csv row) ───────────────────────────────────────────────────────► CONFIRMED
```

## 4.4 Validation Layers (defence in depth)
1. **Image:** Pillow decode check (`prepare_image_for_llm`).
2. **LLM output:** `extract_json` → Pydantic coercion (lenient, null-on-doubt) → `accept()` rejects empty extractions → `sanitize_extraction` (plausibility rules).
3. **Category/type:** enum normalization; unknown category from the LLM is rejected and retried.
4. **Human input:** `parse_correction` per field.
5. **Confirm gate:** `expense_amount`, `expense_date`, `category` required.
6. **Database:** types (`NUMERIC`, `DATE`), conditional UPDATE for state transitions.
7. **Output:** Excel formula-injection guard; only report-dir files sent; path-traversal-safe storage.

## 4.5 State Locations & Synchronization

| State | Location | Lifetime | Multi-process safe? |
|---|---|---|---|
| Expenses, corrections, pending edits | PostgreSQL | durable | **Yes** (single source of truth) |
| Receipt images | `RECEIPTS_DIR` (volume) | durable | Shared-volume only |
| Per-request `AgentState` | LangGraph, in memory | one request | n/a (stateless between requests) |
| Update dedupe set | `bot._seen_updates` | process | **No** — per process |
| Model cooldowns, free-model cache, chat-model cache | `llm_provider` module dicts | process | **No** — per process (harmless: worst case a model is retried) |
| LLM concurrency limit | `Semaphore(2)` | process | per process |
| Temp charts/Excel | `REPORTS_TMP_DIR` | deleted after send; stale >24 h purged at boot | local |

**Synchronization strategy:** *No cross-component locking.* Consistency comes from (a) PostgreSQL transactions and the conditional `UPDATE … WHERE status='pending'` for the one race that matters (double confirm), (b) ownership-scoped queries, and (c) idempotent startup migration. Human-in-the-loop "checkpointing" is the `pending` row itself rather than a LangGraph checkpointer, because Telegram button presses arrive as independent HTTP calls.

---

# 5. Execution Flow & Lifecycle

## 5.1 Boot sequence (`uvicorn app.main:app`)
1. Python imports `app.main` → loads `settings` (`.env` + env, cleaned) → **`setup_tracing()`** sets LangSmith flags (on only if key + flag).
2. Imports `database.models` (creates the SQLAlchemy engine lazily — no connection yet), `expense_service`, `logger`.
3. FastAPI app constructed with `lifespan`.
4. **Lifespan startup:**
   1. Imports `telegram.bot` → constructs `Bot`; imports `agent.graph` → instantiates extractor/categorizer/parser and **compiles the LangGraph** (`create_graph()`).
   2. `init_db()` in a worker thread: `create_all`, then PostgreSQL in-place migration; retries while the DB boots.
   3. `expense_service.expire_stale()` marks old pending receipts `expired`.
   4. `_cleanup_old_report_files()`.
   5. Webhook mode → `set_webhook(url/telegram/webhook, allowed_updates=[message, callback_query], secret_token)`; otherwise → starts `run_polling()` as a background task.
5. Server accepts requests. `GET /health` is available immediately.

## 5.2 Lifecycle A — Receipt upload (the "extraction" path)

```
Telegram ──update(photo)──► webhook/poller ─► schedule_update ─► (HTTP 200 returned to Telegram)
                                                   │
                              handle_telegram_update: dedupe → authorize
                                                   │
                    "⏳ Reading your receipt…" → bot.download(file_id) → storage.save_file → ref
                                                   │
                            _run_and_deliver ─ acquire _llm_slots ─► run_agent(state{input_type=receipt})
                                                   │
 route_input → extract_receipt ── Pillow resize ─► ResilientLLM.ainvoke_json (vision candidates, fallback loop)
            → validate_receipt (sanitize, assume currency)
            → categorize_expense (text LLM → category/item/type)
            → save_pending (INSERT status=pending, AI snapshot, langsmith_run_id)
                                                   │
 _deliver: card text + [✅ Confirm][✏️ Correct] ──► Telegram
```
Failure branches: unreadable image → friendly error + file deleted; all models failing → pending row with empty values and a "tap Correct" warning; categorization failing → category left empty (must be chosen).

## 5.3 Lifecycle B — Confirmation / correction (human-in-the-loop)
1. User taps a button → `callback_query` update → `_handle_callback` parses `cf:12` etc. → `AgentState(input_type="feedback", feedback={action, expense_id, value})`.
2. `route_input` → `handle_feedback_node` → `feedback_flow.handle_feedback`.
3. **Confirm:** `expense_service.confirm` → required-field check → `compute_corrections` → atomic `UPDATE … status='confirmed'` + `expense_corrections` inserts → LangSmith feedback scores → card edited in place to "✅ Expense saved successfully."
4. **Correct:** menu → category/type via buttons (immediate update + re-rendered card); vendor/date/item/amount → `begin_edit` (INSERT `pending_edits`) and a prompt.
5. The user's **next text message** hits `route_input`, which sees the pending edit → `text_feedback_node` → `parse_correction` → update final column → `clear_edit` → new card. Invalid input keeps the edit active; `cancel` clears it.
6. `answer_callback_query` is always sent (stops the button spinner), with an optional toast.

## 5.4 Lifecycle C — Report request ("Show me September expenses by category")

```
text ─► route_input ─► parse_report ─► ReportParser.parse
           (LLM → ReportRequest{intent=category_breakdown, period={month:9}})      [fallback: rule_based_request]
        ─► after_parse ─► run_report
           plan_tool_calls: resolve_period → 2026-09-01..2026-09-30
                            [("get_category_breakdown", args), ("generate_category_chart", args)]
           call_reporting_tools ─► (USE_MCP) spawn `python -m app.mcp_server.server`
                                      initialize → list_tools → call_tool ×2 → JSON
                                  server tool ─► SQLAlchemy ─► PostgreSQL: GROUP BY category (confirmed only)
                                              ─► Matplotlib PNG in REPORTS_TMP_DIR
           build_reply: text lines "Travel — ₹42,000 (34%)" + total + currency note
        ─► _deliver: send text, send chart photo, delete temp file
```
Other intents reuse the same path with different tools; Excel replies send the `.xlsx` via `send_document`.

## 5.5 Lifecycle D — Receipt image retrieval ("Show me the receipt for the ₹2,450 restaurant expense")
`parse_report` → `receipt_lookup` (amount=2450, search_text="restaurant") → `lookup_receipts_node`: `ExpenseFilter` over **confirmed** rows with a receipt → ILIKE across vendor/item/description/category + amount ±0.01 → (relax filters if empty) → `storage.get_file_path(reference)` → up to 10 `{path, caption}` → `_deliver` sends each as photo (document fallback). The LLM supplied only filters.

## 5.6 Lifecycle E — CSV import
Document `.csv` → temp file → `process_csv_node` → `import_csv` (per-row validation, direct `confirmed` inserts) → summary message; temp file removed.

## 5.7 Concurrency & failure semantics
- Telegram receives **200 immediately**; processing happens in tasks → no retry storms; `_already_seen` handles any that still arrive.
- At most **2** LLM-heavy workflows run concurrently (`_llm_slots`), protecting free-tier rate limits; blocking CPU work (image resize) is thread-offloaded; sync DB calls are short.
- Every update is wrapped in `handle_update_safely`; users get a generic error rather than silence.

---

# 6. Cross-Cutting Concerns, Limits & Extension Points

## 6.1 Security
- Allow-list authorization on every message **and** callback; ownership-scoped feedback actions.
- Optional webhook secret header verification.
- Secrets only via environment; token-bearing URLs never stored; logging for HTTP libraries suppressed; LangSmith payloads scrubbed (images, bytes, secrets, hashed user IDs).
- SQL via SQLAlchemy parameters with escaped `LIKE`; Excel formula-injection guard; storage path-traversal guard; file-send restricted to the reports directory.
- MCP tool arguments are validated (`_category`, `style`, `group_by`); `user_id` is injected by trusted code only.

## 6.2 Observability
LangSmith traces: `telegram_request` (root) → LangGraph node runs → `receipt_extraction`, `expense_categorization`, `report_request_parsing`, `llm_json_call` (attempts, provider/model, tokens, failures), `mcp_tool_calls`. Metadata: request type, report type, tools, category predicted, human correction, latency, errors. Fully optional.

## 6.3 Known Limitations (honest list)
- **Sync DB inside async code:** acceptable at this scale; move to `asyncio.to_thread` or an async driver if load grows.
- **Per-process caches/dedupe/cooldowns:** run a single app instance, or move dedupe to the database/Redis before scaling horizontally.
- **MCP spawn per report:** ~1–2 s overhead; switch to a persistent session or streamable-HTTP transport if report volume rises.
- **Schema migration is hand-rolled** (`ensure_schema`) — adopt Alembic for future changes.
- **Free LLM tiers** are heavily rate-limited; add more providers/keys for reliability.
- **Single-currency totals:** other currencies are reported as excluded, not converted.
- **Local-disk receipts** require a shared volume if the app is ever replicated (implement another `StorageProvider`).
- The integration/Telegram/MCP paths were authored without being executable in the authoring environment; run `pytest -q` and a live smoke test (`/start`, a receipt, a report) after installation.

## 6.4 Extension Recipes
| Goal | Change |
|---|---|
| Add/rename a category | Edit `ExpenseCategory` and `CATEGORY_DESCRIPTIONS` in `schemas/enums.py` (prompts, buttons, validation follow automatically; existing rows keep their old strings). |
| Add a provider | Add a branch in `llm_provider.build_chat_model`, a key check in `_provider_has_key`, model settings + `Settings.models_for` mapping. |
| Add a report type | Add a `ReportIntent`, a function in `reporting/tools.py` + `TOOLS`, an MCP wrapper in `server.py`, a branch in `plan_tool_calls` and a renderer in `_text_for`. |
| Add a correctable field | Add to `CORRECTABLE_FIELDS`, an `<field>_ai` column, a case in `parse_correction`/prompt, and a prompt entry in `feedback_flow.PROMPTS`. |
| Change report scope | `REPORTS_SCOPE=user` (no code change). |
| Use object storage | Implement `StorageProvider` and pass it to `StorageService`. |
| Run on a laptop without a public URL | Leave `TELEGRAM_WEBHOOK_URL` empty (long-polling). |
