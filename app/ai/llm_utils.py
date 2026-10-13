"""Pure helper functions for the LLM layer (no LangChain imports, easy to test)."""
import base64
import io
import json
import re
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# Output parsing
# --------------------------------------------------------------------------
class BadModelOutput(ValueError):
    """The model answered, but not with usable JSON."""


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def message_text(response: Any) -> str:
    """LangChain message -> plain text. Handles str and list-of-blocks content."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content or "")


def extract_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Free models often wrap JSON in ```fences```, add chatter, or emit <think>
    blocks. We strip all of that and scan for the first balanced {...}.
    """
    if not text or not text.strip():
        raise BadModelOutput("empty response")
    cleaned = _THINK_RE.sub("", text).strip()
    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(cleaned)):
            ch = cleaned[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = cleaned[start : i + 1]
                    try:
                        data = json.loads(candidate)
                        if isinstance(data, dict):
                            return data
                    except json.JSONDecodeError:
                        pass
                    break
        start = cleaned.find("{", start + 1)
    raise BadModelOutput("no JSON object found in response")


# --------------------------------------------------------------------------
# Error classification -> decides how long a failing model is skipped
# --------------------------------------------------------------------------
def _status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "code"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val
    resp = getattr(exc, "response", None)
    val = getattr(resp, "status_code", None)
    return val if isinstance(val, int) else None


def classify_error(exc: BaseException) -> str:
    """Return one of: rate_limit, not_found, auth, forbidden, credits, context, timeout, empty, bad_output, other."""
    msg = str(exc).lower()
    if isinstance(exc, BadModelOutput):
        # an EMPTY reply is a property of the model (e.g. a reasoning model that spent all its
        # tokens thinking), unlike malformed JSON which depends on the particular receipt
        return "empty" if "empty" in msg else "bad_output"
    code = _status_code(exc)

    if any(s in msg for s in ("context length", "context_length", "maximum context", "too many tokens",
                              "token limit", "tokens exceed", "max_tokens", "prompt is too long",
                              "reduce the length", "input too long")):
        # "fewer max_tokens" on a 402 is a credits problem, not a context problem
        if code == 402 or "requires more credits" in msg or "can only afford" in msg:
            return "credits"
        return "context"
    if code == 429 or any(s in msg for s in ("rate limit", "rate-limit", "too many requests", "quota",
                                             "resource_exhausted", "resource exhausted")):
        return "rate_limit"
    if code == 402 or any(s in msg for s in ("insufficient credit", "requires more credits", "payment required")):
        return "credits"
    if code == 401 or any(s in msg for s in ("invalid api key", "api key not valid", "api_key_invalid",
                                             "incorrect api key", "unauthorized", "user not found")):
        return "auth"  # the KEY is bad -> the whole provider is unusable
    if code == 403 or any(s in msg for s in ("permission denied", "only available on", "not available in your")):
        return "forbidden"  # this ONE model is not allowed for us; other models of the provider may work
    if code == 404 or any(s in msg for s in ("no endpoints found", "model not found", "is not found",
                                             "not a valid model", "does not exist", "no allowed providers")):
        return "not_found"
    if any(s in msg for s in ("timeout", "timed out")) or code in (408, 504):
        return "timeout"
    return "other"


# How long (seconds) a model should be skipped after each kind of failure.
# `base` is settings.LLM_MODEL_COOLDOWN_SECONDS.
def cooldown_seconds(kind: str, base: int) -> int:
    return {
        "rate_limit": base,
        "credits": base * 6,
        "not_found": base * 12,
        "forbidden": base * 12,
        "auth": base * 12,
        "empty": max(60, base // 2),
        "timeout": max(30, base // 5),
        "other": max(30, base // 5),
        "context": 0,      # input-specific, not the model's fault
        "bad_output": 0,   # input-specific
    }.get(kind, base)


# --------------------------------------------------------------------------
# OpenRouter free model discovery
# --------------------------------------------------------------------------
_SKIP_MODEL_WORDS = ("embed", "guard", "moderation", "safeguard", "whisper", "tts", "audio", "image-gen",
                     "lyria", "veo", "imagen")


def _is_free(model: Dict[str, Any]) -> bool:
    model_id = str(model.get("id", ""))
    if model_id.endswith(":free"):
        return True
    pricing = model.get("pricing") or {}
    try:
        return float(pricing.get("prompt", 1)) == 0 and float(pricing.get("completion", 1)) == 0
    except (TypeError, ValueError):
        return False


def select_free_models(payload: Dict[str, Any], vision: bool, limit: int = 6) -> List[str]:
    """Pick currently-free models from OpenRouter's /models response.

    Larger context windows first (they are more tolerant of long prompts).
    """
    picked = []
    for model in payload.get("data", []):
        model_id = str(model.get("id", ""))
        if not model_id or any(w in model_id.lower() for w in _SKIP_MODEL_WORDS):
            continue
        if not _is_free(model):
            continue
        arch = model.get("architecture") or {}
        inputs = arch.get("input_modalities") or ["text"]
        outputs = arch.get("output_modalities") or ["text"]
        if "text" not in outputs:
            continue
        if vision and "image" not in inputs:
            continue
        if not vision and "text" not in inputs:
            continue
        picked.append((int(model.get("context_length") or 0), model_id))
    picked.sort(key=lambda t: (-t[0], t[1]))
    return [mid for _, mid in picked[:limit]]


# --------------------------------------------------------------------------
# Image preparation (keeps vision requests small -> avoids token/size errors)
# --------------------------------------------------------------------------
def prepare_image_for_llm(raw: bytes, max_side: int = 1280, quality: int = 80) -> bytes:
    """Decode, fix rotation, shrink and re-encode as JPEG.

    A 12-megapixel phone photo can cost 5-10x more tokens (and trips provider
    size limits) than the same receipt downscaled to ~1280px.
    """
    from PIL import Image, ImageOps  # imported lazily so other helpers stay light

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Not a readable image: {exc}") from exc

    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > max_side:
        scale = max_side / longest
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def to_data_url(jpeg_bytes: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")


def detect_image_extension(raw: bytes) -> str:
    """'.jpg' / '.png' / '.webp' based on the actual bytes (not the filename)."""
    from PIL import Image

    try:
        fmt = (Image.open(io.BytesIO(raw)).format or "").upper()
    except Exception:  # noqa: BLE001
        return ".jpg"
    return {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}.get(fmt, ".jpg")
