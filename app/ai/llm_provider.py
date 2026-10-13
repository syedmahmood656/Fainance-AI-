"""One place that knows how to talk to OpenRouter, Gemini and Hugging Face.

Design (kept deliberately small):

* A *candidate* is (provider, model). For every call we build an ordered list
  of candidates and try them until one returns valid JSON.
* A model that fails is put on a short "cooldown" so the next request does not
  waste time on it (rate limit -> 5 min, 404/no endpoint -> 1 h, ...).
* OpenRouter free models come and go, so we optionally ask OpenRouter which
  models are free *right now* (cached for 30 minutes).
* We ask the model for JSON in the prompt and validate it with Pydantic.
  Free models rarely support tool-calling / JSON mode reliably, so this is the
  most portable approach.

Switching providers is purely configuration:  LLM_PROVIDER_ORDER=gemini,openrouter
"""
import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

import httpx
from pydantic import BaseModel

from app.ai.llm_utils import (
    BadModelOutput,
    classify_error,
    cooldown_seconds,
    extract_json,
    message_text,
    select_free_models,
    to_data_url,
)
from app.config.settings import settings
from app.utils.logging import logger
from app.utils.tracing import set_trace_metadata, traced

try:  # used to keep raw model calls (which contain images) out of LangSmith
    from langsmith import tracing_context as _tracing_context
except Exception:  # noqa: BLE001
    _tracing_context = None

HF_ROUTER_URL = "https://router.huggingface.co/v1"
OPENROUTER_ROUTER_MODEL = "openrouter/free"  # OpenRouter's own "pick any free model" router


class LLMUnavailableError(RuntimeError):
    """Every configured model failed. The message is safe to show to the user."""


@dataclass(frozen=True)
class Candidate:
    provider: str
    model: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class LLMCallInfo:
    provider: str = ""
    model: str = ""
    attempts: int = 0
    latency_ms: int = 0
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    failures: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider, "model": self.model, "attempts": self.attempts,
            "latency_ms": self.latency_ms, "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


# --------------------------------------------------------------------------
# Cooldowns (in-memory; resets on restart, which is fine)
# --------------------------------------------------------------------------
_cooldown_until: Dict[str, float] = {}


def _is_cooling(key: str) -> bool:
    return _cooldown_until.get(key, 0) > time.monotonic()


def _mark_failed(candidate: Candidate, kind: str) -> None:
    seconds = cooldown_seconds(kind, settings.LLM_MODEL_COOLDOWN_SECONDS)
    if seconds <= 0:
        return
    if kind == "auth":  # a bad key breaks the whole provider, not one model
        _cooldown_until[f"{candidate.provider}:*"] = time.monotonic() + seconds
    else:
        _cooldown_until[candidate.key] = time.monotonic() + seconds


def reset_cooldowns() -> None:
    _cooldown_until.clear()


# --------------------------------------------------------------------------
# OpenRouter free-model discovery
# --------------------------------------------------------------------------
_CATALOG_TTL = 30 * 60
_catalog_cache: Dict[str, Tuple[float, List[str]]] = {}


async def _discover_openrouter_free(vision: bool) -> List[str]:
    cache_key = "vision" if vision else "text"
    cached = _catalog_cache.get(cache_key)
    if cached and time.monotonic() - cached[0] < _CATALOG_TTL:
        return cached[1]
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.OPENROUTER_BASE_URL}/models")
            resp.raise_for_status()
            models = select_free_models(resp.json(), vision=vision)
        _catalog_cache[cache_key] = (time.monotonic(), models)
        logger.info("OpenRouter free %s models discovered: %s", cache_key, models)
        return models
    except Exception as exc:  # noqa: BLE001
        logger.warning("OpenRouter model discovery failed (%s); using configured models only", exc)
        _catalog_cache[cache_key] = (time.monotonic() - _CATALOG_TTL + 120, [])  # retry in ~2 min
        return []


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


