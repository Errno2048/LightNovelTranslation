"""Source/output prompts shared by the hierarchical index workflow."""
from __future__ import annotations

import json

from ..references import STABLE, chapter_ref, pair_rows, prompt_projection


def source_message(source, unresolved=(), *, reference_profile="plain", documents=()):
    if reference_profile == STABLE:
        rows, _ = pair_rows({"source": source, "translation": []})
        return {
            "role": "user",
            "content":
            "【本分支前章未决；不代表事实或硬性译法】\n" +
            json.dumps(prompt_projection(unresolved, documents), ensure_ascii=False) +
            f"\n【当前目标 {chapter_ref(source)}：{source['title']}；完整日文正文】\n" +
            "\n".join(f"[{row['alias']}] {row['text']}" for row in rows) +
            "\n只翻译本章。只输出译文正文，不输出标题、段落定位ID或解释。",
        }
    return {
        "role": "user",
        "content":
        "【本分支前章未决；不代表事实或硬性译法】\n" +
        json.dumps(unresolved, ensure_ascii=False) +
        f"\n【当前目标 {source['match_key']} 完整日文正文；只翻译本章】\n" +
        source["text"] +
        "\n只输出译文正文，不输出标题、段落定位ID或解释。",
    }


SOURCE_RECORDS_RULES = """【译文输出协议 source-records】
先输出一条标题记录，再逐个保留全部非空原文段落，每个源段只输出一条记录。
复制该段给出的完整S标签，不自行计数、改号、合并或遗漏。即使一句话跨段，也逐段保留引语前后的叙述，不把后一段内容移到前一段。
原文中的 [[IMG:<id>]] 是图片占位符；每个占位符必须在对应译文中原样保留，恰好出现一次，顺序不变，不得翻译、改号、合并或删除。占位符应放在译文中语义对应的位置。
只输出如下记录，最后独占一行@@END；text为该源段或标题的完整中文，不输出分析、代码块或额外说明。
@@TITLE
text: 本章标题的中文翻译
@@TRANSLATION S001
text: 第一段的完整中文
@@TRANSLATION S002
text: 第二段的完整中文
@@END
实际输出必须使用本章全部实际S标签，按原文顺序各一次。text中的换行仅作记录内排版；不要借换行遗漏或重复源段。"""


def source_records_alias_map(aliases):
    return ("【本次合法源段ID，按原文顺序各一次】\n" + ",".join(aliases))


def source_records_rules(aliases):
    return SOURCE_RECORDS_RULES + "\n" + source_records_alias_map(aliases)


def full_pair_message(pair, *, reference_profile="plain"):
    if reference_profile == STABLE:
        rows, _ = pair_rows(pair)
        return {
            "role": "user",
            "content": f"【邻近完整双语；{pair['translation_status']}】\n" +
            "\n".join(f"[{row['alias']}] {row['text']}" for row in rows),
        }
    return {
        "role": "user",
        "content":
        f"【邻近完整双语 {pair['source']['match_key']}；{pair['translation_status']}】\n"
        "<ja>\n" + pair["source"]["text"] + "\n</ja>\n<zh>\n" +
        "\n".join(doc["text"] for doc in pair["translation"]) + "\n</zh>",
    }
