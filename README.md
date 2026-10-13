# Ledger Finance AI — Telegram expense tracker

Send a receipt photo → AI reads it → **you confirm or correct it** → it is stored in PostgreSQL.
Then ask for reports, charts, Excel files or the original receipt image in plain English.

```
Telegram ─► FastAPI ─► LangGraph ─┬─ receipt  : extract → validate → categorize → save (pending) → [✅ Confirm][✏️ Correct]
                                  ├─ feedback : confirm / correct field / discard
                                  ├─ report   : parse request → pick MCP tool → PostgreSQL → chart / Excel / text
                                  └─ receipts : parse request → PostgreSQL → stored image → Telegram
```

| Layer | Responsibility |
|---|---|
| LLM (OpenRouter / Gemini / Hugging Face) | read receipts, categorize, understand report requests — **never** does maths |
| LangGraph | routing + state (`app/agent/graph.py`) |
| MCP server | the controlled reporting/visualization tools (`app/mcp_server/server.py`) |
| PostgreSQL | source of truth, filtering, SUM/COUNT/GROUP BY |
| Python | validation, charts (Matplotlib), Excel (OpenPyXL), business rules |
| LangSmith | optional tracing + feedback scores |
| Telegram | the UI |

## Quick start

```bash
cp .env.example .env        # fill in TELEGRAM_BOT_TOKEN, ALLOWED_TELEGRAM_USER_IDS and ONE LLM key
docker compose up --build -d
docker compose logs -f app
```

* **No public URL needed for testing:** leave `TELEGRAM_WEBHOOK_URL` empty and the bot uses long-polling.
* **Production:** set `TELEGRAM_WEBHOOK_URL=https://your-domain` — the webhook is registered on startup.
* Running without Docker: start Postgres (`docker compose up -d db`), set `DATABASE_URL=postgresql://postgres:postgres@localhost:5433/expense_db`, then `pip install -r requirements.txt && uvicorn app.main:app`.

> `.env` values may be quoted or not — both work.

## Switching models / providers (OpenRouter, Gemini, Hugging Face)

Everything is configuration in `.env`; no code changes.

```env
LLM_PROVIDER_ORDER=openrouter,gemini,huggingface   # tried in this order; providers without a key are skipped
OPENROUTER_API_KEY=...        # any one key is enough
GEMINI_API_KEY=...
HUGGINGFACEHUB_API_TOKEN=...
```

For each LLM call the app builds a list of candidate models and tries them until one returns valid JSON:

* a model that fails (rate limit, 404, bad JSON, timeout…) is skipped for a cool-down period, so the next request doesn't waste time on it;
* **OpenRouter free models** — with `OPENROUTER_TEXT_MODELS` / `OPENROUTER_VISION_MODELS` empty, the app uses `openrouter/free` plus the models OpenRouter currently lists as free (and free *vision* models for receipts), refreshed every 30 minutes. Pin your own list to override: `OPENROUTER_VISION_MODELS=google/gemma-3-27b-it:free,openrouter/free`;
* vision (receipt) calls and text calls have separate model lists, because many free text models can't read images.

Free-tier reality check: OpenRouter's free models are heavily rate-limited (on the order of tens of requests per day without credits). One receipt = 2 LLM calls, one report request = 1. Adding a Gemini key as a second provider makes testing much smoother.

## Using the bot

| You send | You get |
|---|---|
| a receipt photo (or image file) | card with Vendor/Date/Category/Item/Type/Expense + **Confirm / Correct** |
| `Show me September expenses by category` | category totals + bar chart |
| `Show me a bar graph of this month's expenses by category` | bar chart |
| `Show me the last 6 months of expenses` | monthly totals + trend chart |
| `Pie chart of last month` / `Top 5 categories chart` | distribution / top-N chart |
| `Show me expenses over time` · `Daily expenses graph this month` · `Cumulative expense timeline last 3 months` · `Weekly expense graph for Travel in September` | **line graph of expense × time** (day/week/month buckets chosen automatically, or say daily/weekly/monthly; "cumulative" plots the running total) |
| `Download the September Excel report` · `Excel report for Travel in September` | `.xlsx` (Summary, Expenses, Category Summary, Monthly Summary) |
| `Show me the receipt for the ₹2,450 restaurant expense` · `Show me Travel receipts` | the stored receipt image(s) |
| a `.csv` (date, vendor, amount, currency, category, description) | bulk import |

