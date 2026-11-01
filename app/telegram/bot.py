"""Telegram is only the user interface: it turns updates into AgentState, runs the
LangGraph workflow, and delivers the result (text, buttons, charts, Excel, receipts).

Reliability rules that fix the old 500 errors / duplicate processing:
* the webhook returns 200 immediately; work happens in a background task
* an update_id is processed at most once (Telegram retries slow webhooks)
* downloads use the Bot API (no token-bearing URLs in state, logs or traces)
* a small semaphore limits concurrent LLM-heavy requests (free-tier rate limits)
"""
import asyncio
import os
import tempfile
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Set

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TelegramError
from telegram.request import HTTPXRequest

from app.agent.graph import HELP_TEXT, run_agent
from app.agent.state import AgentState
from app.ai.llm_utils import detect_image_extension
from app.config.settings import settings
from app.services.storage_service import storage_service
from app.utils.formatting import truncate_for_telegram
from app.utils.logging import logger

START_TEXT = "Welcome to the AI Expense Tracker! 📊\n\n" + HELP_TEXT
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
ALLOWED_UPDATES = ["message", "callback_query"]

CALLBACK_ACTIONS = {
    "cf": "confirm", "co": "correct", "cp": "field", "cc": "set_category",
    "ct": "set_type", "cd": "discard", "cb": "back",
}


class TelegramBot:
    def __init__(self):
        request = HTTPXRequest(connection_pool_size=8, connect_timeout=15, read_timeout=60,
                               write_timeout=60, pool_timeout=15)
        self.bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, request=request)

    @staticmethod
    def _markup(buttons) -> Optional[InlineKeyboardMarkup]:
        if not buttons:
            return None
        return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in row]
                                     for row in buttons])

    async def send_message(self, chat_id: int, text: str, buttons=None) -> None:
        await self.bot.send_message(chat_id=chat_id, text=truncate_for_telegram(text),
                                    reply_markup=self._markup(buttons))

    async def edit_message(self, chat_id: int, message_id: int, text: str, buttons=None) -> None:
        try:
            await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id,
                                             text=truncate_for_telegram(text), reply_markup=self._markup(buttons))
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def answer_callback(self, callback_id: str, text: Optional[str] = None) -> None:
        try:
            await self.bot.answer_callback_query(callback_query_id=callback_id, text=text)
        except TelegramError as exc:  # expired callbacks are harmless
            logger.debug("answer_callback failed: %s", exc)

    async def send_photo(self, chat_id: int, path: str, caption: Optional[str] = None) -> None:
        try:
            with open(path, "rb") as fh:
                await self.bot.send_photo(chat_id=chat_id, photo=fh, caption=caption)
        except BadRequest as exc:
            # e.g. image too large/odd dimensions for a "photo": send it as a file instead
            logger.info("send_photo rejected (%s); sending as document", exc)
            await self.send_document(chat_id, path, caption)

    async def send_document(self, chat_id: int, path: str, caption: Optional[str] = None) -> None:
        with open(path, "rb") as fh:
            await self.bot.send_document(chat_id=chat_id, document=fh, caption=caption,
                                         filename=os.path.basename(path))

    async def download(self, file_id: str) -> bytes:
        file = await self.bot.get_file(file_id)
        if file.file_size and file.file_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("That file is too large (limit 20 MB).")
        return bytes(await file.download_as_bytearray())

    async def typing(self, chat_id: int) -> None:
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action="typing")
        except TelegramError:
            pass


bot_instance = TelegramBot()

# --------------------------------------------------------------------------
# de-duplication + concurrency control
# --------------------------------------------------------------------------
_seen_updates: "OrderedDict[int, None]" = OrderedDict()
_background_tasks: Set[asyncio.Task] = set()
_llm_slots = asyncio.Semaphore(2)


def _already_seen(update_id: Optional[int]) -> bool:
    if update_id is None:
        return False
    if update_id in _seen_updates:
        return True
    _seen_updates[update_id] = None
    while len(_seen_updates) > 2000:
        _seen_updates.popitem(last=False)
    return False


