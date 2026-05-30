"""
Lightweight i18n manager for the public dashboard.
Loads static JSON locale files on first access and caches them in memory.
Falls back to English for any missing key or unsupported language.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

_LOCALES_DIR = Path(__file__).parent.parent / "locales"
_log = logging.getLogger(__name__)

# RTL language codes
RTL_LANGS: frozenset[str] = frozenset({"ar", "ur"})

# Supported languages: code → display name (native)
SUPPORTED_LANGUAGES: List[Dict[str, str]] = [
    {"code": "en",  "name": "English"},
    {"code": "zh",  "name": "中文"},
    {"code": "hi",  "name": "हिन्दी"},
    {"code": "es",  "name": "Español"},
    {"code": "pt",  "name": "Português"},
    {"code": "ru",  "name": "Русский"},
    {"code": "vi",  "name": "Tiếng Việt"},
    {"code": "km",  "name": "ខ្មែរ"},
    {"code": "id",  "name": "Bahasa Indonesia"},
    {"code": "ja",  "name": "日本語"},
    {"code": "ko",  "name": "한국어"},
    {"code": "tr",  "name": "Türkçe"},
    {"code": "de",  "name": "Deutsch"},
    {"code": "fr",  "name": "Français"},
    {"code": "it",  "name": "Italiano"},
    {"code": "ar",  "name": "العربية"},
    {"code": "th",  "name": "ภาษาไทย"},
    {"code": "fil", "name": "Filipino"},
    {"code": "pl",  "name": "Polski"},
    {"code": "uk",  "name": "Українська"},
    {"code": "bn",  "name": "বাংলা"},
    {"code": "ur",  "name": "اردو"},
]

_SUPPORTED_CODES: frozenset[str] = frozenset(
    lang["code"] for lang in SUPPORTED_LANGUAGES
)


@lru_cache(maxsize=22)
def load_locale(lang: str) -> Dict[str, str]:
    """Load and cache the JSON locale file for *lang*. Falls back to English."""
    code = lang.lower().strip() if lang else "en"
    if code not in _SUPPORTED_CODES:
        code = "en"
    path = _LOCALES_DIR / f"{code}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log.warning("i18n: could not load locale %s: %s", code, exc)
        if code != "en":
            return load_locale("en")
        return {}


def translate(key: str, lang: str = "en") -> str:
    """
    Return the translated string for *key* in *lang*.
    Falls back to English if the key is missing in the requested locale.
    Returns the key itself if not found anywhere.
    """
    locale = load_locale(lang)
    if key in locale:
        return locale[key]
    if lang != "en":
        en = load_locale("en")
        if key in en:
            return en[key]
    return key


def is_rtl(lang: str) -> bool:
    """Return True for right-to-left languages (Arabic, Urdu)."""
    return lang.lower() in RTL_LANGS