**Correcting:** tap ✏️ Correct → choose a field. Category and Goods/Service use buttons; vendor, date, item and amount ask you to type the value (`cancel` to go back). Only **confirmed** expenses appear in reports, charts, Excel and receipt lookups; unconfirmed receipts expire after 24 h.

## Data model (PostgreSQL)

`expenses` — final values in plain columns (`vendor`, `expense_date`, `category`, `item`, `goods_or_service`, `expense_amount`, `currency`, `subtotal`, `tax`, `invoice_number`, `description`, `receipt_file_reference`, `telegram_user_id`, `status`, timestamps).
AI values are kept next to them and never overwritten: `category_ai`, `expense_amount_ai`, `vendor_ai`, `expense_date_ai`, `item_ai`, `goods_or_service_ai`, plus the full raw `ai_extraction` JSON and a `final_result` snapshot. `corrected_by_user` / `corrected_fields` say what the human changed.

`expense_corrections` — one row per corrected field: `field`, `ai_value`, `final_value`. This is the evaluation dataset.
`pending_edits` — "waiting for the user to type a value" (survives restarts).

An existing `expenses` table is upgraded automatically on startup (`amount`→`expense_amount`, new columns added, legacy rows become `confirmed`). Back up your database before the first run anyway.

## Feedback → evaluation (no automatic retraining)

```bash
python -m scripts.export_eval_dataset --stats                       # accuracy per field, common category mistakes
python -m scripts.export_eval_dataset --out eval/feedback.jsonl     # JSONL: AI output vs human-confirmed output
python -m scripts.export_eval_dataset --out eval/f.jsonl --langsmith expense-feedback   # also create a LangSmith dataset
```

## LangSmith (optional)

Set `LANGSMITH_API_KEY` and `LANGSMITH_TRACING=true`. Leave the key empty and nothing is sent; the app runs identically.
Traced: the whole request (`telegram_request`), `receipt_extraction`, `expense_categorization`, `report_request_parsing`, every LLM attempt/fallback (`llm_json_call`), `mcp_tool_calls`, and the human decision. Metadata includes request type, report type, tools selected, category predicted, whether a human corrected it, latency and errors.
Privacy: receipt images, API keys and the Telegram bot token are never sent; Telegram user ids are hashed. Set `LANGSMITH_REDACT_CONTENT=true` to also hide message text. On confirmation, per-field correctness scores (`category_correct`, `expense_amount_correct`, …) are attached to the trace.

## Reports scope

`REPORTS_SCOPE=organization` (default): reports/Excel/receipt lookups cover everyone in `ALLOWED_TELEGRAM_USER_IDS`. Set `user` for per-person views. Confirm/correct buttons always work only on your own receipts.
Totals are never added across currencies: the report uses `DEFAULT_CURRENCY` (or the most common one) and tells you how many expenses in other currencies were left out.

## Tests

```bash
pip install -r requirements.txt
pytest -q
```
Tests use SQLite and a fake LLM — no network, no API keys. `USE_MCP=false` is set automatically in tests so tools run in-process; set `USE_MCP=true` (default) in real use. Inspect the MCP server by hand with `npx @modelcontextprotocol/inspector python -m app.mcp_server.server`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| "AI models are rate-limited" | free tiers exhausted — add a second provider key or wait; reports still work via the keyword fallback parser |
| Webhook 500 / duplicate processing | fixed: the webhook now answers 200 immediately and de-duplicates `update_id` |
| Receipts not found after restart | `receipts_data` Docker volume now persists images |
| `Telegram 404/401` in Docker | remove stray quotes/spaces around `TELEGRAM_BOT_TOKEN` (now tolerated) |
| Nothing happens in Telegram | check your id is in `ALLOWED_TELEGRAM_USER_IDS`; look at `docker compose logs app` |

## Project layout

```
app/main.py                 FastAPI app, lifespan (DB init, webhook/polling)
app/telegram/               bot.py (UI), polling.py
app/agent/                  graph.py, state.py, feedback_flow.py, reporting_flow.py
app/ai/                     llm_provider.py (multi-provider fallback), receipt_extractor.py, report_rules.py, llm_utils.py
app/mcp_server/             server.py (FastMCP tools), client.py
app/reporting/              tools.py, charts.py, excel.py, periods.py
app/services/               expense_service.py, storage_service.py, validation.py, feedback.py
app/database/               models.py, repository.py
app/schemas/                enums.py, expense.py (Pydantic)
app/utils/                  tracing.py, logging.py, formatting.py, parsing.py
scripts/export_eval_dataset.py
tests/
```
