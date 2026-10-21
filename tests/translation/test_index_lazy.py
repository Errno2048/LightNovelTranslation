"""Lazy reference-index planning, estimates, splits, and metrics."""
import pytest

from translation.index import (ContextDocument, IndexMetrics, LevelConfig,
                               PlannedBlock, SingleChapterSplitRequired,
                               choose_materialization, direct_estimate,
                               estimate_chapter_tokens, plan_reference_blocks,
                               required_children_for_high_level, rollup_estimate,
                               split_planned_block)


class CharCounter:
    def count(self, text):
        return len(text)


def make_documents(count=25):
    return {
        f"s{i}": ContextDocument(source_id=f"s{i}", order=i, title=f"第{i}章",
                                 source_text="日" * 10, translation_text="中" * 10)
        for i in range(count)
    }


def test_lazy_reference_plan_creates_only_planned_boundaries():
    documents = make_documents(25)
    plan = plan_reference_blocks(documents, LevelConfig((10, 10)),
                                 CharCounter(), capacity=10000)
    level1 = plan.by_level(1)
    level2 = plan.by_level(2)
    assert len(level1) == 3
    assert len(level2) == 1
    assert all(block.status == "not_created" for block in plan.blocks)
    assert all(block.source_kind == "reference" for block in plan.blocks)
    assert all(block.parent_id == level2[0].block_id for block in level1)
    assert level2[0].parent_id is None
    assert level1[0].source_ids[0] == "s0"
    assert level1[-1].end == 25


def test_direct_and_rollup_estimates_and_choice():
    documents = make_documents(5)
    direct = direct_estimate([f"s{i}" for i in range(5)], documents,
                             CharCounter(), prompt_overhead=0)
    assert direct == sum(
        estimate_chapter_tokens(documents[f"s{i}"], CharCounter())
        for i in range(5))
    children = (PlannedBlock("a", 1, 0, 5, ("s0", "s1", "s2", "s3", "s4"),
                             direct, oversized=False),)
    rollup = rollup_estimate(children, prompt_overhead=0)
    assert rollup > 0
    assert choose_materialization(100, 60, 120).mode == "direct"
    assert choose_materialization(150, 60, 120).mode == "rollup"
    assert choose_materialization(150, 130, 120).mode == "split"


def test_split_at_chapter_boundary_and_single_chapter_fallback():
    documents = make_documents(4)
    block = PlannedBlock("block", 1, 0, 4, ("s0", "s1", "s2", "s3"),
                         estimated_tokens=400)
    parts = split_planned_block(block, documents, CharCounter(), capacity=250,
                                prompt_overhead=0)
    assert len(parts) == 2
    assert tuple(source for part in parts for source in part.source_ids) == block.source_ids
    assert all(part.status == "not_created" for part in parts)
    single = PlannedBlock("single", 1, 0, 1, ("s0",), estimated_tokens=400)
    with pytest.raises(SingleChapterSplitRequired):
        split_planned_block(single, documents, CharCounter(), capacity=250,
                            prompt_overhead=0)


def test_required_children_for_high_level_returns_all_descendants():
    documents = make_documents(25)
    plan = plan_reference_blocks(documents, LevelConfig((10, 10)),
                                 CharCounter(), capacity=10000)
    parent = plan.by_level(2)[0]
    descendants = required_children_for_high_level(plan, parent.block_id)
    assert len(descendants) == 3
    assert {block.level for block in descendants} == {1}


def test_metrics_record_dump_and_clear(tmp_path):
    metrics = IndexMetrics()
    metrics.record("direct_attempt", level=2)
    metrics.record("direct_attempt", level=2)
    metrics.record("fallback", reason="context_limit")
    assert metrics.as_dict() == {"direct_attempt": 2, "fallback": 1}
    path = tmp_path / "metrics.jsonl"
    assert metrics.dump(path) == 3
    assert len(path.read_text(encoding="utf-8").splitlines()) == 3
    metrics.clear()
    assert metrics.as_dict() == {}
