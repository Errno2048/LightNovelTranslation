"""The observed Linovelib PC reading-order protocol, without executing site JS.

The v1009c.8 reader permutes each HTTP page separately. Only nonempty direct
<p> children participate; other DOM nodes keep their slots. The rule is pinned
to that script version, rather than inferred from zero-width text markers.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

PROFILE = "linovelib-pc-v1009c8-v1"
SCRIPT_SHA256 = "9a1c2007d048857a619667aeebd4f95496aae77f544c7a2e032323aa2a776969"
ORDER_ATTRIBUTE = "data-novelfetch-order"
JS_SPACE = re.compile(r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]")

# Existing crawler substitution table, centralized so cache/EPUB comparisons
# and subsequent downloads use exactly the same character decoding.
_PUA_PAIRS = (
    ("", "的"), ("", "一"), ("", "是"), ("", "了"), ("", "我"),
    ("", "不"), ("", "人"), ("", "在"), ("", "他"), ("", "有"),
    ("", "这"), ("", "个"), ("", "上"), ("", "们"), ("", "来"),
    ("", "到"), ("", "时"), ("", "大"), ("", "地"), ("", "为"),
    ("", "子"), ("", "中"), ("", "你"), ("", "说"), ("", "生"),
    ("", "国"), ("", "年"), ("", "着"), ("", "就"), ("", "那"),
    ("", "和"), ("", "要"), ("", "她"), ("", "出"), ("", "也"),
    ("", "得"), ("", "里"), ("", "后"), ("", "自"), ("", "以"),
    ("", "会"), ("", "家"), ("", "可"), ("", "下"), ("", "而"),
    ("", "过"), ("", "天"), ("", "去"), ("", "能"), ("", "对"),
    ("", "小"), ("", "多"), ("", "然"), ("", "于"), ("", "心"),
    ("", "学"), ("", "么"), ("", "之"), ("", "都"), ("", "好"),
    ("", "看"), ("", "起"), ("", "发"), ("", "当"), ("", "没"),
    ("", "成"), ("", "只"), ("", "如"), ("", "事"), ("", "把"),
    ("", "还"), ("", "用"), ("", "第"), ("", "样"), ("", "道"),
    ("", "想"), ("", "作"), ("", "种"), ("", "开"), ("", "美"),
    ("", "乳"), ("", "阴"), ("", "液"), ("", "茎"), ("", "欲"),
    ("", "呻"), ("", "肉"), ("", "交"), ("", "性"), ("", "胸"),
    ("", "私"), ("", "穴"), ("", "淫"), ("", "臀"), ("", "舔"),
    ("", "射"), ("", "脱"), ("", "裸"), ("", "骚"), ("", "唇"),
)
PUA_TABLE = str.maketrans(dict(_PUA_PAIRS))


def replace_pua(text: str) -> str:
    return text.translate(PUA_TABLE)


def page_info(soup) -> dict:
    """Fail closed on unknown versions or disagreeing chapter/page identities."""
    scripts = [urlsplit(t["src"]) for t in soup.find_all("script", src=True) if "chapterlog" in t["src"]]
    if len(scripts) != 1 or scripts[0].path != "/scripts/chapterlog.js" or scripts[0].query != "v1009c.8":
        raise ValueError("unsupported Linovelib reading-order script; inspect before importing")
    if scripts[0].hostname not in {None, "www.linovelib.com", "linovelib.com"}:
        raise ValueError("unexpected Linovelib script host")
    nav = soup.select_one(".mlfy_page")
    if nav is None:
        raise ValueError("Linovelib pagination metadata missing")
    links = nav.find_all("a")
    marks = [re.fullmatch(r"sq\('(\d+)','(\d+)','(\d+)'\);", x.get("onclick", "")) for x in links]
    marks = [m for m in marks if m]
    if len(marks) != 1 or len(links) != 5:
        raise ValueError("ambiguous Linovelib chapter/page identity")
    book, chapter, page = (int(x) for x in marks[0].groups())
    params = [t.get_text() for t in soup.find_all("script") if "var ReadParams=" in t.get_text()]
    if len(params) != 1:
        raise ValueError("Linovelib ReadParams missing or ambiguous")
    for field, expected in [("articleid", book), ("chapterid", chapter), ("page", page)]:
        match = re.search(r"\b" + field + r":'(\d+)'", params[0])
        if not match or int(match[1]) != expected or expected < 1:
            raise ValueError("inconsistent Linovelib ReadParams and pagination identity")
    heading = soup.select_one(".h2-title") or soup.find("h1") or soup.find("title")
    if heading is None:
        raise ValueError("Linovelib chapter title missing")
    title = re.sub(r"[（(]\d+/\d+[）)]$", "", heading.get_text(strip=True)).strip()
    return {"book_id": book, "chapter_id": chapter, "page": page, "title": title,
            "previous": links[0].get("href", ""), "next": links[-1].get("href", ""), "profile": PROFILE}


def original_to_reading_order(count: int, chapter_id: int) -> list[int]:
    """Map each HTML-order paragraph to its reading-order position."""
    if count < 0 or chapter_id < 1:
        raise ValueError("invalid paragraph count/chapter identity")
    prefix = list(range(min(20, count)))
    tail = list(range(20, count))
    seed = chapter_id * 126 + 232
    for i in range(len(tail) - 1, 0, -1):
        seed = (seed * 9302 + 49397) % 233280
        j = seed * (i + 1) // 233280
        tail[i], tail[j] = tail[j], tail[i]
    return prefix + tail


def restore_order(content, chapter_id: int, *, profile: str = PROFILE) -> dict:
    if profile != PROFILE:
        raise ValueError("unsupported Linovelib order profile")
    if content.has_attr(ORDER_ATTRIBUTE):
        raise ValueError("reading order already restored; refusing to permute twice")
    direct = content.find_all("p", recursive=False)
    if len(direct) != len(content.find_all("p")):
        raise ValueError("nested Linovelib paragraphs require review")
    participating = [p for p in direct if JS_SPACE.sub("", p.decode_contents())]
    permutation = original_to_reading_order(len(participating), chapter_id)
    restored = [None] * len(participating)
    for original, destination in enumerate(permutation):
        restored[destination] = participating[original]
    by_identity = {id(p): restored[i] for i, p in enumerate(participating)}
    children = [by_identity.get(id(node), node) for node in list(content.contents)]
    input_indices = {id(p): i for i, p in enumerate(direct)}
    output_to_input = [input_indices[id(node)] for node in children if id(node) in input_indices]
    content.clear()
    for node in children:
        content.append(node)
    content[ORDER_ATTRIBUTE] = PROFILE
    return {"profile": PROFILE, "participating_paragraphs": len(participating),
            "output_to_input": output_to_input,
            "moved_paragraphs": sum(i != j for i, j in enumerate(output_to_input))}
