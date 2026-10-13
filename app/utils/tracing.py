"""Optional LangSmith tracing.

* Tracing is OFF unless a LangSmith API key AND the tracing flag are set.
* If the `langsmith` package is not installed, everything here becomes a no-op.
* Receipt images, API keys and raw Telegram user IDs are never sent.
  Set LANGSMITH_REDACT_CONTENT=true to also hide message text.

Usage:
    @traced("receipt_extraction", run_type="llm")
    async def extract(...): ...

    set_trace_metadata(category_predicted="Travel", extraction_success=True)
"""
import hashlib
import os
import re
from typing import Any, Callable, Optional

from app.config.settings import settings
from app.utils.logging import logger

_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z+.-]+;base64,[A-Za-z0-9+/=]+")
_SECRET_RE = re.compile(r"(bot\d{6,}:[A-Za-z0-9_-]{20,})|(sk-[A-Za-z0-9_-]{16,})|(AIza[0-9A-Za-z_-]{20,})|(hf_[A-Za-z0-9]{20,})")

try:  # langsmith is optional
    from langsmith import traceable as _ls_traceable
    from langsmith.run_helpers import get_current_run_tree as _get_run_tree
    _LANGSMITH_AVAILABLE = True
except Exception:  # noqa: BLE001
    _ls_traceable = None
    _get_run_tree = None
    _LANGSMITH_AVAILABLE = False


def setup_tracing() -> bool:
    """Export the right env vars. MUST run before the first traced call.

    Returns True if tracing is active. We always set the flags explicitly:
    an empty key with LANGCHAIN_TRACING_V2=true (as in the old .env) would
    otherwise make LangSmith spam 401/403 warnings on every request.
    """
    enabled = settings.langsmith_enabled and _LANGSMITH_AVAILABLE
    flag = "true" if enabled else "false"
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        os.environ[name] = flag
    if enabled:
        key = settings.LANGSMITH_API_KEY or settings.LANGCHAIN_API_KEY
        os.environ["LANGSMITH_API_KEY"] = key
        os.environ["LANGCHAIN_API_KEY"] = key
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
        os.environ["LANGCHAIN_PROJECT"] = settings.langsmith_project
        logger.info("LangSmith tracing ENABLED (project=%s)", settings.langsmith_project)
    else:
        logger.info("LangSmith tracing disabled")
    return enabled


def tracing_enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "false").lower() == "true" and _LANGSMITH_AVAILABLE


def hash_user_id(user_id: Any) -> str:
    return hashlib.sha256(f"ledger:{user_id}".encode()).hexdigest()[:10]


def _scrub(value: Any, depth: int = 0) -> Any:
    """Remove images/secrets from anything that is about to be traced."""
    if depth > 6:
        return "<nested>"
    if isinstance(value, str):
        text = _DATA_URL_RE.sub("<image omitted>", value)
        text = _SECRET_RE.sub("<secret omitted>", text)
        if settings.LANGSMITH_REDACT_CONTENT and len(text) > 0:
            return f"<redacted {len(text)} chars>"
        return text
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes omitted>"
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key in ("user_id", "telegram_user_id", "chat_id"):
                out[key] = hash_user_id(v)
            elif key.lower() in ("image", "image_bytes", "image_url", "api_key", "token"):
                out[key] = "<omitted>"
            else:
                out[key] = _scrub(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(v, depth + 1) for v in value[:50]]
    if hasattr(value, "model_dump"):
        try:
            return _scrub(value.model_dump(mode="json"), depth + 1)
        except Exception:  # noqa: BLE001
            return f"<{type(value).__name__}>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return f"<{type(value).__name__}>"


def _process_inputs(inputs: dict) -> dict:
    return _scrub(inputs)


def _process_outputs(outputs: Any) -> Any:
    return _scrub(outputs)


def traced(name: str, run_type: str = "chain", tags: Optional[list] = None) -> Callable:
    """Decorator: trace this function in LangSmith (no-op when disabled/not installed)."""

    def decorator(func: Callable) -> Callable:
        if not _LANGSMITH_AVAILABLE:
            return func
        try:
            return _ls_traceable(
                name=name,
                run_type=run_type,
                tags=tags or [],
                process_inputs=_process_inputs,
                process_outputs=_process_outputs,
            )(func)
        except TypeError:
            # Very old langsmith without process_inputs/process_outputs. We refuse to
            # trace (rather than risk sending images) and run the function untraced.
            logger.warning("langsmith too old to scrub payloads; %s runs untraced. "
                           "Upgrade with: pip install -U langsmith", name)
            return func

    return decorator


def set_trace_metadata(**metadata: Any) -> None:
    """Attach metadata (request type, category predicted, latency...) to the current run."""
    if not tracing_enabled():
        return
    try:
        run = _get_run_tree()
        if run is not None:
            run.add_metadata(_scrub(metadata))
    except Exception:  # noqa: BLE001
        pass


def current_run_id() -> Optional[str]:
    """ID of the active LangSmith run (stored with the expense so feedback can attach to it)."""
    if not tracing_enabled():
        return None
    try:
        run = _get_run_tree()
        return str(run.id) if run is not None else None
    except Exception:  # noqa: BLE001
        return None


def send_feedback(run_id: Optional[str], key: str, score: Optional[float], comment: Optional[str] = None,
                  value: Any = None) -> None:
    """Attach human feedback to a traced run. Silent no-op if tracing is off."""
    if not run_id or not tracing_enabled():
        return
    try:
        from langsmith import Client

        Client().create_feedback(
            run_id=run_id, key=key, score=score, value=value, comment=comment,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("LangSmith feedback failed: %s", exc)
