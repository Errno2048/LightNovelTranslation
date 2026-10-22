"""Phase 1 hierarchical index primitives."""
import pytest

from translation.index import (Artifact, BlockStatus, ChapterStatus, IndexSnapshot,
                               InputRef, LevelConfig, PointerSet, block_key,
                               contiguous_advance)


def chapter_artifact(status=ChapterStatus.TRANSLATED, **overrides):
    values = {
        "artifact_id": "c1",
        "kind": "chapter",
        "level": 0,
        "source_ids": ("src1",),
        "status": status,
        "inputs": (InputRef("src1", "fixture", "hash1"),),
    }
    values.update(overrides)
    return Artifact(**values)


def block_artifact(status=BlockStatus.PROVISIONAL, **overrides):
    values = {
        "artifact_id": "b1",
        "kind": "block",
        "level": 1,
        "source_ids": ("src1",),
        "status": status,
    }
    values.update(overrides)
    return Artifact(**values)


def test_level_config_arrays_and_fallback():
    levels = LevelConfig(max_children=(10, 10), context_num_blocks=(3,))
    assert levels.max_level == 2
    assert levels.enabled(1) and levels.enabled(2) and not levels.enabled(3)
    assert levels.max_children_for(2) == 10
    assert levels.context_blocks_for(1) == 3
    assert levels.context_blocks_for(2) == 3
    with pytest.raises(ValueError):
        LevelConfig(max_children=())
    with pytest.raises(ValueError):
        LevelConfig(max_children=(10,), context_num_blocks=(1, 2))
    with pytest.raises(ValueError):
        levels.max_children_for(3)


def test_artifact_status_validation():
    assert chapter_artifact().status == "translated"
    with pytest.raises(ValueError):
        chapter_artifact(level=1)
    with pytest.raises(ValueError):
        block_artifact(level=0)
    with pytest.raises(ValueError):
        block_artifact(status=BlockStatus.PROVISIONAL, review_id="r1")
    with pytest.raises(ValueError):
        block_artifact(status=BlockStatus.ACCEPTED)
    accepted = block_artifact(status=BlockStatus.ACCEPTED,
                              inputs=(InputRef("src1", "fixture", "hash1"),),
                              review_mode="model", review_id="r1")
    assert accepted.status == "accepted"


def test_snapshot_parent_coverage_and_disabled_levels():
    child = block_artifact(artifact_id="child", source_ids=("src1", "src2"),
                           parent_id="parent")
    parent = block_artifact(artifact_id="parent", level=2,
                            source_ids=("src1", "src2", "src3"))
    snapshot = IndexSnapshot(snapshot_id="s1", generation="g1",
                             levels=LevelConfig((10, 10)),
                             artifacts={"child": child, "parent": parent})
    assert len(snapshot.blocks(1)) == 1
    assert len(snapshot.blocks(2)) == 1

    bad_parent = block_artifact(artifact_id="parent", level=2,
                                source_ids=("src1",))
    child = block_artifact(artifact_id="child", source_ids=("src1", "src2"),
                           parent_id="parent")
    with pytest.raises(ValueError):
        IndexSnapshot(snapshot_id="s2", generation="g2",
                      levels=LevelConfig((10, 10)),
                      artifacts={"child": child, "parent": bad_parent})

    level3 = block_artifact(artifact_id="b3", level=3, source_ids=("src1",))
    with pytest.raises(ValueError):
        IndexSnapshot(snapshot_id="s3", generation="g3",
                      levels=LevelConfig((10, 10)),
                      artifacts={"b3": level3})


def test_snapshot_rebuild_only_quality_label():
    accepted = block_artifact(
        artifact_id="b1", status=BlockStatus.ACCEPTED,
        inputs=(InputRef("src1", "fixture", "hash1"),),
        review_mode="rebuild_only")
    with pytest.raises(ValueError):
        IndexSnapshot(snapshot_id="s1", generation="g1",
                      levels=LevelConfig((10,)),
                      artifacts={"b1": accepted})
    accepted = block_artifact(
        artifact_id="b1", status=BlockStatus.ACCEPTED,
        inputs=(InputRef("src1", "fixture", "hash1"),),
        review_mode="rebuild_only", quality_labels=("rebuild_only",))
    snapshot = IndexSnapshot(snapshot_id="s2", generation="g2",
                             levels=LevelConfig((10,)),
                             artifacts={"b1": accepted})
    assert snapshot.accepted_blocks(1)[0].review_mode == "rebuild_only"


def test_pointers_are_monotonic_and_contiguous():
    pointers = PointerSet()
    assert pointers.translated_through == -1
    pointers = pointers.with_scalar("translated_through", 3)
    with pytest.raises(ValueError):
        pointers.with_scalar("translated_through", 2)
    pointers = pointers.with_materialized(1, 5).with_accepted(1, 4)
    assert pointers.materialized(1) == 5
    assert pointers.accepted(1) == 4
    with pytest.raises(ValueError):
        pointers.with_materialized(1, 4)

    advance = contiguous_advance(-1, {0, 1, 3})
    assert advance.value == 1
    assert [event.current for event in advance.events] == [0, 1]
    blocked = contiguous_advance(-1, {0, 1, 2}, blocked_at=2)
    assert blocked.value == 1


def test_block_key_is_deterministic():
    assert block_key(1, 10, 20, "abcdef0123456789") == "L1:000010-000020:abcdef0123456789"
    assert block_key(1, 10, 20, "abcdef0123456789") == block_key(1, 10, 20, "abcdef0123456789")
    with pytest.raises(ValueError):
        block_key(1, 20, 20, "hash")
