"""Run the installed public library without API keys, network or repository data.

This is a protocol/engineering demonstration; its replies are deliberately fixed
and do not measure translation quality. python examples/library_offline.py <dir>
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

from lightnovel_translation import (
    EditionIndexSpec, GenerationResult, TranslationConfig, TranslationProject,
)


class OfflineProvider:
    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request)
        if request.task == "p7_translation":
            aliases = re.findall(r"C[A-Za-z0-9_]+\.J\d+", request.messages[-1]["content"].rsplit("【本次合法源段ID", 1)[1])
            lines = ["诺亚回到了村子。", "众人听他讲述旅途。"]
            text = "\n".join(f"@@TRANSLATION {alias}\ntext: {lines[i % 2]}" for i, alias in enumerate(aliases)) + "\n@@END"
        elif request.task == "p5_review":
            text = "@@SUMMARY\nreviewed_all: yes\nsemantic_coverage_complete: yes\nzero_findings: yes\n@@END"
        elif request.task.startswith("p6_extraction"):
            full = "\n".join(m["content"] for m in request.messages)
            alias = re.search(r"\[(C[A-Za-z0-9_]+\.J\d+)\]", full).group(1)
            text = f"@@SUMMARY\ntext: 诺亚回村并讲述旅途。\nja: {alias}\n@@END"
        elif request.task in {"p7_compression", "p7_summary"}:
            text = "诺亚回村并讲述旅途。"
        elif request.task.startswith("edition-index:"):
            payload = json.loads(request.messages[-1]["content"].split("\n正文结束", 1)[0])
            doc = payload["documents"][0]
            ref = doc["paragraphs"][0]["ref"]
            name = "ノア" if "ノア" in doc["paragraphs"][0]["text"] else "诺亚"
            text = (f"@@OVERVIEW\ntext: 诺亚回村并讲述旅途。\n@@EVENT e1\ntext: 旅人回村。\nevidence_ref: {ref}\n"
                    f"@@ENTITY n1\nform: {name}\nkind: person\ndescription: 回村的旅人。\nevidence_ref: {ref}\n"
                    "@@COVERAGE\nprocessed_all: yes\nentities_exhaustive: no\nomissions: 无\n@@END")
        elif request.task.startswith("edition-parent:"):
            text = "旅人回村并向众人讲述旅途。"
        elif request.task.startswith("edition-block-association"):
            payload = json.loads(request.messages[-1]["content"])
            lines = []
            for s in payload["source_blocks"]:
                for r in payload["reference_blocks"]:
                    lines.append(f'@@ASSOC a{len(lines)+1}\nsource_node: {s["node_id"]}\nreference_node: {r["node_id"]}\n'
                        f'relation: event_overlap\nsource_event: {s["events"][0]["id"]}\nreference_event: {r["events"][0]["id"]}\n'
                        'reason: 两侧均记述旅人回村。\ndifferences: 未确认整块相同。')
            text = "\n".join(lines) + "\n@@END"
        else:
            raise AssertionError("unhandled offline task: " + request.task)
        return GenerationResult(text, "stop", "offline-demo", str(len(self.requests)), {}, 0)


def run_demo(destination: str | Path) -> dict:
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    sample = destination / "sample-ja.txt"
    sample.write_text("\n\n".join(f"第{i}話 帰還\n\nノアは村に帰った。\n\n皆は旅の話を聞いた。" for i in range(1, 6)), encoding="utf-8")
    provider = OfflineProvider()
    config = TranslationConfig(index=EditionIndexSpec(child_max_chapters=2, parent_max_chapters=4))
    with TranslationProject(destination / "project", config, provider=provider, provider_id="offline-demo-v1") as project:
        source = project.import_book(sample, edition_id="ja", language="ja", paragraph_policy="blank_line")
        with project.create_job("demo", source=source) as job:
            planned = job.plan_next()
            first = job.run(max_chapters=2)
            checkpoint = job.checkpoint()
            resumed = job.resume(materialize=True)
            exported = job.export(include_io=True)
            return {"first": first.to_dict(), "final": resumed.to_dict(), "checkpoint": checkpoint,
                    "initial_state": planned["state"], "requests": len(provider.requests), "export": exported}


if __name__ == "__main__":
    print(json.dumps(run_demo(sys.argv[1] if len(sys.argv) > 1 else "offline-library-demo"), ensure_ascii=True, indent=2))
