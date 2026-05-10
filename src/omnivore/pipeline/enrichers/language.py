from __future__ import annotations

from functools import lru_cache

from lingua import Language, LanguageDetectorBuilder

# Build detector once at import — it loads n-gram models into memory.
# Using all languages is thorough but slow; restrict to a common set for speed.
_SUPPORTED = [
    Language.ENGLISH, Language.SPANISH, Language.FRENCH, Language.GERMAN,
    Language.PORTUGUESE, Language.ITALIAN, Language.DUTCH, Language.RUSSIAN,
    Language.CHINESE, Language.JAPANESE, Language.KOREAN, Language.ARABIC,
    Language.TURKISH, Language.POLISH, Language.SWEDISH, Language.BOKMAL,
]


@lru_cache(maxsize=1)
def _detector():
    return (
        LanguageDetectorBuilder
        .from_languages(*_SUPPORTED)
        .with_minimum_relative_distance(0.1)
        .build()
    )


def detect_language(text: str) -> str | None:
    """Return ISO 639-1 code (e.g. 'en') or None if confidence is too low."""
    if not text or len(text.strip()) < 20:
        return None
    result = _detector().detect_language_of(text)
    if result is None:
        return None
    return result.iso_code_639_1.name.lower()
