"""Policy-bound P6 views and explicit story-time/disclosure queries."""
from __future__ import annotations

from ..storage import digest
from .epistemic import information_status
from .retrieval import EvidenceSearch
from .schemas import P6Error, StoryTime
from .store import StateStore
from .temporal import TemporalIndex

STATE_PREDICATES = {"location", "presence", "membership", "relation", "possession", "capability"}


class NarrativeState:
    def __init__(self, narrative_store):
        self.store = StateStore(narrative_store)

    def open_view(self, narrative_view, *, snapshot_id=None, branch_commit=None, policy=None,
                  target_position=None, story_anchor=None, perspective=None, purpose="translation",
                  analysis_overlay_id=None):
        if story_anchor is None:
            raise P6Error("invalid_record", "story_anchor is explicit and cannot be inferred from disclosure order")
        anchor = StoryTime(**story_anchor) if isinstance(story_anchor, dict) else StoryTime(str(story_anchor))
        if target_position is not None and (type(target_position) is not int or target_position < 0):
            raise P6Error("invalid_record", "target disclosure position must be nonnegative")
        if perspective is not None and not isinstance(perspective, str):
            raise P6Error("invalid_record", "perspective identifier must be a string")
        if snapshot_id and branch_commit:
            raise P6Error("invalid_record", "choose snapshot_id or branch_commit")
        if policy is not None and policy.public() != narrative_view.policy.public():
            raise P6Error("visibility_denied", "P6 policy must match the source view")
        commit_id = branch_commit
        if snapshot_id:
            snap = self.store.store.get(snapshot_id, "p6-state-snapshot")
            commit_id = snap.get("commit_id")
            if not commit_id:
                raise P6Error("invalid_record", "snapshot does not reference a P6 commit")
        if commit_id:
            self.store.read_commit(commit_id)
        overlay = self.store.read_overlay(analysis_overlay_id) if analysis_overlay_id else None
        view_fingerprint = digest({"policy": narrative_view.policy.public(),
                         "position": target_position, "anchor": anchor.__dict__, "perspective": perspective,
                         "purpose": purpose, "commit": snapshot_id or branch_commit or "working"})
        if overlay is not None and overlay.get("policy") != view_fingerprint:
            raise P6Error("visibility_denied", "analysis overlay belongs to another temporal/policy view")
        return StateView(narrative_view, self.store, anchor, target_position, perspective, purpose, overlay,
                         commit_id or "working", view_fingerprint)


