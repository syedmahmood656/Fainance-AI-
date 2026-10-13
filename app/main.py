import asyncio
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config.settings import settings
from app.utils.tracing import setup_tracing

setup_tracing()  # must run before the first traced call

from app.database.models import init_db  # noqa: E402
from app.services.expense_service import expense_service  # noqa: E402
from app.utils.logging import logger  # noqa: E402


def _cleanup_old_report_files(max_age_hours: int = 24) -> None:
    folder = settings.REPORTS_TMP_DIR
    if not os.path.isdir(folder):
        return
    cutoff = time.time() - max_age_hours * 3600
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def _use_webhook() -> bool:
    mode = settings.TELEGRAM_MODE.lower()
    return mode == "webhook" or (mode == "auto" and bool(settings.TELEGRAM_WEBHOOK_URL))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.telegram.bot import ALLOWED_UPDATES, bot_instance
    from app.telegram.polling import run_polling

    logger.info("Initializing database...")
    await asyncio.to_thread(init_db)
    expired = await asyncio.to_thread(expense_service.expire_stale)
    if expired:
        logger.info("Expired %d stale pending receipt(s)", expired)
    _cleanup_old_report_files()

    polling_task = None
    if _use_webhook():
        url = settings.TELEGRAM_WEBHOOK_URL.rstrip("/")
        if not url.endswith("/telegram/webhook"):
            url += "/telegram/webhook"
        try:
            await bot_instance.bot.set_webhook(
                url=url, allowed_updates=ALLOWED_UPDATES,
                secret_token=settings.TELEGRAM_WEBHOOK_SECRET or None)
            logger.info("Telegram webhook set to %s", url)
        except Exception as exc:  # noqa: BLE001
            logger.error("Could not set webhook: %s", exc)
    else:
        polling_task = asyncio.create_task(run_polling())

    yield

    if polling_task:
        polling_task.cancel()
        try:
            await polling_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="AI Expense Tracker API", lifespan=lifespan)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Acknowledge immediately; process in the background.

    Returning an error (or taking too long) makes Telegram re-send the same update
    again and again, which is what caused duplicate processing and cascading 500s.
    """
    from app.telegram.bot import schedule_update

    secret = settings.TELEGRAM_WEBHOOK_SECRET
    if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
        return JSONResponse({"status": "forbidden"}, status_code=403)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"status": "ignored"})
    schedule_update(data)
    return JSONResponse({"status": "ok"})