def schedule_update(update_data: Dict[str, Any]) -> None:
    """Fire-and-forget: used by the webhook so Telegram gets its 200 right away."""
    task = asyncio.create_task(handle_update_safely(update_data))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def handle_update_safely(update_data: Dict[str, Any]) -> None:
    try:
        await handle_telegram_update(update_data)
    except Exception as exc:  # noqa: BLE001 - never let one update crash the loop
        logger.error("Unhandled error processing update: %s", exc, exc_info=True)


# --------------------------------------------------------------------------
# main entry
# --------------------------------------------------------------------------
def _is_authorized(user_id: Optional[int]) -> bool:
    return user_id is not None and user_id in settings.allowed_user_ids


def _new_state(user_id: int, input_type: str, **extra) -> AgentState:
    state: AgentState = {"user_id": user_id, "input_type": input_type, "message_text": None, "receipt_ref": None,
                         "csv_file_path": None, "feedback": None}
    state.update(extra)  # type: ignore[typeddict-item]
    return state


async def handle_telegram_update(update_data: dict) -> None:
    if _already_seen(update_data.get("update_id")):
        logger.info("Ignoring duplicate update %s", update_data.get("update_id"))
        return
    if update_data.get("callback_query"):
        await _handle_callback(update_data["callback_query"])
    elif update_data.get("message"):
        await _handle_message(update_data["message"])


async def _handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    user_id = message.get("from", {}).get("id")
    if chat_id is None:
        return

    if not _is_authorized(user_id):
        logger.warning("Unauthorized access attempt by user %s", user_id)
        await bot_instance.send_message(
            chat_id, f"❌ You are not authorized to use this bot.\nYour Telegram ID is {user_id}. "
                     "Ask the administrator to add it to ALLOWED_TELEGRAM_USER_IDS.")
        return

    text = (message.get("text") or "").strip()
    command = text.split()[0].split("@")[0].lower() if text.startswith("/") else None
    if command == "/start":
        await bot_instance.send_message(chat_id, START_TEXT)
        return
    if command == "/help":
        await bot_instance.send_message(chat_id, HELP_TEXT)
        return

    state: Optional[AgentState] = None
    receipt_ref: Optional[str] = None
    try:
        if message.get("photo") or _is_image_document(message.get("document")):
            file_id = message["photo"][-1]["file_id"] if message.get("photo") else message["document"]["file_id"]
            await bot_instance.send_message(chat_id, "⏳ Reading your receipt…")
            raw = await bot_instance.download(file_id)
            receipt_ref = await storage_service.save_file(raw, detect_image_extension(raw))
            state = _new_state(user_id, "receipt", receipt_ref=receipt_ref)
        elif message.get("document"):
            doc = message["document"]
            if (doc.get("file_name") or "").lower().endswith(".csv") or doc.get("mime_type") == "text/csv":
                raw = await bot_instance.download(doc["file_id"])
                with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as tmp:
                    tmp.write(raw)
                state = _new_state(user_id, "csv", csv_file_path=tmp.name)
            else:
                await bot_instance.send_message(chat_id, "❌ I can read receipt images (JPG/PNG) and CSV files.")
                return
        elif command == "/add":
            state = _new_state(user_id, "manual_add", manual_event={"kind": "start"})
        elif command == "/cancel":
            state = _new_state(user_id, "feedback", feedback={"action": "cancel_edit"})
        elif text:
            await bot_instance.typing(chat_id)
            state = _new_state(user_id, "text", message_text=text)
        else:
            return
    except ValueError as exc:  # e.g. file too large
        await bot_instance.send_message(chat_id, f"⚠️ {exc}")
        return
    except TelegramError as exc:
        logger.error("Telegram download failed: %s", exc)
        await bot_instance.send_message(chat_id, "⚠️ I couldn't download that file from Telegram. Please try again.")
        return

    await _run_and_deliver(chat_id, state, receipt_ref=receipt_ref)


def _is_image_document(doc: Optional[dict]) -> bool:
    return bool(doc) and str(doc.get("mime_type", "")).startswith("image/")


