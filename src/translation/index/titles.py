"""Deterministic title-pattern normalization.

Title patterns are deliberately shallow: a fixed prefix, a numeral, and an
optional suffix. Unique titles are left untouched.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

_KANJI_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}
_NUMBER = r"(?:[0-9]+|[〇零一二三四五六七八九十百千]+)"
_PATTERNS = (
    re.compile(rf"^(?P<prefix>.*?)\s*其の\s*(?P<num>{_NUMBER})\s*(?P<suffix>.*)$"),
    re.compile(rf"^(?P<prefix>.*?)\s*其之\s*(?P<num>{_NUMBER})\s*(?P<suffix>.*)$"),
    re.compile(rf"^(?P<prefix>.*?)第\s*(?P<num>{_NUMBER})\s*話\s*(?P<suffix>.*)$"),
    re.compile(rf"^(?P<prefix>.*?)(?P<num>{_NUMBER})\s*話\s*(?P<suffix>.*)$"),
)
_TARGET_MARKER = re.compile(
    rf"(?P<prefix>.*?)(?P<marker>其之?|第)\s*(?P<num>{_NUMBER})\s*(?P<suffix>.*)$")


@dataclass(frozen=True)
class TitlePattern:
    key: str
    prefix: str
    ordinal: int
    suffix: str
    marker: str
    normalized: str


@dataclass(frozen=True)
class TargetTemplate:
    template: str
    style: str


def normalize_title(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def parse_kanji_number(value: str) -> int | None:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    if text.isdigit():
        return int(text)
    total = section = number = 0
    for char in text:
        if char in _KANJI_DIGITS:
            number = _KANJI_DIGITS[char]
        elif char in _KANJI_UNITS:
            unit = _KANJI_UNITS[char]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return None
    return section + number


def format_chinese_number(value: int) -> str:
    value = int(value)
    if value < 0:
        raise ValueError("ordinal must be nonnegative")
    if value == 0:
        return "零"
    digits = "零一二三四五六七八九"
    if value < 10:
        return digits[value]
    if value < 20:
        return "十" + (digits[value - 10] if value > 10 else "")
    if value < 100:
        tens, ones = divmod(value, 10)
        return digits[tens] + "十" + (digits[ones] if ones else "")
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        text = digits[hundreds] + "百"
        if rest == 0:
            return text
        if rest < 10:
            return text + "零" + digits[rest]
        return text + format_chinese_number(rest)
    return str(value)


def parse_title_pattern(value: str) -> TitlePattern | None:
    normalized = normalize_title(value)
    for regex in _PATTERNS:
        match = regex.match(normalized)
        if not match:
            continue
        ordinal = parse_kanji_number(match.group("num"))
        if ordinal is None:
            continue
        prefix = match.group("prefix").strip()
        suffix = match.group("suffix").strip()
        marker_match = re.search(r"(其の|其之|第)", normalized)
        marker = marker_match.group(1) if marker_match else ""
        key = f"{prefix} {marker}{{n}} {suffix}".strip()
        return TitlePattern(key=key, prefix=prefix, ordinal=ordinal,
                            suffix=suffix, marker=marker,
                            normalized=normalized)
    return None


def title_pattern_key(pattern: TitlePattern) -> str:
    return "@title:" + pattern.key


def parse_target_template(value: str, ordinal: int) -> TargetTemplate | None:
    normalized = normalize_title(value)
    match = _TARGET_MARKER.match(normalized)
    if match:
        number = parse_kanji_number(match.group("num"))
        if number == ordinal:
            template = (match.group("prefix") + match.group("marker") +
                        "{n}" + match.group("suffix"))
            style = ("arabic" if match.group("num").isdigit()
                     else "chinese")
            return TargetTemplate(template=template, style=style)
    candidates = []
    for found in re.finditer(_NUMBER, normalized):
        number = parse_kanji_number(found.group())
        if number == ordinal:
            candidates.append(found)
    if len(candidates) == 1:
        found = candidates[0]
        style = "arabic" if found.group().isdigit() else "chinese"
        return TargetTemplate(
            template=normalized[:found.start()] + "{n}" + normalized[found.end():],
            style=style)
    return None


def render_title_template(template: str, ordinal: int, style: str = "chinese") -> str:
    number = (str(int(ordinal)) if style == "arabic"
              else format_chinese_number(int(ordinal)))
    return str(template).replace("{n}", number).replace("{ordinal}", number)