class StateView:
    def __init__(self, narrative, store, story_anchor, target_position, perspective, purpose,
                 overlay, snapshot_id, view_fingerprint):
        self.narrative, self.store, self.story_anchor = narrative, store, story_anchor
        self.target_position, self.perspective, self.purpose = target_position, perspective, purpose
        self.overlay, self.snapshot_id, self.view_fingerprint = overlay, snapshot_id, view_fingerprint
        self.target_id = narrative.policy.target_id
        self.policy_public = narrative.policy.public()
        from ..narrative.service import dependency
        self.dependencies = []
        if self.target_id:
            target = narrative.validate_dependency(dependency(narrative.body(self.target_id, task=True)), task=True)
            self.dependencies.append(dependency(target))
        self.searcher = EvidenceSearch(self)

    def search_evidence(self, query, **kwargs):
        return self.searcher.search(query, **kwargs)

    def read_evidence(self, evidence_id, **kwargs):
        return self.searcher.read(evidence_id, **kwargs)

    def _records(self, source_id=None, include_candidates=False):
        out = []
        if self.snapshot_id != "working":
            committed = self.store.read_commit(self.snapshot_id)
            for row in committed["records"]:
                if source_id and row.get("chapter_id") not in {None, source_id}:
                    continue
                if row.get("disclosure_order") is not None and self.target_position is not None and row["disclosure_order"] > self.target_position:
                    continue
                cid = row.get("chapter_id")
                try:
                    self.narrative.check(cid, task=cid == self.target_id)
                except Exception:
                    continue
                if include_candidates or row.get("review_status") == "reviewed":
                    out.append(row)
        overlays = ([self.overlay] if self.overlay else [])
        overlays += [data for _, data in self.store.store.objects("p6-analysis-overlay")
                     if data.get("policy") == self.view_fingerprint]
        seen = set()
        for overlay in overlays:
            if overlay["task_id"] in seen:
                continue
            seen.add(overlay["task_id"])
            for row in overlay["records"]:
                if source_id and row.get("chapter_id") not in {None, source_id}:
                    continue
                if include_candidates or row.get("review_status") == "reviewed":
                    out.append(row)
        unique = {}
        for row in out:
            unique[row["record_id"]] = row
        return list(unique.values())

    def get_state(self, subject, predicate, *, at_anchor=None, branch="main"):
        if not subject or not predicate:
            raise P6Error("invalid_query", "subject and predicate are required")
        target = at_anchor or self.story_anchor.anchor_id
        matching = [r for r in self._records(include_candidates=False) if r.get("type") == "STATE"
                    and r.get("subject") == subject and r.get("predicate") == predicate
                    and r.get("branch", "main") == branch]
        if predicate not in STATE_PREDICATES:
            return {"status": "unsupported", "records": [], "coverage": "predicate_outside_registered_set",
                    "story_anchor": target, "disclosure_position": self.target_position}
        if not matching:
            return {"status": "unknown", "records": [], "coverage": "no_reviewed_state_record",
                    "story_anchor": target, "disclosure_position": self.target_position}
        temporal = TemporalIndex()
        for row in self._records(include_candidates=False):
            if row.get("type") == "EVENT" and row.get("relation") in {"before", "after", "same", "during", "overlaps"}:
                if not row.get("left_anchor") or not row.get("right_anchor"):
                    continue
                temporal.add(row.get("left_anchor"), row["relation"], row.get("right_anchor"),
                             branch=row.get("branch", "main"), evidence=row["record_id"])
        applicable, unknown = [], []
        for row in matching:
            start = row.get("valid_from") or row.get("anchor")
            if not start:
                unknown.append(row); continue
            order = temporal.compare(start, target, branch=branch)
            if order in {"same", "before"}:
                if row.get("end_status") == "known":
                    end = temporal.compare(target, row.get("valid_to"), branch=branch)
                    if end in {"same", "before"}:
                        applicable.append(row)
                else:
                    unknown.append({**row, "query_status": "last_observed_not_assumed_persistent"})
            elif order == "uncertain":
                unknown.append(row)
        objects = {r.get("object") for r in applicable}
        polarities = {r.get("polarity", "affirmed") for r in applicable}
        status = "conflicting" if len(objects) > 1 or len(polarities) > 1 else "supported" if applicable else "unknown"
        return {"status": status, "records": applicable + unknown, "temporal_conflicts": temporal.conflicts,
                "story_anchor": target, "disclosure_position": self.target_position,
                "view_fingerprint": self.view_fingerprint}

    def get_timeline(self, *, subject=None, branch="main", include_candidates=False):
        rows = self._records(include_candidates=include_candidates)
        result = []
        for row in rows:
            if row.get("type") not in {"SCENE", "EVENT", "STATE", "PERSPECTIVE"}:
                continue
            if row.get("branch", "main") != branch or (subject and subject not in {row.get("subject"), row.get("actor")}):
                continue
            if row.get("disclosure_order") is not None and self.target_position is not None and row["disclosure_order"] > self.target_position:
                continue
            result.append(row)
        return {"items": result, "coverage": "reviewed_and_candidates_separated",
                "view_fingerprint": self.view_fingerprint, "branch": branch}

    def get_information(self, person, proposition, *, at_anchor=None, branch="main"):
        return information_status(self._records(include_candidates=False), person=person,
                                  proposition=proposition, at_anchor=at_anchor or self.story_anchor.anchor_id,
                                  branch=branch)

    def get_dialogue(self, *, chapter_id=None, include_candidates=True):
        from .dialogue import dialogue_candidates
        return dialogue_candidates(self._records(source_id=chapter_id, include_candidates=include_candidates),
                                   chapter_id=chapter_id, include_unreviewed=include_candidates)

    def explain(self, record_id):
        record = next((r for r in self._records(include_candidates=True) if r.get("record_id") == record_id), None)
        if record is None:
            raise P6Error("not_covered", "record is not in this state view")
        return {"record": record, "story_anchor": self.story_anchor.__dict__,
                "disclosure_position": self.target_position, "perspective": self.perspective,
                "dependencies": self.dependencies, "view_fingerprint": self.view_fingerprint}

    def compile_context(self, base_context, overlay_id, *, max_tokens=8000):
        from .context import compile_state_context
        return compile_state_context(self, base_context, overlay_id, max_tokens=max_tokens)
