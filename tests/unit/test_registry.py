"""Handler registry unit tests — MIME dispatch, glob patterns, registration."""
from __future__ import annotations

from omnivore.pipeline.registry import HandlerRegistry


class _FakeHandler:
    name = "fake"
    version = "1.0.0"
    accepts = ("application/x-fake",)
    cost_class = "io"
    timeout_seconds = 30

    async def extract(self, blob, ctx):  # pragma: no cover
        raise NotImplementedError


class _GlobHandler:
    name = "text_glob"
    version = "1.0.0"
    accepts = ("text/*",)
    cost_class = "io"
    timeout_seconds = 30

    async def extract(self, blob, ctx):  # pragma: no cover
        raise NotImplementedError


class TestHandlerRegistry:
    def test_register_then_resolve_exact_mime(self):
        reg = HandlerRegistry()
        reg.register(_FakeHandler)
        assert reg.resolve("application/x-fake") is _FakeHandler

    def test_resolve_unknown_mime_returns_none(self):
        reg = HandlerRegistry()
        assert reg.resolve("application/totally-unknown") is None

    def test_resolve_unknown_after_registration_returns_none(self):
        reg = HandlerRegistry()
        reg.register(_FakeHandler)
        assert reg.resolve("application/not-fake") is None

    def test_resolve_glob_pattern_matches_subtype(self):
        reg = HandlerRegistry()
        reg.register(_GlobHandler)
        assert reg.resolve("text/plain") is _GlobHandler
        assert reg.resolve("text/html") is _GlobHandler
        assert reg.resolve("text/csv") is _GlobHandler

    def test_glob_does_not_match_different_type(self):
        reg = HandlerRegistry()
        reg.register(_GlobHandler)
        assert reg.resolve("application/json") is None

    def test_all_handlers_lists_registered(self):
        reg = HandlerRegistry()
        reg.register(_FakeHandler)
        reg.register(_GlobHandler)
        handlers = reg.all_handlers()
        names = {h["name"] for h in handlers}
        assert "fake" in names
        assert "text_glob" in names

    def test_all_handlers_empty_registry(self):
        reg = HandlerRegistry()
        assert reg.all_handlers() == []

    def test_register_overwrites_same_name(self):
        class _Updated:
            name = "fake"
            version = "2.0.0"
            accepts = ("application/x-fake",)
            cost_class = "cpu"
            timeout_seconds = 60

            async def extract(self, blob, ctx):  # pragma: no cover
                raise NotImplementedError

        reg = HandlerRegistry()
        reg.register(_FakeHandler)
        reg.register(_Updated)
        assert reg.resolve("application/x-fake") is _Updated

    def test_all_handlers_returns_correct_fields(self):
        reg = HandlerRegistry()
        reg.register(_FakeHandler)
        info = reg.all_handlers()[0]
        assert info["name"] == "fake"
        assert info["version"] == "1.0.0"
        assert "application/x-fake" in info["accepts"]
        assert info["cost_class"] == "io"
