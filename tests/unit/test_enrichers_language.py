from __future__ import annotations

from omnivore.pipeline.enrichers.language import detect_language


def test_detects_english():
    text = "The quick brown fox jumps over the lazy dog. This is a standard English sentence."
    assert detect_language(text) == "en"


def test_detects_spanish():
    text = "El rápido zorro marrón salta sobre el perro perezoso. Esta es una oración en español."
    result = detect_language(text)
    assert result == "es"


def test_detects_french():
    text = "Le renard brun rapide saute par-dessus le chien paresseux. C'est une phrase en français."
    result = detect_language(text)
    assert result == "fr"


def test_returns_none_for_empty():
    assert detect_language("") is None


def test_returns_none_for_short_text():
    # Fewer than 20 chars → no detection
    assert detect_language("hello") is None


def test_returns_none_for_whitespace():
    assert detect_language("   ") is None


def test_long_english_text():
    text = " ".join(["This is a test sentence in English."] * 10)
    assert detect_language(text) == "en"


def test_german_text():
    text = "Der schnelle braune Fuchs springt über den faulen Hund. Das ist ein Satz auf Deutsch."
    result = detect_language(text)
    assert result == "de"