async def _models_for_provider(provider: str, vision: bool) -> List[str]:
    configured = settings.models_for(provider, vision)
    if provider != "openrouter":
        return configured
    discovered = await _discover_openrouter_free(vision) if settings.OPENROUTER_DISCOVER_FREE_MODELS else []
    if vision:
        return _dedupe(configured + discovered + [OPENROUTER_ROUTER_MODEL])
    return _dedupe(configured + [OPENROUTER_ROUTER_MODEL] + discovered)


def _provider_has_key(provider: str) -> bool:
    return bool({
        "openrouter": settings.OPENROUTER_API_KEY,
        "gemini": settings.gemini_key,
        "huggingface": settings.hf_token,
    }.get(provider))


async def get_candidates(vision: bool) -> List[Candidate]:
    """Ordered candidates for one call: healthy ones first, cooling ones last."""
    healthy: List[Candidate] = []
    cooling: List[Candidate] = []
    for provider in settings.provider_order:
        if not _provider_has_key(provider):
            continue
        models = await _models_for_provider(provider, vision)
        for model in models[: settings.LLM_MAX_MODELS_PER_PROVIDER]:
            cand = Candidate(provider, model)
            if _is_cooling(cand.key) or _is_cooling(f"{provider}:*"):
                cooling.append(cand)
            else:
                healthy.append(cand)
    return healthy + cooling


# --------------------------------------------------------------------------
# Building LangChain chat models (imports are lazy: a missing optional
# package only breaks that one provider, not the whole app)
# --------------------------------------------------------------------------
_model_cache: Dict[str, Any] = {}


def build_chat_model(candidate: Candidate):
    if candidate.key in _model_cache:
        return _model_cache[candidate.key]

    if candidate.provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        model = ChatGoogleGenerativeAI(
            model=candidate.model,
            google_api_key=settings.gemini_key,
            temperature=0,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,  # we do our own fallback; internal retries just burn quota
        )
    else:
        from langchain_openai import ChatOpenAI

        if candidate.provider == "openrouter":
            headers = {}
            if settings.OPENROUTER_SITE_URL:
                headers["HTTP-Referer"] = settings.OPENROUTER_SITE_URL
            if settings.OPENROUTER_APP_NAME:
                headers["X-Title"] = settings.OPENROUTER_APP_NAME
            base_url, api_key = settings.OPENROUTER_BASE_URL, settings.OPENROUTER_API_KEY
        elif candidate.provider == "huggingface":
            headers, base_url, api_key = {}, HF_ROUTER_URL, settings.hf_token
        else:
            raise ValueError(f"Unknown provider: {candidate.provider}")

        model = ChatOpenAI(
            model=candidate.model,
            api_key=api_key,
            base_url=base_url,
            default_headers=headers or None,
            temperature=0,
            # An explicit cap matters: without it OpenRouter reserves the model's
            # maximum (often 32k-65k) and answers 402 "requires more credits, or fewer max_tokens".
            max_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            max_retries=0,
        )
    _model_cache[candidate.key] = model
    return model


def _no_tracing():
    """Context manager that disables LangSmith for the wrapped block (never raises)."""
    if _tracing_context is None:
        return contextlib.nullcontext()
    try:
        return _tracing_context(enabled=False)
    except Exception:  # noqa: BLE001
        return contextlib.nullcontext()


def _build_messages(system: str, user_text: str, image_jpeg: Optional[bytes]):
    from langchain_core.messages import HumanMessage, SystemMessage

    if image_jpeg:
        content: Any = [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": to_data_url(image_jpeg)}},
        ]
    else:
        content = user_text
    return [SystemMessage(content=system), HumanMessage(content=content)]


def _usage(response: Any) -> Tuple[Optional[int], Optional[int]]:
    usage = getattr(response, "usage_metadata", None) or {}
    return usage.get("input_tokens"), usage.get("output_tokens")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
