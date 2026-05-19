"""Regression tests for the API lifespan startup/shutdown sequence.

Covers the Phase 6 audit fixes:
- M-3: QUEUE_DEPTH gauges pre-initialised on startup
- M-5: provider.shutdown() called on graceful exit
- M-6: FastAPIInstrumentor gated on settings.OTEL_ENABLED
- C-2: lifespan raises RuntimeError on truncated JWT keys
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from prometheus_client import REGISTRY


@pytest.fixture
def reload_main(monkeypatch):
    """Force a clean reload of api.main with the current env."""
    import omnivore.api.main as m
    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    return m


async def test_lifespan_initialises_queue_depth_labels(reload_main, monkeypatch):
    """M-3: QUEUE_DEPTH must have queue='default' and queue='gpu' labels after startup."""
    # Provide a valid (long enough) fake key so the C-2 check passes
    fake_pem = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 1700) + "\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", fake_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    import importlib
    importlib.reload(reload_main)

    app = FastAPI()
    # Mock pieces that need real services
    from unittest.mock import AsyncMock
    pool_mock = AsyncMock()
    pool_mock.zcard = AsyncMock(return_value=0)
    monkeypatch.setattr(reload_main, "create_pool", AsyncMock(return_value=pool_mock))
    redis_mock = AsyncMock()
    monkeypatch.setattr(reload_main.aioredis, "from_url", lambda *a, **kw: redis_mock)
    monkeypatch.setattr(reload_main.registry, "discover", lambda: None)
    monkeypatch.setattr(reload_main.registry, "all_handlers", lambda: [])

    async with reload_main.lifespan(app):
        # After startup, both queue labels should produce a metric sample
        default_val = REGISTRY.get_sample_value(
            "omnivore_queue_depth", labels={"queue": "default"}
        )
        gpu_val = REGISTRY.get_sample_value(
            "omnivore_queue_depth", labels={"queue": "gpu"}
        )
        assert default_val is not None, "M-3 regression: queue=default not initialised"
        assert gpu_val is not None, "M-3 regression: queue=gpu not initialised"


async def test_lifespan_calls_provider_shutdown(reload_main, monkeypatch):
    """M-5: provider.shutdown() must be called when the lifespan context exits."""
    fake_pem = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 1700) + "\n-----END PRIVATE KEY-----"
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", fake_pem)
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    import importlib
    importlib.reload(reload_main)

    from unittest.mock import AsyncMock, MagicMock, patch
    pool_mock = AsyncMock()
    pool_mock.zcard = AsyncMock(return_value=0)
    monkeypatch.setattr(reload_main, "create_pool", AsyncMock(return_value=pool_mock))
    redis_mock = AsyncMock()
    monkeypatch.setattr(reload_main.aioredis, "from_url", lambda *a, **kw: redis_mock)
    monkeypatch.setattr(reload_main.registry, "discover", lambda: None)
    monkeypatch.setattr(reload_main.registry, "all_handlers", lambda: [])

    app = FastAPI()
    fake_provider = MagicMock()
    fake_provider.shutdown = MagicMock()
    with patch.object(reload_main.trace, "get_tracer_provider", return_value=fake_provider):
        async with reload_main.lifespan(app):
            pass

    assert fake_provider.shutdown.called, "M-5 regression: provider.shutdown() not called"


def test_lifespan_rejects_truncated_private_key(monkeypatch):
    """C-2: lifespan must raise RuntimeError when private PEM is too short."""
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", "-----BEGIN PRIVATE KEY-----")  # 27 chars
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    import importlib

    import omnivore.api.main as m
    importlib.reload(m)

    import asyncio
    async def _run():
        async with m.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY_PEM appears truncated"):
        asyncio.run(_run())


def test_lifespan_rejects_pem_without_begin_marker(monkeypatch):
    """C-2 tighter: a 200+ char value without BEGIN marker must be rejected."""
    # Long string but no PEM header — what you'd get pasting only base64 body
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", "A" * 500)
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    import importlib

    import omnivore.api.main as m
    importlib.reload(m)

    import asyncio
    async def _run():
        async with m.lifespan(FastAPI()):
            pass

    with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY_PEM appears truncated"):
        asyncio.run(_run())


def test_lifespan_accepts_empty_keys(monkeypatch):
    """C-2: empty JWT keys are allowed (JWT auth disabled) — must not raise."""
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", "")
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")
    monkeypatch.setenv("OTEL_ENABLED", "false")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()
    import importlib

    import omnivore.api.main as m
    importlib.reload(m)

    from unittest.mock import AsyncMock
    pool_mock = AsyncMock()
    pool_mock.zcard = AsyncMock(return_value=0)
    monkeypatch.setattr(m, "create_pool", AsyncMock(return_value=pool_mock))
    redis_mock = AsyncMock()
    monkeypatch.setattr(m.aioredis, "from_url", lambda *a, **kw: redis_mock)
    monkeypatch.setattr(m.registry, "discover", lambda: None)
    monkeypatch.setattr(m.registry, "all_handlers", lambda: [])

    import asyncio
    async def _run():
        async with m.lifespan(FastAPI()):
            pass

    # Should not raise
    asyncio.run(_run())


def test_is_valid_pem_helper():
    """Direct test of the _is_valid_pem helper."""
    import omnivore.api.main as m
    valid = "-----BEGIN PRIVATE KEY-----\n" + ("A" * 1700) + "\n-----END PRIVATE KEY-----"
    assert m._is_valid_pem(valid, min_length=200)
    assert not m._is_valid_pem("-----BEGIN PRIVATE KEY-----", min_length=200)  # too short
    assert not m._is_valid_pem("A" * 500, min_length=200)  # no markers
    assert not m._is_valid_pem("-----BEGIN PRIVATE KEY-----\nAAAA", min_length=200)  # missing END
    assert not m._is_valid_pem("", min_length=200)


def test_instrument_app_skipped_when_otel_disabled(monkeypatch):
    """M-6: FastAPIInstrumentor.instrument_app must not be called when OTEL_ENABLED=False."""
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", "")
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()

    from unittest.mock import patch
    with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app") as inst:
        import importlib

        import omnivore.api.main as m
        importlib.reload(m)
        # Module-level if-block ran during reload
        assert not inst.called, "M-6 regression: instrument_app called when OTEL_ENABLED=False"


def test_instrument_app_runs_when_otel_enabled(monkeypatch):
    """M-6: FastAPIInstrumentor.instrument_app must be called when OTEL_ENABLED=True."""
    monkeypatch.setenv("OTEL_ENABLED", "true")
    monkeypatch.setenv("JWT_PRIVATE_KEY_PEM", "")
    monkeypatch.setenv("JWT_PUBLIC_KEY_PEM", "")

    import omnivore.config as cfg
    cfg.get_settings.cache_clear()

    from unittest.mock import patch
    with patch("opentelemetry.instrumentation.fastapi.FastAPIInstrumentor.instrument_app") as inst:
        import importlib

        import omnivore.api.main as m
        importlib.reload(m)
        assert inst.called, "M-6 regression: instrument_app not called when OTEL_ENABLED=True"
