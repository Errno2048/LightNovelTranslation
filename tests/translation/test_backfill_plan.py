from __future__ import annotations

from translation.index import (EXTRA, MAIN, GlossaryTerm, LevelConfig,
                               TranslationWorkflow, plan_backfill)
from lightnovel_translation import TranslationConfig, TranslationProject


class _Store:
    def accepted_orders(self):
        return [0, 5]


class _Plan:
    def is_translatable(self, order, roles):
        return self.role(order) in set(roles)

    def role(self, order):
        return EXTRA if order in {2, 4, 7} else MAIN


class _Runner:
    def __init__(self):
        self.source_by_order = {
            order: {"raw_title": f"{order}話"} for order in range(10)}
        self.reference_texts = {0: "r0", 1: "r1", 5: "r5"}
        self.chapter_plan = _Plan()
        self.translate_roles = {MAIN, EXTRA}
        self.store = _Store()
        self.main_end = 10

    def source(self, order):
        return self.source_by_order[order]


def test_backfill_plan_excludes_reference_and_groups_regions():
    plan = plan_backfill(_Runner(), region_span=1,
                         before_count=2, after_count=2)
    assert [gap.order for gap in plan.gaps] == [2, 3, 4, 6, 7, 8, 9]
    assert len(plan.regions) == 2
    first, second = plan.regions
    assert first.before_orders == (0, 1)
    assert first.after_orders == (5,)
    assert second.before_orders == (1, 5)
    assert second.after_orders == ()
    assert first.prefix_hash != second.prefix_hash
    assert plan.report["gap_count"] == 7


def test_assemble_backfill_orders_region_before_target(tmp_path):
    japanese = tmp_path / "ja.txt"
    chinese = tmp_path / "zh.txt"
    japanese.write_text(
        "第1話 帰還\n\nノアは村に帰った。\n\n"
        "第2話 出発\n\nノアは村を出た。\n\n"
        "第3話 到着\n\nノアは町に着いた。\n",
        encoding="utf-8")
    chinese.write_text(
        "第1章 归来\n\n诺亚回到了村子。\n\n"
        "第2章 出发\n\n诺亚离开了村子。\n\n"
        "第3章 到达\n\n诺亚抵达了城镇。\n",
        encoding="utf-8")
    with TranslationProject(tmp_path / "project", TranslationConfig()) as project:
        project.import_book(japanese, edition_id="ja", language="ja",
                            paragraph_policy="blank_line")
        project.import_book(chinese, edition_id="zh", language="zh",
                            paragraph_policy="blank_line")
        flow = TranslationWorkflow(project, "ja", "zh",
                                   levels=LevelConfig((2, 2), (1, 1)),
                                   full_chapters=1)
        messages, report = flow.assemble_backfill(
            target_order=1, target_title="第2話 出発",
            before_texts=[(0, "第1話 帰還", "前文参考")],
            after_texts=[(2, "第3話 到着", "后文参考")],
            glossary=[GlossaryTerm("ノア", "诺亚", "person", 1)],
            stable_glossary=[GlossaryTerm("ノア", "诺亚", "person", 1)],
            reference_summary="全局摘要",
            target_summaries=["已接受摘要"],
            source_rows=[{"text": "ノアは村を出た。"}],
            output_tokens=512,
            region_label="region 1 [1:2)")
        contents = [message["content"] for message in messages]
        before = next(index for index, value in enumerate(contents)
                      if "缺口前文参考" in value)
        after = next(index for index, value in enumerate(contents)
                     if "缺口后文参考" in value)
        target = next(index for index, value in enumerate(contents)
                      if "当前目标" in value)
        assert before < after < target
        assert report["fits"]
