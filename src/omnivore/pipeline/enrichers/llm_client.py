"""Provider-agnostic LLM client for text completion and vision description.

Supported providers (configured via settings.LLM_PROVIDER):
  anthropic — Claude via Anthropic SDK (requires ANTHROPIC_API_KEY)
  openai    — GPT via OpenAI SDK (requires OPENAI_API_KEY)
  google    — Gemini via Google's OpenAI-compatible endpoint (requires GOOGLE_API_KEY)
  ollama    — Any local model via Ollama's OpenAI-compatible endpoint (no key needed)

Model selection (per provider):
  LLM_TEXT_MODEL / LLM_VISION_MODEL override the defaults below.
  Leave empty to use the provider default.
"""
from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import structlog
from anthropic import AsyncAnthropic
from anthropic.types import TextBlock
from openai import AsyncOpenAI

if TYPE_CHECKING:
    from omnivore.config import Settings

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider defaults
# ---------------------------------------------------------------------------

_TEXT_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "google":    "gemini-2.0-flash",
    "ollama":    "qwen2.5:7b",
}
_VISION_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
    "openai":    "gpt-4o-mini",
    "google":    "gemini-2.0-flash",
    "ollama":    "qwen2.5-vl:7b",
}
_GOOGLE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"

# ---------------------------------------------------------------------------
# Module-level singletons — constructed once per process, reusing TCP pools
# ---------------------------------------------------------------------------

_anthropic_singleton: AsyncAnthropic | None = None
_openai_singletons: dict[str, AsyncOpenAI] = {}


def _get_anthropic(settings: Settings) -> AsyncAnthropic:
    global _anthropic_singleton
    if _anthropic_singleton is None:
        if not settings.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY not set")
        _anthropic_singleton = AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY.get_secret_value(),
            timeout=120.0,
        )
    return _anthropic_singleton


def _get_openai_client(settings: Settings) -> AsyncOpenAI:
    p = settings.LLM_PROVIDER
    if p not in _openai_singletons:
        _openai_singletons[p] = _build_openai_client(settings)
    return _openai_singletons[p]


def _build_openai_client(settings: Settings) -> AsyncOpenAI:
    p = settings.LLM_PROVIDER
    if p == "openai":
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY not set")
        return AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value(), timeout=120.0)
    if p == "google":
        if not settings.GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY not set")
        return AsyncOpenAI(
            api_key=settings.GOOGLE_API_KEY.get_secret_value(),
            base_url=_GOOGLE_BASE_URL,
            timeout=120.0,
        )
    if p == "ollama":
        return AsyncOpenAI(api_key="ollama", base_url=f"{settings.OLLAMA_BASE_URL}/v1", timeout=120.0)
    raise ValueError(f"Unknown LLM provider: {p!r}")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def llm_configured(settings: Settings) -> bool:
    """True when the configured provider has the credentials/endpoint it needs."""
    p = settings.LLM_PROVIDER
    if p == "anthropic":
        return bool(settings.ANTHROPIC_API_KEY)
    if p == "openai":
        return bool(settings.OPENAI_API_KEY)
    if p == "google":
        return bool(settings.GOOGLE_API_KEY)
    if p == "ollama":
        return True  # always attempt; fail at request time if Ollama not running
    return False


async def complete_text(prompt: str, *, settings: Settings) -> str | None:
    """Send a text prompt to the configured provider. Returns response text or None."""
    provider = settings.LLM_PROVIDER
    model = settings.LLM_TEXT_MODEL or _TEXT_DEFAULTS.get(provider, "")
    try:
        if provider == "anthropic":
            return await _anthropic_text(prompt, model=model, settings=settings)
        return await _openai_compat_text(prompt, model=model, settings=settings)
    except Exception:
        logger.warning("llm_client.complete_text.failed", provider=provider, model=model, exc_info=True)
        return None


async def complete_vision(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    *,
    settings: Settings,
) -> str | None:
    """Describe an image via the configured provider. Returns caption or None."""
    provider = settings.LLM_PROVIDER
    model = settings.LLM_VISION_MODEL or _VISION_DEFAULTS.get(provider, "")
    try:
        if provider == "anthropic":
            return await _anthropic_vision(image_bytes, mime_type, prompt, model=model, settings=settings)
        return await _openai_compat_vision(image_bytes, mime_type, prompt, model=model, settings=settings)
    except Exception:
        logger.warning("llm_client.complete_vision.failed", provider=provider, model=model, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Anthropic backend
# ---------------------------------------------------------------------------

async def _anthropic_text(prompt: str, *, model: str, settings: Settings) -> str:
    client = _get_anthropic(settings)
    msg = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    blocks = [b for b in msg.content if isinstance(b, TextBlock)]
    return blocks[0].text.strip() if blocks else ""


async def _anthropic_vision(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    *,
    model: str,
    settings: Settings,
) -> str:
    b64 = base64.standard_b64encode(image_bytes).decode()
    client = _get_anthropic(settings)
    msg = await client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime_type, "data": b64},
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )
    blocks = [b for b in msg.content if isinstance(b, TextBlock)]
    return blocks[0].text.strip() if blocks else ""


# ---------------------------------------------------------------------------
# OpenAI-compatible backend  (openai · google · ollama)
# ---------------------------------------------------------------------------

async def _openai_compat_text(prompt: str, *, model: str, settings: Settings) -> str:
    client = _get_openai_client(settings)
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


async def _openai_compat_vision(
    image_bytes: bytes,
    mime_type: str,
    prompt: str,
    *,
    model: str,
    settings: Settings,
) -> str:
    b64 = base64.standard_b64encode(image_bytes).decode()
    client = _get_openai_client(settings)
    resp = await client.chat.completions.create(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return (resp.choices[0].message.content or "").strip()
