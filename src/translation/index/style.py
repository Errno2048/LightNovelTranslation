"""Rule-based style profiling for continuation translation.

The profile is extracted from the human reference once per run and injected as
a stable prompt block.  Title ordinals and punctuation are also enforced
deterministically after translation; prompt wording alone is not sufficient.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence

from .titles import format_chinese_number

_TITLE = re.compile(r"^\s*(第)?\s*(\d{1,4})\s*([话話章節节])\s*(.*)$")
_LEADING_ORDINAL = re.compile(
    r"^\s*(?:第\s*)?(?:[0-9]{1,4}|[〇零一二三四五六七八九十百千]+)\s*"
    r"(?:[话話章節节]\s*[：:.\-—、,，]?|[：:.\-—、,，]\s*|\s+)")
_ASCII_ELLIPSIS = re.compile(r"\.{3,}")


@dataclass(frozen=True)
class StyleProfile:
    title_marker: str = "话"
    title_separator: str = " "
    ordinal_style: str = "arabic"
    title_has_prefix: bool = False
    dialogue_open: str = "「"
    dialogue_close: str = "」"
    nested_open: str = "『"
    nested_close: str = "』"
    fullwidth: bool = True
    ellipsis: str = "……"
    examples: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "title_marker": self.title_marker,
            "title_separator": self.title_separator,
            "ordinal_style": self.ordinal_style,
            "title_has_prefix": self.title_has_prefix,
            "dialogue_open": self.dialogue_open,
            "dialogue_close": self.dialogue_close,
            "fullwidth": self.fullwidth,
            "ellipsis": self.ellipsis,
            "examples": list(self.examples),
        }

    def card(self) -> str:
        prefix = "第" if self.title_has_prefix else ""
        title_rule = (f"{prefix}{{n}}{self.title_marker}"
                      f"{self.title_separator}{{标题后缀}}")
        lines = [
            "【文体与标题约定】",
            f"- 章节标题：{title_rule}；序号必须保留，模型只翻译后缀。",
            f"- 对话引号：{self.dialogue_open}…{self.dialogue_close}",
            f"- 标点：{'全角' if self.fullwidth else '半角'}；省略号：{self.ellipsis}",
        ]
        if self.examples:
            lines.append("- 参考示例：")
            lines.extend(f"  {example}" for example in self.examples)
        return "\n".join(lines)


def _dominant_pair(texts: Sequence[str]) -> tuple[str, str]:
    counts = {
        ("「", "」"): sum(text.count("「") for text in texts),
        ("“", "”"): sum(text.count("“") for text in texts),
        ("『", "』"): sum(text.count("『") for text in texts),
    }
    pair, count = max(counts.items(), key=lambda item: item[1])
    if count:
        return pair
    return "「", "」"


def _title_profile(titles: Sequence[str]) -> tuple[str, str, bool, str]:
    markers: dict[str, int] = {}
    prefixes = 0
    separators: dict[str, int] = {}
    styles: dict[str, int] = {}
    for title in titles:
        match = _TITLE.match(str(title or ""))
        if not match:
            continue
        prefix, number, marker, rest = match.groups()
        markers[marker] = markers.get(marker, 0) + 1
        if prefix:
            prefixes += 1
        after = str(title).split(marker, 1)[1] if marker in title else ""
        separator = " "
        for candidate in (" ", "　", "：", ":", " - ", "—"):
            if after.startswith(candidate):
                separator = candidate
                break
        separators[separator] = separators.get(separator, 0) + 1
        style = "arabic" if number.isdigit() else "chinese"
        styles[style] = styles.get(style, 0) + 1
    marker = (max(markers.items(), key=lambda item: item[1])[0]
              if markers else "话")
    marker = {"話": "话", "節": "节"}.get(marker, marker)
    separator = (max(separators.items(), key=lambda item: item[1])[0]
                 if separators else " ")
    style = (max(styles.items(), key=lambda item: item[1])[0]
             if styles else "arabic")
    has_prefix = prefixes > 0
    return marker, separator, has_prefix, style


def extract_style_profile(reference_texts: Iterable[str] = (),
                          titles: Iterable[str] = ()) -> StyleProfile:
    texts = [str(value) for value in reference_texts if str(value).strip()]
    title_list = [str(value) for value in titles if str(value).strip()]
    dialogue_open, dialogue_close = _dominant_pair(texts)
    nested_candidates = {
        ("『", "』"): sum(text.count("『") for text in texts),
        ("“", "”"): sum(text.count("“") for text in texts),
        ("「", "」"): sum(text.count("「") for text in texts),
    }
    nested_candidates.pop((dialogue_open, dialogue_close), None)
    nested_pair, nested_count = max(nested_candidates.items(),
                                    key=lambda item: item[1])
    nested_open, nested_close = ("『", "』") if (
        dialogue_open == "「" and nested_pair == ("『", "』")) or (
        dialogue_open != "「" and nested_count == 0) else (
        nested_pair if nested_count > 0 else ("『", "』"))
    marker, separator, has_prefix, ordinal_style = _title_profile(title_list)
    fullwidth = sum(text.count("！") + text.count("？") for text in texts) >= \
        sum(text.count("!") + text.count("?") for text in texts)
    ellipsis = "……" if sum(text.count("……") for text in texts) >= \
        sum(text.count("...") for text in texts) else "..."
    examples = []
    for text in texts:
        for paragraph in text.split("\n\n"):
            value = paragraph.strip()
            if not value:
                continue
            if dialogue_open in value or len(value) >= 30:
                examples.append(value[:120])
            if len(examples) >= 3:
                break
        if len(examples) >= 3:
            break
    return StyleProfile(
        title_marker=marker, title_separator=separator,
        ordinal_style=ordinal_style, title_has_prefix=has_prefix,
        dialogue_open=dialogue_open, dialogue_close=dialogue_close,
        nested_open=nested_open, nested_close=nested_close,
        fullwidth=fullwidth, ellipsis=ellipsis, examples=tuple(examples))


def strip_title_ordinal(title: str, profile: StyleProfile | None = None) -> str:
    value = str(title or "").strip()
    match = _LEADING_ORDINAL.match(value)
    if match:
        value = value[match.end():].strip()
    return value


def render_main_title(ordinal: int, suffix: str,
                      profile: StyleProfile) -> str:
    number = (str(int(ordinal)) if profile.ordinal_style == "arabic"
              else format_chinese_number(int(ordinal)))
    prefix = "第" if profile.title_has_prefix else ""
    suffix = str(suffix or "").strip()
    title = f"{prefix}{number}{profile.title_marker}"
    if suffix:
        title += profile.title_separator + suffix
    return title


def normalize_punctuation(text: str, profile: StyleProfile) -> tuple[str, list[str]]:
    value = str(text or "")
    changes: list[str] = []
    if profile.fullwidth:
        converted = value.replace("!", "！").replace("?", "？")
        if converted != value:
            changes.append("ascii_punctuation")
            value = converted
    if profile.ellipsis == "……":
        converted = _ASCII_ELLIPSIS.sub("……", value)
        if converted != value:
            changes.append("ellipsis")
            value = converted
    if profile.dialogue_open == "「":
        if "「" not in value and "“" in value:
            changes.append("dialogue_quotes")
            value = value.replace("“", "「").replace("”", "」")
        # Nested quotes inside dialogue use the reference's nested pair
        # (『…』 when the primary dialogue style is 「…」).
        if profile.nested_open == "『":
            def _fix_nested(segment: str) -> str:
                open_pos = segment.find("「")
                while open_pos != -1:
                    close = segment.find("」", open_pos)
                    if close == -1:
                        break
                    inner = segment[open_pos + 1:close]
                    if "“" in inner or "”" in inner:
                        segment = (segment[:open_pos + 1]
                                   + inner.replace("“", "『").replace("”", "』")
                                   + segment[close:])
                        changes.append("nested_quotes")
                    open_pos = segment.find("「", close + 1)
                return segment
            value = _fix_nested(value)
    return value, changes


def style_violations(text: str, profile: StyleProfile) -> list[str]:
    value = str(text or "")
    problems = []
    if profile.dialogue_open == "「" and "“" in value and "「" not in value:
        problems.append("dialogue_quotes")
    if profile.dialogue_open == "“" and "「" in value and "“" not in value:
        problems.append("dialogue_quotes")
    if profile.fullwidth and re.search(r"[!?]", value):
        problems.append("ascii_punctuation")
    return problems