async def _handle_callback(query: dict) -> None:
    callback_id = query.get("id")
    user_id = query.get("from", {}).get("id")
    message = query.get("message") or {}
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    if not _is_authorized(user_id) or chat_id is None:
        await bot_instance.answer_callback(callback_id, "Not authorized.")
        return

    parts = (query.get("data") or "").split(":")
    if parts[0] == "ma" and len(parts) == 2:        # category button of the /add wizard
        state = _new_state(user_id, "manual_add", manual_event={"kind": "category", "value": parts[1]})
        await _run_and_deliver(chat_id, state, callback_id=callback_id, message_id=message_id)
        return
    action = CALLBACK_ACTIONS.get(parts[0])
    try:
        expense_id = int(parts[1])
    except (IndexError, ValueError):
        action = None
    if action is None:
        await bot_instance.answer_callback(callback_id, "Unknown button.")
        return

    feedback = {"action": action, "expense_id": expense_id, "value": parts[2] if len(parts) > 2 else None}
    state = _new_state(user_id, "feedback", feedback=feedback)
    await _run_and_deliver(chat_id, state, callback_id=callback_id, message_id=message_id)


async def _run_and_deliver(chat_id: int, state: AgentState, receipt_ref: Optional[str] = None,
                           callback_id: Optional[str] = None, message_id: Optional[int] = None) -> None:
    final: Dict[str, Any] = {}
    try:
        async with _llm_slots:
            final = await run_agent(state)
        await _deliver(chat_id, final, message_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("Agent execution error: %s", exc, exc_info=True)
        final = {"error": "x"}
        try:
            await bot_instance.send_message(chat_id, "❌ Something went wrong while processing that. Please try again.")
        except TelegramError:
            pass
    finally:
        if callback_id:
            await bot_instance.answer_callback(callback_id, final.get("toast"))
        # the receipt image was stored but no expense was created -> don't keep an orphan file
        if receipt_ref and not final.get("expense_id"):
            await storage_service.delete_file(receipt_ref)
        _cleanup([*(final.get("chart_paths") or []), final.get("excel_file_path"), state.get("csv_file_path")])


async def _deliver(chat_id: int, final: Dict[str, Any], message_id: Optional[int]) -> None:
    if final.get("error"):
        await bot_instance.send_message(chat_id, f"⚠️ {final['error']}")
        return

    text, buttons = final.get("response_text") or "", final.get("buttons")
    if text:
        if final.get("edit_message") and message_id:
            try:
                await bot_instance.edit_message(chat_id, message_id, text, buttons)
            except TelegramError as exc:
                logger.info("edit failed (%s); sending a new message", exc)
                await bot_instance.send_message(chat_id, text, buttons)
        else:
            await bot_instance.send_message(chat_id, text, buttons)

    for path in final.get("chart_paths") or []:
        if _is_report_file(path):
            await bot_instance.send_photo(chat_id, path)
    excel = final.get("excel_file_path")
    if excel and _is_report_file(excel):
        await bot_instance.send_document(chat_id, excel, caption="Your expense report is ready.")
    for item in final.get("receipt_images") or []:
        await bot_instance.send_photo(chat_id, item["path"], caption=item.get("caption"))
        await asyncio.sleep(0.4)  # stay under Telegram's per-chat rate limit


def _is_report_file(path: str) -> bool:
    """Only send files our reporting tools wrote into REPORTS_TMP_DIR (defence in depth)."""
    base = os.path.abspath(settings.REPORTS_TMP_DIR) + os.sep
    return os.path.abspath(path).startswith(base) and os.path.isfile(path)


def _cleanup(paths: List[Optional[str]]) -> None:
    """Delete temp files we generated (charts, Excel, CSV). Never touches stored receipts."""
    reports_dir = os.path.abspath(settings.REPORTS_TMP_DIR)
    tmp_dir = os.path.abspath(tempfile.gettempdir())
    for path in paths:
        if not path:
            continue
        full = os.path.abspath(path)
        if full.startswith(reports_dir + os.sep) or full.startswith(tmp_dir + os.sep):
            try:
                os.remove(full)
            except OSError:
                pass
