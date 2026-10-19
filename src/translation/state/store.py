"""P6 append-only overlay and analysis request ledger backed by NarrativeStore."""
from __future__ import annotations

from dataclasses import asdict

from ..storage import digest
from .schemas import P6Error, REVIEW_STATES, SCHEMA_VERSION, validate_record


class StateStore:
    def __init__(self, narrative_store):
        self.store = narrative_store

    def add_candidate(self, task_id, policy, dependencies, records, *, evidence_texts):
        if not task_id or not isinstance(dependencies, list):
            raise P6Error("invalid_record", "analysis task and dependency list required")
        allowed = {k: v for k, v in evidence_texts.items()}
        checked = [{**validate_record(r, allowed_refs=allowed), "review_status": "candidate"} for r in records]
        data = {"schema": SCHEMA_VERSION, "task_id": task_id, "policy": policy,
                "dependencies": dependencies, "records": checked,
                "admission": "candidate_only", "review_status": "unreviewed"}
        oid = self.store.put("p6-analysis-overlay", data)
        return {"overlay_id": oid, **data}

    def review(self, overlay_id, decisions, *, reviewer, method, reason):
        old = self.store.get(overlay_id, "p6-analysis-overlay")
        if not reviewer or not method or not reason or not decisions:
            raise P6Error("invalid_record", "explicit review identity, method, reason and decisions required")
        by_id = {r["record_id"]: r for r in old["records"]}
        if set(decisions) - set(by_id):
            raise P6Error("not_covered", "review references an unknown P6 record")
        records = []
        for row in old["records"]:
            decision = decisions.get(row["record_id"], "candidate")
            if decision not in REVIEW_STATES:
                raise P6Error("invalid_record", "unsupported review state")
            records.append({**row, "review_status": decision,
                            "review": {"reviewer": reviewer, "method": method, "reason": reason}
                            if decision in {"reviewed", "rejected", "retracted"} else None})
        data = {**old, "records": records, "admission": "reviewed_revision",
                "review_status": "reviewed_with_candidates" if any(r["review_status"] == "candidate" for r in records) else "reviewed"}
        return {"overlay_id": self.store.put("p6-analysis-overlay", data), **data}

    def commit_overlay(self, overlay_id, state_view, *, parent_commit=None, reviewer, method, reason):
        """Create an immutable knowledge revision; unreviewed rows remain candidates."""
        overlay = self.read_overlay(overlay_id, view=state_view)
        if not reviewer or not method or not reason:
            raise P6Error("invalid_record", "commit requires reviewer, method and reason")
        if parent_commit:
            self.store.get(parent_commit, "p6-state-commit")
        data = {"schema": SCHEMA_VERSION, "parent": parent_commit, "overlay_id": overlay_id,
            "policy_at_commit": state_view.view_fingerprint, "dependencies": overlay["dependencies"],
            "changes": overlay["records"], "reviewer": reviewer, "method": method, "reason": reason}
        return {"commit_id": self.store.put("p6-state-commit", data), **data}

    def read_commit(self, commit_id):
        chain, seen = [], set()
        current = commit_id
        while current:
            if current in seen:
                raise P6Error("invalid_record", "P6 knowledge commit cycle")
            seen.add(current)
            data = self.store.get(current, "p6-state-commit")
            chain.append((current, data))
            current = data.get("parent")
        latest = {}
        for _, commit in reversed(chain):
            for row in commit["changes"]:
                latest[row["record_id"]] = row
        return {"commit_id": commit_id, "records": list(latest.values()),
                "dependencies": [dep for _, c in chain for dep in c["dependencies"]]}

    def read_overlay(self, overlay_id, *, view=None):
        data = self.store.get(overlay_id, "p6-analysis-overlay")
        if data.get("schema") != SCHEMA_VERSION:
            raise P6Error("stale_evidence", "P6 overlay schema changed")
        if view is not None:
            narrative = getattr(view, "narrative", view)
            expected_policy = getattr(view, "view_fingerprint", narrative.policy.fingerprint)
            if data["policy"] != expected_policy:
                raise P6Error("visibility_denied", "overlay belongs to another policy/view")
            for dep in data["dependencies"]:
                target_id = getattr(view, "target_id", narrative.policy.target_id)
                narrative.validate_dependency(dep, task=dep["chapter_id"] == target_id)
        return data


def reserve_generation(store, identity, kind, request, *, input_tokens, limits):
    return store.reserve(identity, kind, input_tokens, request.max_tokens, limits)