class ResilientLLM:
    """`await llm.ainvoke_json(...)` -> (validated pydantic object, call info)."""

    @traced("llm_json_call", run_type="llm")
    async def ainvoke_json(
        self,
        *,
        task: str,
        system: str,
        user_text: str,
        schema: Type[BaseModel],
        image_jpeg: Optional[bytes] = None,
        accept: Optional[Callable[[BaseModel], None]] = None,
    ) -> Tuple[BaseModel, LLMCallInfo]:
        """`accept` may raise BadModelOutput to reject a technically-valid but useless answer."""
        vision = image_jpeg is not None
        candidates = await get_candidates(vision)
        if not candidates:
            raise LLMUnavailableError(
                "No AI provider is configured. Set OPENROUTER_API_KEY, GEMINI_API_KEY or "
                "HUGGINGFACEHUB_API_TOKEN in your .env file."
            )

        messages = _build_messages(system, user_text, image_jpeg)
        info = LLMCallInfo()
        started = time.perf_counter()

        for candidate in candidates:
            if info.attempts >= settings.LLM_MAX_MODEL_ATTEMPTS:
                break
            info.attempts += 1
            attempt_started = time.perf_counter()
            try:
                chat_model = build_chat_model(candidate)
                # Keep the raw model call out of LangSmith: it contains the image.
                with _no_tracing():
                    response = await asyncio.wait_for(
                        chat_model.ainvoke(messages), timeout=settings.LLM_TIMEOUT_SECONDS + 10
                    )
                parsed = schema.model_validate(extract_json(message_text(response)))
                if accept is not None:
                    accept(parsed)
            except asyncio.TimeoutError as exc:
                self._record_failure(info, candidate, exc, "timeout", attempt_started)
                continue
            except Exception as exc:  # noqa: BLE001 - any failure -> try next model
                kind = classify_error(exc) if not _looks_like_validation_error(exc) else "bad_output"
                self._record_failure(info, candidate, exc, kind, attempt_started)
                continue

            info.provider, info.model = candidate.provider, candidate.model
            info.latency_ms = int((time.perf_counter() - started) * 1000)
            info.input_tokens, info.output_tokens = _usage(response)
            logger.info("LLM ok task=%s %s/%s attempts=%d %dms", task, candidate.provider,
                        candidate.model, info.attempts, info.latency_ms)
            set_trace_metadata(task=task, vision=vision, **info.as_dict(), failures=info.failures)
            return parsed, info

        info.latency_ms = int((time.perf_counter() - started) * 1000)
        set_trace_metadata(task=task, vision=vision, all_failed=True, **info.as_dict(), failures=info.failures)
        raise LLMUnavailableError(_friendly_failure(info))

    @staticmethod
    def _record_failure(info: LLMCallInfo, cand: Candidate, exc: BaseException, kind: str, started: float) -> None:
        ms = int((time.perf_counter() - started) * 1000)
        info.failures.append(f"{cand.key}:{kind}")
        _mark_failed(cand, kind)
        # Log the class + a short message, never the prompt/image.
        logger.warning("LLM fail task-attempt %s kind=%s after %dms: %s", cand.key, kind, ms,
                       str(exc)[:200].replace("\n", " "))


def _looks_like_validation_error(exc: BaseException) -> bool:
    return exc.__class__.__name__ == "ValidationError"


def _friendly_failure(info: LLMCallInfo) -> str:
    kinds = {f.rsplit(":", 1)[-1] for f in info.failures}
    if kinds and kinds <= {"rate_limit", "credits"}:
        return "The AI models are rate-limited right now. Please try again in a minute."
    if "auth" in kinds and len(kinds) == 1:
        return "The AI provider rejected the API key. Please check the keys in your .env file."
    return "I couldn't get a usable answer from any AI model just now. Please try again shortly."


llm = ResilientLLM()
