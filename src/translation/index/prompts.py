"""Source/output prompts shared by the fused index workflow."""
from __future__ import annotations

import json

from ..references import STABLE, chapter_ref, pair_rows, prompt_projection


def source_message(source, unresolved=(), *, reference_profile="legacy", documents=()):
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


def full_pair_message(pair, *, reference_profile="legacy"):
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
