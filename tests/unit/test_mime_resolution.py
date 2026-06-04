"""Unit tests for _effective_mime — extension-aware MIME refinement on upload.

Regression coverage for the smoke-harness finding: libmagic sniffs .xlsx as
application/zip and .md as text/plain, so handler resolution must fall back to
the filename extension for those generic types (else xlsx uploads fail and
markdown is processed by the plain-text handler).
"""
from __future__ import annotations

from omnivore.api.routes.documents import _effective_mime
from omnivore.pipeline.registry import registry

OOXML_SHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class TestEffectiveMime:
    @classmethod
    def setup_class(cls):
        registry.discover()  # idempotent; ensures handlers are registered

    def test_xlsx_zip_refined_to_spreadsheet(self):
        assert _effective_mime("application/zip", "report.xlsx") == OOXML_SHEET

    def test_md_textplain_refined_to_markdown(self):
        assert _effective_mime("text/plain", "notes.md") == "text/markdown"

    def test_csv_textplain_refined_to_csv(self):
        assert _effective_mime("text/plain", "data.csv") == "text/csv"

    def test_specific_mime_trusts_magic(self):
        # A mislabeled .txt that is really a PDF must keep magic's content detection.
        assert _effective_mime("application/pdf", "actually.txt") == "application/pdf"

    def test_generic_with_unknown_ext_unchanged(self):
        # A genuine .zip has no handler — stays application/zip and fails gracefully.
        assert _effective_mime("application/zip", "archive.zip") == "application/zip"

    def test_no_filename_unchanged(self):
        assert _effective_mime("text/plain", None) == "text/plain"

    def test_no_extension_unchanged(self):
        assert _effective_mime("application/zip", "noext") == "application/zip"
