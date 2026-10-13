"""Central configuration. Everything is read from environment variables / .env."""
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split(",") if v.strip()]


class Settings(BaseSettings):
    # ---------------- Telegram ----------------
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_WEBHOOK_URL: str = ""
    # auto = webhook if TELEGRAM_WEBHOOK_URL is set, otherwise long-polling (no public URL needed)
    TELEGRAM_MODE: str = "auto"
    # Optional shared secret Telegram sends back in a header (setWebhook secret_token)
    TELEGRAM_WEBHOOK_SECRET: str = ""
    # Comma separated Telegram user ids, e.g. "1158704160,222333444".
    # (Kept as a string on purpose: pydantic-settings would try to JSON-decode a List field.)
    ALLOWED_TELEGRAM_USER_IDS: str = ""

    # ---------------- LLM providers ----------------
    # Provider order tried for each call. The first provider that has an API key
    # and a working model wins; failures fall through to the next model/provider.
    LLM_PROVIDER_ORDER: str = "openrouter,gemini,huggingface"

    OPENROUTER_API_KEY: str | None = None
    GEMINI_API_KEY: str | None = None
    GOOGLE_API_KEY: str | None = None  # alias for GEMINI_API_KEY
    HUGGINGFACEHUB_API_TOKEN: str | None = None
    HF_TOKEN: str | None = None  # alias for HUGGINGFACEHUB_API_TOKEN

    # Comma separated, tried in order. Empty = use built-in defaults / discovery.
    # "openrouter/free" is OpenRouter's own router across currently-free models.
    OPENROUTER_TEXT_MODELS: str = ""
    OPENROUTER_VISION_MODELS: str = ""
    GEMINI_TEXT_MODELS: str = "gemini-2.5-flash,gemini-2.5-flash-lite"
    GEMINI_VISION_MODELS: str = "gemini-2.5-flash,gemini-2.5-flash-lite"
    HF_TEXT_MODELS: str = "Qwen/Qwen2.5-72B-Instruct,meta-llama/Llama-3.3-70B-Instruct"
    HF_VISION_MODELS: str = "Qwen/Qwen2.5-VL-72B-Instruct"

    # Ask OpenRouter /models for currently-free (and free+vision) models and try
    # them before the static defaults.
    OPENROUTER_DISCOVER_FREE_MODELS: bool = True
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    OPENROUTER_SITE_URL: str = ""
    OPENROUTER_APP_NAME: str = "Ledger Finance AI"

    # Token / size safety (fixes the "token limit" errors)
    LLM_MAX_OUTPUT_TOKENS: int = 1024
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_MODEL_ATTEMPTS: int = 6  # total model attempts per call, across providers
    LLM_MAX_MODELS_PER_PROVIDER: int = 3  # so one provider can't use up every attempt
    LLM_MODEL_COOLDOWN_SECONDS: int = 300  # skip a model that just failed
    IMAGE_MAX_SIDE_PX: int = 1280
    IMAGE_JPEG_QUALITY: int = 80

    # ---------------- LangSmith (optional) ----------------
    LANGSMITH_API_KEY: str | None = None
    LANGCHAIN_API_KEY: str | None = None  # old name, still supported
    LANGSMITH_TRACING: bool | None = None
    LANGCHAIN_TRACING_V2: bool | None = None  # old name, still supported
    LANGSMITH_PROJECT: str | None = None
    LANGCHAIN_PROJECT: str = "expense-tracker"
    # Images, API keys and raw Telegram IDs are NEVER sent to LangSmith.
    # Set this to true to also redact message text (vendor names, amounts, queries).
    LANGSMITH_REDACT_CONTENT: bool = False

    # ---------------- Storage ----------------
    DATABASE_URL: str
    RECEIPTS_DIR: str = "receipts"
    REPORTS_TMP_DIR: str = "reports_tmp"
    DEFAULT_CURRENCY: str = "INR"

    # ---------------- Reporting / MCP ----------------
    # true  = reporting tools are called through the MCP server (stdio subprocess)
    # false = the same tool functions are called in-process (handy for debugging)
    # organization = reports/Excel/receipt lookups cover ALL authorized users' confirmed expenses
    # user         = each person only sees the expenses they submitted themselves
    REPORTS_SCOPE: str = "organization"
    USE_MCP: bool = True
    MCP_CALL_TIMEOUT_SECONDS: int = 90

    # ---------------- Conversation state ----------------
    PENDING_EXPENSE_TTL_MINUTES: int = 1440  # unconfirmed receipts expire after 24h
    PENDING_EDIT_TTL_MINUTES: int = 15  # "type the new value" prompts expire

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @field_validator("*", mode="before")
    @classmethod
    def clean_env_strings(cls, v, info):
        # Docker's env_file keeps literal quotes (KEY="value"); python-dotenv strips them.
        # Also: an empty value (KEY=) means "use the default" instead of failing validation.
        if isinstance(v, str):
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1].strip()
            elif v and v[0] in "\"'" and v.count(v[0]) == 1:
                v = v[1:].strip()
            if v == "":
                field = cls.model_fields.get(info.field_name)
                if field is not None and not field.is_required():
                    return field.get_default(call_default_factory=True)
        return v

    # -------- convenience accessors --------
    @property
    def allowed_user_ids(self) -> List[int]:
        raw = self.ALLOWED_TELEGRAM_USER_IDS.strip().strip("[]")
        return [int(part) for part in raw.replace(";", ",").split(",") if part.strip().lstrip("-").isdigit()]

    @property
    def gemini_key(self) -> str | None:
        return self.GEMINI_API_KEY or self.GOOGLE_API_KEY

    @property
    def hf_token(self) -> str | None:
        return self.HUGGINGFACEHUB_API_TOKEN or self.HF_TOKEN

    @property
    def provider_order(self) -> List[str]:
        return [p.lower() for p in _split_csv(self.LLM_PROVIDER_ORDER)]

    @property
    def langsmith_enabled(self) -> bool:
        key = self.LANGSMITH_API_KEY or self.LANGCHAIN_API_KEY
        flag = self.LANGSMITH_TRACING
        if flag is None:
            flag = self.LANGCHAIN_TRACING_V2
        return bool(key) and bool(flag)

    @property
    def langsmith_project(self) -> str:
        return self.LANGSMITH_PROJECT or self.LANGCHAIN_PROJECT

    def models_for(self, provider: str, vision: bool) -> List[str]:
        attr = {
            ("openrouter", False): "OPENROUTER_TEXT_MODELS",
            ("openrouter", True): "OPENROUTER_VISION_MODELS",
            ("gemini", False): "GEMINI_TEXT_MODELS",
            ("gemini", True): "GEMINI_VISION_MODELS",
            ("huggingface", False): "HF_TEXT_MODELS",
            ("huggingface", True): "HF_VISION_MODELS",
        }[(provider, vision)]
        return _split_csv(getattr(self, attr))


settings = Settings()
