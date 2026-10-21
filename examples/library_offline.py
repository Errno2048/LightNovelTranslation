"""Run the installed public library without API keys, network or repository data.

This demonstrates the fused index/context/provider surface; replies are fixed
and do not measure translation quality. python examples/library_offline.py <dir>
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from lightnovel_translation import (EditionIndexSpec, GenerationResult, TranslationConfig,
                                    TranslationProject)
from translation.index import FusedWorkflow, LevelConfig


class OfflineProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if request.task == "reference_summary":
            text = "诺亚回到村子，并向众人讲述旅途。"
        elif request.task.startswith("fused_translate_"):
            text = "诺亚回到了村子。\n众人听他讲述旅途。"
        else:
            raise AssertionError("unhandled offline task: " + request.task)
        return GenerationResult(text, "stop", "offline-demo", str(len(self.requests)), {}, 0)


def run_demo(destination: str | Path) -> dict:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    japanese = destination / "sample-ja.txt"
    chinese = destination / "sample-zh.txt"
    japanese.write_text(
        "\n\n".join(f"第{i}話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。"
                    for i in range(1, 6)), encoding="utf-8")
    chinese.write_text(
        "\n\n".join(f"第{i}章 归来\n\n诺亚回到了村子。\n\n众人听他讲述旅途。"
                    for i in range(1, 6)), encoding="utf-8")
    provider = OfflineProvider()
    config = TranslationConfig(index=EditionIndexSpec(child_max_chapters=2,
                                                      parent_max_chapters=4))
    with TranslationProject(destination / "project", config, provider=provider,
                            provider_id="offline-demo-v1") as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = FusedWorkflow(project, "ja", "zh",
                             levels=LevelConfig((2, 2), (1, 1)), full_chapters=2)
        plan = flow.reference_plan()
        documents = flow.documents(start=0, end=5)
        summary, _ = flow.summarize_reference(start=0, end=2)
        snapshot = flow.make_snapshot(summary, summary_start=0, summary_end=2,
                                      full_start=2, full_end=4, documents=documents)
        result, report, context = flow.translate(
            snapshot=snapshot, documents=documents, target_order=4,
            batch_orders=[4], output_tokens=256)
        return {
            "planned_blocks": len(plan.blocks),
            "context_segments": [(segment.representation, segment.start, segment.end)
                                 for segment in context.segments],
            "fits": report["fits"],
            "text": result.text,
            "requests": len(provider.requests),
        }


if __name__ == "__main__":
    print(json.dumps(run_demo(sys.argv[1] if len(sys.argv) > 1 else "offline-library-demo"),
                     ensure_ascii=True, indent=2))
