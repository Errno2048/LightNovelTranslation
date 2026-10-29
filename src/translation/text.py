"""Text cleanup shared by crawlers, cache rebuilds and corpus readers.

Remove format controls (Unicode Cf) and COMBINING GRAPHEME JOINER. Keep
combining accents, kana voicing marks and variation selectors: they carry
orthographic information rather than crawler watermarks. Never run this on
Linovelib's input DOM before its paragraph permutation has been decoded.
"""
from __future__ import annotations

import unicodedata
from collections import Counter
from functools import lru_cache

VERSION = "zero-width-v1"


@lru_cache(maxsize=4096)
def is_zero_width_control(char: str) -> bool:
    return unicodedata.category(char) == "Cf" or char == "\u034f"


def strip_zero_width(text: str) -> str:
    return "".join(c for c in text if not is_zero_width_control(c))


def zero_width_counts(text: str) -> dict[str, int]:
    return dict(sorted(Counter(f"U+{ord(c):04X}" for c in text if is_zero_width_control(c)).items()))


def clean_dom(node) -> dict[str, int]:
    """Clean decoded text/attributes, preserving elements and empty paragraphs."""
    counts = Counter()
    for text in list(node.find_all(string=True)):
        if text.parent.name in {"script", "style"}:
            continue
        counts.update(zero_width_counts(str(text)))
        cleaned = strip_zero_width(str(text))
        if cleaned != str(text):
            text.replace_with(cleaned)
    for tag in [node, *node.find_all(True)]:
        for key, value in list(tag.attrs.items()):
            values = value if isinstance(value, list) else [value]
            cleaned = []
            for item in values:
                if isinstance(item, str):
                    counts.update(zero_width_counts(item))
                    item = strip_zero_width(item)
                cleaned.append(item)
            tag[key] = cleaned if isinstance(value, list) else cleaned[0]
    return dict(counts)
