"""Unit tests for pipeline/enrichers/llm_client.py — all four providers, both SDKs mocked."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import omnivore.pipeline.enrichers.llm_client as _llm_mod
from omnivore.pipeline.enrichers.llm_client import (
    _GOOGLE_BASE_URL,
    _TEXT_DEFAULTS,
    _VISION_DEFAULTS,
    complete_text,
    complete_vision,
    llm_configured,
)

# ---------------------------------------------------------------------------
# Reset module-level singletons between tests so constructor patches work
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_llm_singletons():
    _llm_mod._anthropic_singleton = None
    _llm_mod._openai_singletons.clear()
    yield
    _llm_mod._anthropic_singleton = None
    _llm_mod._openai_singletons.clear()


# ---------------------------------------------------------------------------
# Settings factory
# ---------------------------------------------------------------------------


def _settings(
    provider: str = "anthropic",
    text_model: str = "",
    vision_model: str = "",
    anthropic_key: str | None = "ant-key",
    openai_key: str | None = None,
    google_key: str | None = None,
    ollama_url: str = "http://localhost:11434",
) -> MagicMock:
    s = MagicMock()
    s.LLM_PROVIDER = provider
    s.LLM_TEXT_MODEL = text_model
    s.LLM_VISION_MODEL = vision_model
    s.OLLAMA_BASE_URL = ollama_url

    def _secret(val):
        m = MagicMock()
        m.get_secret_value.return_value = val
        return m

    s.ANTHROPIC_API_KEY = _secret(anthropic_key) if anthropic_key else None
    s.OPENAI_API_KEY = _secret(openai_key) if openai_key else None
    s.GOOGLE_API_KEY = _secret(google_key) if google_key else None
    return s


# ---------------------------------------------------------------------------
# llm_configured
# ---------------------------------------------------------------------------


def test_llm_configured_anthropic_with_key():
    assert llm_configured(_settings(provider="anthropic", anthropic_key="sk")) is True


def test_llm_configured_anthropic_without_key():
    assert llm_configured(_settings(provider="anthropic", anthropic_key=None)) is False


def test_llm_configured_openai_with_key():
    assert llm_configured(_settings(provider="openai", anthropic_key=None, openai_key="sk")) is True


def test_llm_configured_openai_without_key():
    assert llm_configured(_settings(provider="openai", anthropic_key=None, openai_key=None)) is False


def test_llm_configured_google_with_key():
    assert llm_configured(_settings(provider="google", anthropic_key=None, google_key="gk")) is True


def test_llm_configured_google_without_key():
    assert llm_configured(_settings(provider="google", anthropic_key=None, google_key=None)) is False


def test_llm_configured_ollama_always_true():
    assert llm_configured(_settings(provider="ollama", anthropic_key=None)) is True


def test_llm_configured_unknown_provider_false():
    assert llm_configured(_settings(provider="unknown")) is False


# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------


def test_text_defaults_cover_all_providers():
    for p in ("anthropic", "openai", "google", "ollama"):
        assert p in _TEXT_DEFAULTS
        assert _TEXT_DEFAULTS[p]


def test_vision_defaults_cover_all_providers():
    for p in ("anthropic", "openai", "google", "ollama"):
        assert p in _VISION_DEFAULTS
        assert _VISION_DEFAULTS[p]


# ---------------------------------------------------------------------------
# complete_text — Anthropic
# ---------------------------------------------------------------------------


async def test_complete_text_anthropic_returns_text():
    from anthropic.types import TextBlock

    tb = MagicMock(spec=TextBlock)
    tb.text = "Summary of the document."
    resp = MagicMock()
    resp.content = [tb]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        result = await complete_text("Summarise this.", settings=_settings(provider="anthropic"))

    assert result == "Summary of the document."


async def test_complete_text_anthropic_uses_default_model():
    from anthropic.types import TextBlock

    tb = MagicMock(spec=TextBlock)
    tb.text = "ok"
    resp = MagicMock()
    resp.content = [tb]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        await complete_text("prompt", settings=_settings(provider="anthropic", text_model=""))

    assert mock_client.messages.create.call_args.kwargs["model"] == _TEXT_DEFAULTS["anthropic"]


async def test_complete_text_anthropic_uses_override_model():
    from anthropic.types import TextBlock

    tb = MagicMock(spec=TextBlock)
    tb.text = "ok"
    resp = MagicMock()
    resp.content = [tb]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        await complete_text("prompt", settings=_settings(provider="anthropic", text_model="claude-opus-4-7"))

    assert mock_client.messages.create.call_args.kwargs["model"] == "claude-opus-4-7"


async def test_complete_text_anthropic_missing_key_returns_none():
    result = await complete_text("prompt", settings=_settings(provider="anthropic", anthropic_key=None))
    assert result is None


async def test_complete_text_exception_returns_none():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        result = await complete_text("prompt", settings=_settings(provider="anthropic"))

    assert result is None


# ---------------------------------------------------------------------------
# complete_text — OpenAI-compatible (openai / google / ollama)
# ---------------------------------------------------------------------------


def _openai_text_response(text: str) -> MagicMock:
    choice = MagicMock()
    choice.message.content = text
    resp = MagicMock()
    resp.choices = [choice]
    return resp


async def test_complete_text_openai():
    resp = _openai_text_response("OpenAI answer.")
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", return_value=mock_client):
        result = await complete_text(
            "prompt", settings=_settings(provider="openai", anthropic_key=None, openai_key="sk")
        )

    assert result == "OpenAI answer."


async def test_complete_text_google_uses_google_base_url():
    resp = _openai_text_response("Gemini answer.")
    mock_cls = MagicMock()
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)
    mock_cls.return_value = mock_client

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", mock_cls):
        result = await complete_text(
            "prompt", settings=_settings(provider="google", anthropic_key=None, google_key="gk")
        )

    assert result == "Gemini answer."
    init_kwargs = mock_cls.call_args.kwargs
    assert init_kwargs["base_url"] == _GOOGLE_BASE_URL


async def test_complete_text_ollama_uses_ollama_base_url():
    resp = _openai_text_response("Ollama answer.")
    mock_cls = MagicMock()
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)
    mock_cls.return_value = mock_client

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", mock_cls):
        result = await complete_text(
            "prompt",
            settings=_settings(provider="ollama", anthropic_key=None, ollama_url="http://myhost:11434"),
        )

    assert result == "Ollama answer."
    init_kwargs = mock_cls.call_args.kwargs
    assert "myhost:11434" in init_kwargs["base_url"]


async def test_complete_text_ollama_default_model():
    resp = _openai_text_response("ok")
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", return_value=mock_client):
        await complete_text("prompt", settings=_settings(provider="ollama", anthropic_key=None))

    assert mock_client.chat.completions.create.call_args.kwargs["model"] == _TEXT_DEFAULTS["ollama"]


# ---------------------------------------------------------------------------
# complete_vision — message format verification
# ---------------------------------------------------------------------------


async def test_complete_vision_anthropic_uses_source_format():
    """Anthropic vision API uses content[0].source.data, not image_url."""
    import base64

    from anthropic.types import TextBlock

    img = b"fake-image"
    tb = MagicMock(spec=TextBlock)
    tb.text = "A scene."
    resp = MagicMock()
    resp.content = [tb]
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        await complete_vision(img, "image/jpeg", "describe", settings=_settings(provider="anthropic"))

    content = mock_client.messages.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content if b.get("type") == "image")
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["data"] == base64.standard_b64encode(img).decode()


async def test_complete_vision_openai_uses_image_url_format():
    """OpenAI-compatible providers use image_url with data URI."""
    import base64

    img = b"fake-image"
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="A scene."))]
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", return_value=mock_client):
        await complete_vision(
            img,
            "image/png",
            "describe",
            settings=_settings(provider="openai", anthropic_key=None, openai_key="sk"),
        )

    content = mock_client.chat.completions.create.call_args.kwargs["messages"][0]["content"]
    image_block = next(b for b in content if b.get("type") == "image_url")
    expected = f"data:image/png;base64,{base64.standard_b64encode(img).decode()}"
    assert image_block["image_url"]["url"] == expected


async def test_complete_vision_google_uses_image_url_format():
    """Google provider goes through OpenAI-compat path — same image_url format."""
    img = b"frame"
    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="Scene desc."))]
    mock_cls = MagicMock()
    mock_client = AsyncMock()
    mock_client.chat.completions.create = AsyncMock(return_value=resp)
    mock_cls.return_value = mock_client

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncOpenAI", mock_cls):
        result = await complete_vision(
            img,
            "image/jpeg",
            "describe",
            settings=_settings(provider="google", anthropic_key=None, google_key="gk"),
        )

    assert result == "Scene desc."
    assert mock_cls.call_args.kwargs["base_url"] == _GOOGLE_BASE_URL


async def test_complete_vision_exception_returns_none():
    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("network error"))

    with patch("omnivore.pipeline.enrichers.llm_client.AsyncAnthropic", return_value=mock_client):
        result = await complete_vision(b"img", "image/jpeg", "desc", settings=_settings())

    assert result is None
