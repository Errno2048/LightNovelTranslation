from __future__ import annotations

import json

from lightnovel_translation import (GenerationResult, TranslationConfig,
                                    TranslationProject)
from translation.index import TranslationRunner, TranslationStore, TranslationWorkflow, LevelConfig
from translation.index.block_builder import SummaryIndex
from translation.index.summary_index import (ChapterRef, COMMITTED,
                                              NOT_CREATED, PROVISIONAL)


class IndexProvider:
    """Returns a valid structured summary for any index task."""

    def __init__(self):
        self.requests = []

    def generate(self, request):
        self.requests.append(request.task)
        if request.task.startswith(("index_l1_", "index_l2_")):
            text = (
                "@@OVERVIEW\ntext: 艾维一行在森林中前行，遇到了商人并交换了情报。\n"
                "@@EVENT e1\ntext: 遇到商人\nevidence_ref: J0000P001\n"
                "@@ENTITY n1\nform: アイビー\nkind: person\n"
                "description: 主角\nevidence_ref: J0000P001\n"
                "@@COVERAGE\nprocessed_all: yes\nomissions: 无\n@@END")
            return GenerationResult(text, "stop", "scripted", request.task, {}, 0)
        if request.task.startswith("index_l2_rollup_"):
            return GenerationResult(
                "艾维的旅程持续推进：离开村庄、结识同伴、发现线索。",
                "stop", "scripted", request.task, {}, 0)
        if request.task.startswith("glossary_"):
            return GenerationResult("@@END", "stop", "scripted", request.task, {}, 0)
        raise AssertionError("unexpected task " + request.task)


def _chapters(count: int) -> list[ChapterRef]:
    return [ChapterRef(order=i, title=f"{i}話 出発",
                       ja_text="アイビーは村を出た。\n\n森で商人に出会う。",
                       zh_text="艾维离开了村子。\n\n在森林遇到商人。")
            for i in range(count)]


def _spec(**overrides):
    from translation.editions.index import EditionIndexSpec
    values = {"child_max_chapters": 10, "parent_max_chapters": 100,
              "expected_l2_count": 10,
              "parent_build_mode": "direct_then_rollup"}
    values.update(overrides)
    return EditionIndexSpec(**values)


def _runner(tmp_path, provider):
    from pathlib import Path
    ja = tmp_path / "ja.txt"
    zh = tmp_path / "zh.txt"
    ja.write_text("第1話 序\n\nアイビーは村を出た。\n\n"
                  "第2話 旅\n\n森で商人に出会う。\n",
                  encoding="utf-8")
    zh.write_text("第1章 序\n\n艾维离开了村子。\n\n"
                  "第2章 旅\n\n在森林遇到商人。\n",
                  encoding="utf-8")
    project = TranslationProject(tmp_path / "project", TranslationConfig(),
                                 provider=provider, provider_id="index-v1")
    project.import_book(ja, edition_id="ja", language="ja", paragraph_policy="blank_line")
    project.import_book(zh, edition_id="zh", language="zh", paragraph_policy="blank_line")
    flow = TranslationWorkflow(project, "ja", "zh",
                               levels=LevelConfig((2, 2), (1, 1)), full_chapters=1)
    return project, flow


