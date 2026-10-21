"""Pure context planning over synthetic hierarchical snapshots."""
from translation.index import (Artifact, ContextDocument, IndexSnapshot, InputRef,
                               LevelConfig, build_context_plan, choose_batch_size,
                               preflight_batch)


class CharCounter:
    def count(self, text):
        return len(text)


def _chapter(index, accepted=True):
    source_id = f"s{index}"
    return Artifact(
        artifact_id=f"c{index}", kind="chapter", level=0,
        source_ids=(source_id,),
        status="accepted" if accepted else "translated",
        inputs=(InputRef(source_id, f"v{index}", f"h{index}"),) if accepted else ())


def _block(artifact_id, level, source_ids, *, parent_id=None, summary="summary",
           status="provisional"):
    values = {"artifact_id": artifact_id, "kind": "block", "level": level,
              "source_ids": tuple(source_ids), "status": status,
              "content": {"summary": summary}}
    if parent_id:
        values["parent_id"] = parent_id
    return Artifact(**values)


def make_case():
    documents = {
        f"s{i}": ContextDocument(
            source_id=f"s{i}", order=i, title=f"第{i}章",
            source_text=f"日文{i}",
            translation_text=f"中文{i}" if i < 25 else None)
        for i in range(0, 30)
    }
    artifacts = {f"c{i}": _chapter(i) for i in range(25)}
    level2 = _block("L2", 2, [f"s{i}" for i in range(25)], summary="一级")
    artifacts["L2"] = level2
    for index in range(5):
        start = index * 5
        block = _block(f"L1-{index}", 1, [f"s{i}" for i in range(start, start + 5)],
                       parent_id="L2", summary=f"块{index}")
        artifacts[block.artifact_id] = block
    snapshot = IndexSnapshot(
        snapshot_id="snap", generation="gen",
        levels=LevelConfig(max_children=(10, 10), context_num_blocks=(3, 1)),
        artifacts=artifacts)
    return snapshot, documents


def test_context_aligns_full_text_to_level1_boundary():
    snapshot, documents = make_case()
    plan = build_context_plan(snapshot, documents, "s25", ["s25", "s26"],
                              estimator=CharCounter(), max_tokens=10000,
                              full_chapters=3)
    representations = [(segment.representation, segment.start, segment.end)
                       for segment in plan.segments]
    assert ("full", 20, 21) in representations
    assert ("full", 24, 25) in representations
    assert not any(segment.representation == "full" and segment.start < 20
                   for segment in plan.segments)
    assert ("target_source", 25, 26) in representations
    assert plan.fits is True
    assert not any(segment.start < 20 and segment.representation == "full"
                   for segment in plan.segments)


def test_context_includes_batch_japanese_prefix_for_later_targets():
    snapshot, documents = make_case()
    plan = build_context_plan(snapshot, documents, "s26", ["s25", "s26"],
                              estimator=CharCounter(), max_tokens=10000,
                              full_chapters=3)
    prefix = [segment for segment in plan.segments
              if segment.representation == "batch_source"]
    assert len(prefix) == 1
    assert prefix[0].source_ids == ("s25",)
    assert "日文25" in prefix[0].text


def test_context_ledger_has_no_overlapping_full_and_summary():
    snapshot, documents = make_case()
    plan = build_context_plan(snapshot, documents, "s25", ["s25", "s26"],
                              estimator=CharCounter(), max_tokens=10000,
                              full_chapters=3)
    covered = []
    for segment in plan.segments:
        for source_id in segment.source_ids:
            order = documents[source_id].order
            covered.append((order, segment.representation))
    assert len(covered) == len(set(covered))
    summary_orders = {order for order, kind in covered if kind == "summary"}
    full_orders = {order for order, kind in covered if kind == "full"}
    assert summary_orders.isdisjoint(full_orders)


def test_batch_preflight_and_shrink():
    snapshot, documents = make_case()
    batch = [f"s{i}" for i in range(25, 30)]
    full = preflight_batch(snapshot, documents, batch, estimator=CharCounter(),
                           max_tokens=10 ** 9, full_chapters=3)
    one = preflight_batch(snapshot, documents, batch[:1], estimator=CharCounter(),
                          max_tokens=10 ** 9, full_chapters=3)
    limit = one.max_tokens + 1
    selection = choose_batch_size(snapshot, documents, batch,
                                  estimator=CharCounter(), max_tokens=limit,
                                  requested_size=5, full_chapters=3)
    assert selection.size < 5
    assert selection.fits
    plan = preflight_batch(snapshot, documents, selection.batch_ids,
                           estimator=CharCounter(), max_tokens=limit,
                           full_chapters=3)
    assert plan.fits
    assert all(item.total_tokens <= limit for item in plan.plans)
    assert full.max_tokens > one.max_tokens


def test_provisional_blocks_are_marked_in_coverage():
    snapshot, documents = make_case()
    plan = build_context_plan(snapshot, documents, "s25", ["s25", "s26"],
                              estimator=CharCounter(), max_tokens=10000,
                              full_chapters=3)
    summaries = [segment for segment in plan.segments
                 if segment.representation == "summary"]
    assert summaries
    assert all(segment.status == "provisional" for segment in summaries)


def test_higher_level_summary_covers_missing_level1_blocks():
    documents = {
        f"s{i}": ContextDocument(source_id=f"s{i}", order=i, title=f"第{i}章",
                                 source_text=f"日文{i}",
                                 translation_text=f"中文{i}" if i < 25 else None)
        for i in range(30)
    }
    artifacts = {f"c{i}": _chapter(i) for i in range(25)}
    level2 = Artifact(
        artifact_id="L2", kind="block", level=2,
        source_ids=tuple(f"s{i}" for i in range(20)),
        status="provisional", content={"summary": "上级摘要"})
    artifacts["L2"] = level2
    snapshot = IndexSnapshot(
        snapshot_id="snap", generation="gen",
        levels=LevelConfig(max_children=(10, 10), context_num_blocks=(3, 1)),
        artifacts=artifacts)
    plan = build_context_plan(snapshot, documents, "s25", ["s25", "s26"],
                              estimator=CharCounter(), max_tokens=10000,
                              full_chapters=5)
    summaries = [segment for segment in plan.segments
                 if segment.representation == "summary"]
    assert len(summaries) == 1
    assert summaries[0].level == 2
    assert summaries[0].start == 0 and summaries[0].end == 20
