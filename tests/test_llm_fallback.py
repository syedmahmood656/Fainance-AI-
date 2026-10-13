"""Provider switching: fallback order, bad JSON, rate limits, cooldowns, vision payload."""
import asyncio
from types import SimpleNamespace as NS

import pytest

from app.ai import llm_provider
from app.ai.llm_provider import LLMUnavailableError, llm, reset_cooldowns
from app.config.settings import settings
from app.schemas.expense import CategorizationResult

GOOD = '{"category": "Food & Meals", "item": "Team Dinner", "goods_or_service": "Service"}'


class RateLimit(Exception):
    status_code = 429


class FakeModel:
    def __init__(self, name, behaviour, log):
        self.name, self.behaviour, self.log = name, behaviour, log

    async def ainvoke(self, messages):
        self.log.append((self.name, messages))
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return NS(content=self.behaviour, usage_metadata={"input_tokens": 10, "output_tokens": 5})


@pytest.fixture(autouse=True)
def clean():
    reset_cooldowns()
    llm_provider._model_cache.clear()
    yield
    reset_cooldowns()


def install(monkeypatch, behaviours):
    """behaviours: {'provider:model': str | Exception}"""
    calls = []
    monkeypatch.setattr(llm_provider, "build_chat_model",
                        lambda cand: FakeModel(cand.key, behaviours.get(cand.key, RateLimit("rate limit")), calls))
    return calls


def run(**kw):
    return asyncio.run(llm.ainvoke_json(task="t", system="s", user_text="u", schema=CategorizationResult, **kw))


def test_falls_through_rate_limit_then_bad_json_to_a_working_model(monkeypatch):
    calls = install(monkeypatch, {
        "openrouter:openrouter/free": RateLimit("429 rate limit"),
        "gemini:gemini-2.5-flash": "I cannot help with that",
        "gemini:gemini-2.5-flash-lite": GOOD,
    })
    result, info = run()
    assert result.category.value == "Food & Meals"
    assert (info.provider, info.model, info.attempts) == ("gemini", "gemini-2.5-flash-lite", 3)
    assert [c[0] for c in calls] == ["openrouter:openrouter/free", "gemini:gemini-2.5-flash",
                                    "gemini:gemini-2.5-flash-lite"]


def test_rate_limited_model_is_skipped_on_the_next_request(monkeypatch):
    calls = install(monkeypatch, {"openrouter:openrouter/free": RateLimit("429"), "gemini:gemini-2.5-flash": GOOD})
    run()
    calls.clear()
    run()
    assert calls[0][0] == "gemini:gemini-2.5-flash"   # openrouter is cooling down, so it is tried last


def test_all_models_failing_gives_a_friendly_error(monkeypatch):
    install(monkeypatch, {})
    with pytest.raises(LLMUnavailableError) as exc:
        run()
    assert "rate-limited" in str(exc.value)


def test_provider_order_is_just_configuration(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER_ORDER", "gemini,openrouter")
    calls = install(monkeypatch, {"gemini:gemini-2.5-flash": GOOD})
    run()
    assert calls[0][0] == "gemini:gemini-2.5-flash"


def test_provider_without_key_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "GOOGLE_API_KEY", None)
    calls = install(monkeypatch, {"openrouter:openrouter/free": GOOD})
    run()
    assert all(not c[0].startswith("gemini") for c in calls)


def test_image_is_sent_as_image_url_part(monkeypatch):
    calls = install(monkeypatch, {"openrouter:openrouter/free": GOOD})
    run(image_jpeg=b"\xff\xd8fakejpeg")
    human = calls[0][1][1]
    assert isinstance(human.content, list) and human.content[1]["type"] == "image_url"
    assert human.content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_unknown_category_from_model_is_rejected_and_next_model_used(monkeypatch):
    install(monkeypatch, {"openrouter:openrouter/free": '{"category": "Zakath"}', "gemini:gemini-2.5-flash": GOOD})
    result, info = run()
    assert info.provider == "gemini"
