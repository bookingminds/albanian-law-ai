"""Albanian text normalizer for search.

Handles:
- ë/ç normalization (e/c ↔ ë/ç treated as equivalent)
- Common Albanian typos and dialect variations
- Legal term normalization
- Whitespace/punctuation cleanup
"""

import re
import unicodedata

# ── Character normalization maps ──────────────────────────────

_ALBANIAN_CHAR_MAP = {
    "ë": "e", "Ë": "E",
    "ç": "c", "Ç": "C",
}

# Reverse: add diacritical variants for search expansion
_DIACRITICAL_VARIANTS = {
    "e": ["ë", "e"],
    "c": ["ç", "c"],
}

# Common Albanian legal term normalization
# Maps various forms → canonical form for matching
_LEGAL_SYNONYMS = {
    "nene": "neni",
    "nenin": "neni",
    "nenit": "neni",
    "neneve": "neni",
    "ligji": "ligj",
    "ligjin": "ligj",
    "ligjit": "ligj",
    "ligjeve": "ligj",
    "kushtetuta": "kushtetute",
    "kushtetutes": "kushtetute",
    "kushtetuese": "kushtetute",
    "kushtetutese": "kushtetute",
    "kodi": "kod",
    "kodit": "kod",
    "kodeve": "kod",
    "gjykata": "gjykate",
    "gjykates": "gjykate",
    "gjykatave": "gjykate",
    "vendimi": "vendim",
    "vendimit": "vendim",
    "vendimeve": "vendim",
    "kontrata": "kontrate",
    "kontrates": "kontrate",
    "kontratave": "kontrate",
    "pronesia": "pronesi",
    "pronesise": "pronesi",
    "pronesive": "pronesi",
    "detyrimi": "detyrim",
    "detyrimit": "detyrim",
    "detyrimeve": "detyrim",
    "drejta": "drejte",
    "drejtes": "drejte",
    "drejtave": "drejte",
    "arsimi": "arsim",
    "arsimin": "arsim",
    "arsimit": "arsim",
    "arsimim": "arsim",
    "arsimor": "arsim",
}


def normalize_for_search(text: str) -> str:
    """Normalize Albanian text for search matching.

    - Strips diacritics (ë→e, ç→c) so searches are accent-insensitive
    - Lowercases
    - Normalizes whitespace
    - Preserves numbers, legal references (Neni, Nr.)
    """
    if not text:
        return ""

    # Unicode NFKC normalization first
    text = unicodedata.normalize("NFKC", text)

    # Replace Albanian diacritics
    for char, replacement in _ALBANIAN_CHAR_MAP.items():
        text = text.replace(char, replacement)

    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    return text


def normalize_query(query: str) -> str:
    """Normalize a search query: lowercase + strip diacritics."""
    return normalize_for_search(query).lower()


def expand_diacritical_variants(word: str) -> list[str]:
    """Generate variants of a word with/without Albanian diacritics.

    E.g., "drejte" → ["drejte", "drejtë"]
    """
    if not word:
        return [word]

    variants = set()
    variants.add(word)

    # Add version with diacritics stripped
    stripped = word
    for char, repl in _ALBANIAN_CHAR_MAP.items():
        stripped = stripped.replace(char, repl)
    variants.add(stripped)

    # Add version with diacritics added back for 'e' and 'c'
    accented = word
    for plain, specials in _DIACRITICAL_VARIANTS.items():
        for s in specials:
            if s != plain:
                variant = accented.replace(plain, s)
                if variant != accented:
                    variants.add(variant)

    return list(variants)


def get_legal_root(word: str) -> str | None:
    """Get the canonical root form of a legal term, or None."""
    w = word.lower().strip()
    # Strip diacritics for lookup
    w_norm = normalize_query(w)
    return _LEGAL_SYNONYMS.get(w_norm) or _LEGAL_SYNONYMS.get(w)


def normalize_legal_query(query: str) -> str:
    """Normalize a query with legal term root forms.

    E.g., "Kushtetutes" → "kushtetute", "ligjit" → "ligj"
    """
    words = query.split()
    normalized = []
    for w in words:
        root = get_legal_root(w)
        if root:
            normalized.append(root)
        else:
            normalized.append(normalize_query(w))
    return " ".join(normalized)
