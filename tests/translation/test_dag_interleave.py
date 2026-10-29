from __future__ import annotations

import threading

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject)
from translation.index import (LevelConfig, TranslationRunner,
                               TranslationStore, TranslationWorkflow)
from translation.index.summary_index import COMMITTED, PROVISIONAL


class DagProvider:
    """Handles the full translate/review/index task mix."""

    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request):
        with self.lock:
            self.requests.append(request.task)
        task = request.task
        if task.startswith(("index_l1_", "index_l2_")):
            return GenerationResult(
                "@@OVERVIEW\ntext: 艾维与父亲继续旅行，遇到商队并交换情报。\n"
                "@@EVENT e1\ntext: 遇到商队\nevidence_ref: J0000P001\n"
                "@@ENTITY n1\nform: アイビー\nkind: person\n"
                "description: 主角\nevidence_ref: J0000P001\n"
                "@@COVERAGE\nprocessed_all: yes\nomissions: 无\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("index_l2_rollup_"):
            return GenerationResult("旅程持续推进。", "stop", "scripted",
                                    task, {}, 0)
        if task.startswith("batch_glossary_"):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("glossary_"):
            return GenerationResult("@@END", "stop", "scripted", task, {}, 0)
        if task.startswith("translate_"):
            return GenerationResult(
                "@@TITLE\ntext: 归来\n"
                "@@TRANSLATION S001\ntext: 诺亚回到了村子。\n"
                "@@TRANSLATION S002\ntext: 众人听他讲述旅途。\n@@END",
                "stop", "scripted", task, {}, 0)
        if task.startswith("review_"):
            return GenerationResult(
                "@@SUMMARY\nreviewed_all: yes\n"
                "semantic_coverage_complete: yes\nzero_findings: yes\n@@END",
                "stop", "scripted", task, {}, 0)
        raise AssertionError("unexpected task " + task)


def _project(tmp_path, provider, chapters=40):
    ja = tmp_path / "ja.txt"
    zh = tmp_path / "zh.txt"
    body = "".join(
        f"第{i}話 序\n\nアイビーは村を出た。\n\n森で商人に出会う。\n\n"
        for i in range(1, chapters + 1))
    ja.write_text(body, encoding="utf-8")
    zh.write_text("第1章 序\n\n艾维离开了村子。\n\n", encoding="utf-8")
    project = TranslationProject(tmp_path / "project", TranslationConfig(),
                                 provider=provider, provider_id="dag-v1")
    project.import_book(ja, edition_id="ja", language="ja",
                        paragraph_policy="blank_line")
    project.import_book(zh, edition_id="zh", language="zh",
                        paragraph_policy="blank_line")
    flow = TranslationWorkflow(project, "ja", "zh",
                               levels=LevelConfig((10, 10), (3, 1)),
                               full_chapters=1)
    return project, flow


def test_dag_interleave_builds_blocks_during_batch(tmp_path):
    provider = DagProvider()
    project, flow = _project(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(
                project, flow, store, continuation_start=1, main_end=3,
                reference_texts={0: "艾维离开了村子。"},
                require_reference_glossary=False)
            runner.failure_policy = "continue"
            runner.review_start = "prefix_as_completed"
            # seed one built block so barriers/interleave engage
            index = runner._summary_index()
            first = min((b for b in index.blocks.values() if b.level == 1),
                        key=lambda b: b.start)
            index.build_level1(first)
            result = runner.run_concurrent(
                start=10, end=30, batch_size=20,
                translation_concurrency=4, review_concurrency=4,
                update_summaries=False)
            assert not result["errors"], result["errors"]
            interleaved = store.db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='index_block_interleaved'"
            ).fetchone()[0]
            assert interleaved >= 1
            # Blocks covering [10,30) must now be built (provisional or better)
            statuses = {
                (b.start, b.end): b.status
                for b in index.blocks.values() if b.level == 1}
            built = [rng for rng, s in statuses.items()
                     if s in (PROVISIONAL, COMMITTED)]
            assert any(s <= 10 and e >= 20 for s, e in built), built
    finally:
        project.close()
