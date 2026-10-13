"""Long-polling mode: works on a laptop with no public URL (no ngrok needed).

Used automatically when TELEGRAM_WEBHOOK_URL is empty (TELEGRAM_MODE=auto).
"""
import asyncio

from telegram.error import TelegramError

from app.telegram.bot import ALLOWED_UPDATES, bot_instance, schedule_update
from app.utils.logging import logger


async def run_polling() -> None:
    bot = bot_instance.bot
    try:
        await bot.delete_webhook(drop_pending_updates=False)  # polling and webhooks are mutually exclusive
    except TelegramError as exc:
        logger.warning("delete_webhook failed: %s", exc)
    logger.info("Telegram long-polling started")
    offset = None
    while True:
        try:
            updates = await bot.get_updates(offset=offset, timeout=25, read_timeout=40,
                                            allowed_updates=ALLOWED_UPDATES)
            for update in updates:
                offset = update.update_id + 1
                schedule_update(update.to_dict())
        except asyncio.CancelledError:
            logger.info("Telegram long-polling stopped")
            raise
        except Exception as exc:  # noqa: BLE001 - network blips: back off and retry
            logger.warning("Polling error: %s", exc)
            await asyncio.sleep(3)
