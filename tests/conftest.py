"""Test environment: SQLite file DB, temp folders, no API keys, no tracing, no MCP subprocess."""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="ledger_tests_")

os.environ.update({
    "TELEGRAM_BOT_TOKEN": "123456:TEST-TOKEN",
    "DATABASE_URL": f"sqlite:///{_TMP}/test.db",
    "RECEIPTS_DIR": f"{_TMP}/receipts",
    "REPORTS_TMP_DIR": f"{_TMP}/reports",
    "ALLOWED_TELEGRAM_USER_IDS": "111,222",
    "USE_MCP": "false",
    "LANGSMITH_TRACING": "false",
    "LANGCHAIN_TRACING_V2": "false",
    "REPORTS_SCOPE": "organization",
    "LLM_PROVIDER_ORDER": "openrouter,gemini,huggingface",
    "OPENROUTER_API_KEY": "test-openrouter-key",
    "GEMINI_API_KEY": "test-gemini-key",
    "OPENROUTER_DISCOVER_FREE_MODELS": "false",
})
# Empty value = "use the default" (None / built-in list). Environment variables beat a real .env,
# so a developer's own keys and model lists can never leak into the tests.
for _name in ("HUGGINGFACEHUB_API_TOKEN", "HF_TOKEN", "GOOGLE_API_KEY", "LANGSMITH_API_KEY", "LANGCHAIN_API_KEY",
              "OPENROUTER_TEXT_MODELS", "OPENROUTER_VISION_MODELS", "GEMINI_TEXT_MODELS", "GEMINI_VISION_MODELS",
              "HF_TEXT_MODELS", "HF_VISION_MODELS", "LLM_MAX_MODELS_PER_PROVIDER", "LLM_MAX_MODEL_ATTEMPTS"):
    os.environ[_name] = ""