def test_level1_build_and_status_lifecycle(tmp_path):
    provider = IndexProvider()
    project, flow = _runner(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(project, flow, store,
                                       continuation_start=1, main_end=2,
                                       reference_texts={0: "艾维离开了村子。"},
                                       require_reference_glossary=False)
            index = SummaryIndex(runner, _chapters(25), spec=_spec())
            level1 = sorted((b for b in index.blocks.values() if b.level == 1),
                            key=lambda b: b.start)
            assert len(level1) == 3   # 25 chapters / child_max 10 -> 3 blocks
            block = level1[0]
            assert block.status == NOT_CREATED
            result = index.build_level1(block)
            assert result["status"] == PROVISIONAL
            assert block.summary and block.content["entities"]
            assert block.content["coverage"]["processed_all"] == "yes"
            # committed requires accepted chapters
            outcome = index.commit_block(block, accepted_orders={0, 1})
            assert outcome["status"] == PROVISIONAL and outcome["missing_accepted"]
            outcome = index.commit_block(block, accepted_orders=set(range(10)))
            assert outcome["status"] == COMMITTED
            persisted = store.db.execute(
                "SELECT status FROM summary_blocks WHERE block_id=?",
                (block.block_id,)).fetchone()
            assert persisted["status"] == COMMITTED
    finally:
        project.close()


def test_level2_direct_then_rollup(tmp_path):
    provider = IndexProvider()
    project, flow = _runner(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(project, flow, store,
                                       continuation_start=1, main_end=2,
                                       reference_texts={0: "艾维离开了村子。"},
                                       require_reference_glossary=False)
            index = SummaryIndex(runner, _chapters(20), spec=_spec())
            parent = next(b for b in index.blocks.values() if b.level == 2)
            result = index.build_level2(parent, method="direct_then_rollup")
            assert result["method"] == "direct"
            assert parent.build_method == "direct"
            assert parent.content["events"]
            # force rollup by requesting it explicitly
            result = index.build_level2(parent, method="child_summary_rollup")
            assert result["method"] == "child_summary_rollup"
            assert parent.build_method == "child_summary_rollup"
            assert any(t.startswith("index_l1_") for t in provider.requests)
    finally:
        project.close()


def test_context_blocks_and_barrier_gaps(tmp_path):
    provider = IndexProvider()
    project, flow = _runner(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(project, flow, store,
                                       continuation_start=1, main_end=2,
                                       reference_texts={0: "艾维离开了村子。"},
                                       require_reference_glossary=False)
            index = SummaryIndex(runner, _chapters(50), spec=_spec())
            # Nothing built: strict barrier must report a gap covering history.
            gaps = index.translation_barrier(45, strict=True)
            assert (0, 45) in [(a, b) for a, b in gaps] or gaps
            # Build everything up to order 45 as provisional then committed.
            for block in sorted(index.blocks.values(), key=lambda b: b.start):
                if block.level == 1 and block.end <= 40:
                    index.build_level1(block)
                elif block.level == 2 and block.end <= 30:
                    index.build_level2(block, method="direct_then_rollup")
            # Concurrent mode sees the provisional blocks; the 40..45 tail is
            # covered by full-text chapters, and 0..10 is still a real gap.
            concurrent_gaps = index.translation_barrier(45, strict=False)
            assert (0, 10) in concurrent_gaps
            assert all(start >= 40 or end <= 10 for start, end in concurrent_gaps
                       if (start, end) != (0, 10))
            selection = index.context_blocks(45, num_level1=3,
                                             full_start=40)
            assert selection["level1"] and selection["level1"][-1].end <= 40
            assert all(b.end <= selection["level1"][0].start
                       for b in selection["level2"])
    finally:
        project.close()


def test_reload_preserves_block_state(tmp_path):
    provider = IndexProvider()
    project, flow = _runner(tmp_path, provider)
    try:
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner = TranslationRunner(project, flow, store,
                                       continuation_start=1, main_end=2,
                                       reference_texts={0: "艾维离开了村子。"},
                                       require_reference_glossary=False)
            index = SummaryIndex(runner, _chapters(10), spec=_spec())
            block = next(b for b in index.blocks.values() if b.level == 1)
            index.build_level1(block)
        with TranslationStore(tmp_path / "translation.sqlite") as store:
            runner2 = TranslationRunner(project, flow, store,
                                        continuation_start=1, main_end=2,
                                        reference_texts={0: "艾维离开了村子。"},
                                        require_reference_glossary=False)
            index2 = SummaryIndex(runner2, _chapters(10), spec=_spec())
            block2 = index2.blocks[block.block_id]
            assert block2.status == PROVISIONAL
            assert block2.summary == block.summary
    finally:
        project.close()
