# Installation and Usage Guide — AI Expense Tracker

## 1. Prerequisites
* **Python 3.11+** (tested on 3.13) · **Git** · a **Telegram account**
* Optional: **Docker Desktop** (for PostgreSQL / production-style run)

## 2. Setup (Windows PowerShell)
```powershell
cd C:\Projects\Ledger-Finance-AI
python -m venv .venv
.\.venv\Scripts\Activate.ps1          # the prompt must now start with (.venv)
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```
> If activation is blocked: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
> **Always work inside the activated `(.venv)`** — outside it, imports such as `telegram` fail.

Fill in `.env`:

| Key | What to put |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from @BotFather (`/newbot`, or `/revoke` for a fresh one) |
| `ALLOWED_TELEGRAM_USER_IDS` | your numeric id, comma-separated for several people. Don't know it? Message the bot: the "not authorized" reply shows your id |
| `OPENROUTER_API_KEY` / `GEMINI_API_KEY` / `HUGGINGFACEHUB_API_TOKEN` | at least one (two is better: free tiers rate-limit) |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5433/expense_db` (after `docker compose up -d db`) **or** `sqlite:///./expenses.db` for a quick local test — run everything from the project folder |
| `TELEGRAM_WEBHOOK_URL` | leave **empty** locally (long-polling). Set your public https URL in production |

## 3. Check your AI models first (30 seconds, saves hours)
```powershell
python -m scripts.check_llm --image "C:\path\to\a\receipt.jpg"
```
You need at least one `OK` vision model. Model names change; if one says `not_found`, put a current name in `GEMINI_VISION_MODELS` / `OPENROUTER_VISION_MODELS`.

## 4. Run
```powershell
uvicorn app.main:app --port 8000        # or: python -m app.main
```
Do not use `--reload` on Windows (it breaks the reporting subprocess).
Docker alternative: `docker compose up --build -d` then `docker compose logs -f app`.

## 5. Use it in Telegram
| You do | Result |
|---|---|
| send a receipt photo | card → ✅ Confirm / ✏️ Correct |
| `/add` | add an expense without a receipt (amount → category → description) |
| `/cancel` | stop any "type the value" prompt |
| "Show me September expenses by category" | text + bar chart |
| "Show me expenses over time" | line graph (daily/weekly/monthly) |
| "Download the September Excel report" | `.xlsx` |
| "Show me the receipt for the ₹2,450 expense" | the stored image |
| send a `.csv` (date, vendor, amount, currency, category, description) | bulk import |

## 6. Tests
```powershell
pytest -q
```
No network, keys or Docker needed (SQLite + fake LLM).

## 7. Measure accuracy (for your résumé)
See **README → Evaluation**: `python -m scripts.export_eval_dataset --stats` (live usage) and `python -m scripts.eval_receipts` (labelled receipts).

## 8. Troubleshooting
| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: telegram` | you are outside the venv — activate `.venv` |
| "couldn't read this receipt" + a *Reason* line | run `python -m scripts.check_llm` |
| Bot silent | check the console; is your id in `ALLOWED_TELEGRAM_USER_IDS`? |
| Old receipts show old category names | categories changed — see README → *Changing categories* |
