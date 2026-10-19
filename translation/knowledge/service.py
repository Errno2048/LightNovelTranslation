"""Versioned knowledge storage and policy-bound read views for P4.

The corpus archive remains the source of text. SQLite stores only reviewed
knowledge, commit history, and a rebuildable chapter/paragraph search index.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from ..storage import digest
from .evidence import CorpusArchive, review_range
from .schemas import (
    KnowledgeError,
    QueryPolicy,
    scope_applicability,
    validate_item,
)


_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"


def _clean(value: str) -> str:
    return "".join(ch for ch in value if ch not in _ZERO_WIDTH)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default):
    return default if value is None else json.loads(value)


SCHEMA = "knowledge-store-v1"


class KnowledgeView:
    def __init__(self, kb: "KnowledgeBase", *, commit_id: str, policy: QueryPolicy,
                 target_position: int | None, purpose: str):
        self.kb = kb
        self.commit_id = commit_id
        self.policy = policy
        self.target_position = target_position
        self.purpose = purpose

    @property
    def fingerprint(self) -> str:
        return digest({"db": self.kb.db_identity, "commit": self.commit_id,
                       "policy": self.policy.public(), "target_position": self.target_position,
                       "purpose": self.purpose})

    def _visible_chapter(self, chapter: dict, *, reference=False) -> bool:
        try:
            self.kb.archive.check_allowed(chapter, self.policy, reference=reference)
            return True
        except KnowledgeError:
            return False

    def _source_allowed(self, source_id: str, language: str) -> bool:
        chapter = self.kb.archive.chapters.get(source_id)
        if chapter is None:
            return False
        if language == "ja":
            try:
                self.kb.archive.check_allowed(chapter, self.policy)
                return True
            except KnowledgeError:
                return False
        return False

    def _items(self) -> list[dict]:
        visible = []
        all_items = {i["id"]: i for i in self.kb._items_at(self.commit_id)}
        def current(item, seen=None):
            seen = set(seen or ())
            if item["id"] in seen or item.get("status") in {"rejected", "retracted"}:
                return False
            seen.add(item["id"])
            return all(parent in all_items and all_items[parent]["revision_id"] == revision
                       and current(all_items[parent], seen) for parent, revision in item.get("knowledge_dependency_revisions", {}).items())
        for item in all_items.values():
            if not current(item):
                continue
            if item.get("status") in {"retracted", "rejected"}:
                continue
            try:
                if set(item["source_dependencies"]) & set(self.policy.hidden_source_ids + self.policy.reserved_source_ids):
                    continue
                for sid in item["source_dependencies"]:
                    self.kb.archive.check_allowed(self.kb.archive.chapters[sid], self.policy)
                for e in item["evidence"]:
                    self.kb.archive.validate_evidence(e, self.policy)
            except (KnowledgeError, KeyError):
                continue
            visible.append(item)
        return visible

    def search(self, query: str, scopes: Iterable[str] | None = None, limit: int = 10) -> list[dict]:
        if not isinstance(query, str) or not query.strip() or limit <= 0:
            return []
        scopes = set(scopes or {"title", "body", "convention"})
        eligible = [r[0] for r in self.kb._conn.execute("SELECT chapter_id FROM docs WHERE language='ja'")
                    if self._visible_chapter(self.kb.archive.chapters[r[0]])]
        # Filter the corpus before ranking/limiting. Hidden documents never
        # affect rank statistics, including when an optional zh index exists.
        rows = self.kb._search_docs(query, limit, eligible)
        results = []
        for row in rows:
            if row["language"] == "zh":
                # A hidden reference's title/body must never influence ranking.
                continue
            chapter = self.kb.archive.chapters.get(row["chapter_id"])
            if chapter is None or not self._visible_chapter(chapter):
                continue
            hit_scopes = []
            if "title" in scopes and query.casefold() in row["title"].casefold():
                hit_scopes.append("title")
            if "body" in scopes and query.casefold() in row["text"].casefold():
                hit_scopes.append("body")
            if "convention" in scopes:
                for item in self._items():
                    if item.get("kind") == "convention" and query in {item.get("source_form"), item.get("target_form"), *item.get("variants", [])}:
                        hit_scopes.append("convention")
                        break
            if not hit_scopes:
                continue
            self.kb.archive.ensure_rows([chapter["chapter_id"]])
            matched = [p for p in self.kb.archive.rows[chapter["chapter_id"]]
                       if p["kind"] == "body" and query.casefold() in p["text"].casefold()][:3]
            refs = [{"chapter_id": chapter["chapter_id"], "paragraph_ids": [p["paragraph_id"]]} for p in matched]
            results.append({"chapter_id": row["chapter_id"], "source_id": self.kb.archive.canonical(chapter),
                            "language": row["language"], "order": row["chapter_order"], "title": row["title"],
                            "score": row["score"], "scopes": sorted(set(hit_scopes)),
                            "coverage": "indexed", "reason": "text/title index match",
                            "revision_id": row["revision_id"], "evidence_refs": refs,
                            "knowledge_coverage": "unknown",
                            "neighbors": [c["chapter_id"] for c in self.kb.archive.chapters.values()
                                          if c["language"] == "ja" and abs(c["order"] - chapter["order"]) == 1
                                          and self._visible_chapter(c)]})
            if len(results) >= limit:
                break
        return results

    def read_evidence(self, refs: Iterable[dict], surrounding: int = 0) -> list[dict]:
        if surrounding < 0 or surrounding > 8:
            raise KnowledgeError("unsupported_query", "surrounding must be between 0 and 8")
        output = []
        for ref in refs:
            if not isinstance(ref, dict) or not ref.get("chapter_id") or not ref.get("paragraph_ids"):
                raise KnowledgeError("invalid_record", "evidence reference requires chapter and paragraphs")
            evidence = self.kb.archive.evidence(ref["chapter_id"], ref["paragraph_ids"], self.policy,
                                                 purpose=self.purpose)
            self.kb.archive.validate_evidence(evidence, self.policy)
            if surrounding:
                rows = self.kb.archive.rows[evidence["chapter_id"]]
                selected = set(evidence["paragraph_ids"])
                ordinals = [p["ordinal"] for p in rows if p["paragraph_id"] in selected]
                low, high = min(ordinals), max(ordinals)
                ids = [p["paragraph_id"] for p in rows if low - surrounding <= p["ordinal"] <= high + surrounding
                       and p["kind"] == "body" and p["text"].strip()]
                evidence = self.kb.archive.evidence(evidence["chapter_id"], ids, self.policy, purpose=self.purpose)
            output.append(evidence)
        return output

    def get_conventions(self, text: str = "", entities: Iterable[str] | None = None) -> dict:
        entities = set(entities or ())
        selected, conflicts = [], []
        for item in self._items():
            if item.get("kind") != "convention" or item.get("status") not in {"accepted", "frozen"}:
                continue
            applicability = "uncertain"
            if self.target_position is not None:
                applicability = scope_applicability(item.get("scope", {}), self.target_position)
            if applicability == "not_applicable":
                continue
            forms = {item.get("source_form", ""), item.get("target_form", ""), *item.get("variants", [])}
            if text and item["category"] not in {"style", "voice"} and not any(form and form in text for form in forms):
                if not entities or not entities.intersection(forms):
                    continue
            selected.append({"item_id": item["id"], "source_form": item["source_form"],
                             "target_form": item["target_form"], "category": item["category"],
                             "variants": item.get("variants", []), "strength": item.get("strength", "preferred"),
                             "applicability": applicability, "scope": item.get("scope", {}),
                             "evidence_ids": item.get("evidence_ids", [])})
        by_source: dict[str, list[dict]] = {}
        for item in selected:
            by_source.setdefault(item["source_form"], []).append(item)
        for source, values in by_source.items():
            if len({v["target_form"] for v in values}) > 1:
                conflicts.append({"source_form": source, "items": values})
        return {"conventions": selected, "conflicts": conflicts,
                "unknown": not selected, "view_fingerprint": self.fingerprint}

    def resolve_mentions(self, text: str) -> dict:
        candidates = []
        for item in self._items():
            if item.get("kind") != "entity" or item.get("status") not in {"accepted", "frozen"}:
                continue
            aliases = [item.get("canonical", ""), *item.get("aliases", [])]
            matched = [a for a in aliases if a and a in text]
            if matched:
                candidates.append({"entity_id": item["id"], "matched": matched,
                                   "aliases": aliases, "evidence_ids": item.get("evidence_ids", []),
                                   "canonical": item.get("canonical", ""), "voice": item.get("voice", []),
                                   "ambiguity": item.get("ambiguity", "unknown")})
        return {"candidates": candidates, "view_fingerprint": self.fingerprint}

    def explain(self, item_id: str) -> dict:
        item = next((x for x in self._items() if x.get("id") == item_id), None)
        if item is None:
            raise KnowledgeError("not_covered", "item is not visible in this snapshot")
        evidence = item["evidence"]
        return {"item": item, "evidence": evidence, "commit_id": self.commit_id,
                "view_fingerprint": self.fingerprint,
                "applicability": "unknown" if self.target_position is None else
                scope_applicability(item.get("scope", {}), self.target_position) if item.get("kind") == "convention" else "recorded"}


class KnowledgeBase:
    def __init__(self, archive: CorpusArchive, db_path: Path):
        self.archive = archive
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    @property
    def db_identity(self) -> str:
        return digest({"corpus": self.archive.fingerprint, "schema": SCHEMA})

    def close(self):
        self._conn.close()

    def bind_policy(self, policy: QueryPolicy):
        """Permanently restrict commits in an evaluation fold database."""
        row = self._conn.execute("SELECT value FROM meta WHERE key='fold_policy'").fetchone()
        if row and json.loads(row[0]) != policy.public():
            raise KnowledgeError("conflict", "fold database policy is already frozen")
        for item in self._items_at(self._head("main")):
            if set(item["source_dependencies"]) & set(policy.hidden_source_ids + policy.reserved_source_ids):
                raise KnowledgeError("visibility_denied", "existing item violates fold policy")
            for evidence in item["evidence"]:
                self.archive.validate_evidence(evidence, policy)
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO meta VALUES ('fold_policy',?)", (_json(policy.public()),))

    def _init_schema(self):
        self._conn.executescript("""
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS branches (name TEXT PRIMARY KEY, head TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS commits (
            commit_id TEXT PRIMARY KEY, branch TEXT NOT NULL, parent TEXT,
            created REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS idempotency (
            branch TEXT NOT NULL, idem TEXT NOT NULL, commit_id TEXT NOT NULL,
            PRIMARY KEY(branch, idem)
        );
        CREATE TABLE IF NOT EXISTS items (
            row_id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
            revision_id TEXT NOT NULL, commit_id TEXT NOT NULL, kind TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL, evidence_ids TEXT NOT NULL,
            source_dependencies TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS items_item_idx ON items(item_id, row_id);
        CREATE TABLE IF NOT EXISTS snapshots (
            snapshot_id TEXT PRIMARY KEY, commit_id TEXT NOT NULL, frozen REAL NOT NULL,
            manifest TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS docs (
            chapter_id TEXT PRIMARY KEY, language TEXT NOT NULL, source_id TEXT,
            title TEXT NOT NULL, text TEXT NOT NULL, chapter_order INTEGER NOT NULL,
            revision_id TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contexts (fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(chapter_id UNINDEXED, title, text);
        """)
        root = self._conn.execute("SELECT commit_id FROM commits WHERE commit_id='root'").fetchone()
        if root is None:
            self._conn.execute("INSERT INTO commits VALUES ('root','main',NULL,?,'root',?)", (time.time(), _json({})))
            self._conn.execute("INSERT INTO branches VALUES ('main','root')")
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('schema',?)", (SCHEMA,))
            self._conn.execute("INSERT OR REPLACE INTO meta VALUES ('corpus_fingerprint',?)", (self.archive.fingerprint,))
            self._conn.commit()
        else:
            fp = self._conn.execute("SELECT value FROM meta WHERE key='corpus_fingerprint'").fetchone()
            if fp and fp[0] != self.archive.fingerprint:
                raise KnowledgeError("stale_evidence", "knowledge database belongs to another corpus")

    def build_index(self, *, languages=("ja",), force=False) -> dict:
        wanted = [c for c in self.archive.chapters.values() if c.get("language") in set(languages)]
        if force:
            self._conn.execute("DELETE FROM docs")
            self._conn.execute("DELETE FROM docs_fts")
        existing = {r[0] for r in self._conn.execute("SELECT chapter_id FROM docs")}
        pending = [c for c in wanted if c["chapter_id"] not in existing]
        self.archive.ensure_rows([c["chapter_id"] for c in pending])
        added = 0
        for chapter in pending:
            text = "\n".join(_clean(p["text"]) for p in self.archive.rows[chapter["chapter_id"]]
                             if p["kind"] == "body" and p.get("section", {}).get("semantic_status") != "known_numeric_conflict")
            title = _clean(chapter.get("title") or "")
            source_id = self.archive.canonical(chapter)
            self._conn.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?)", (chapter["chapter_id"], chapter["language"], source_id,
                title, text, chapter["order"], chapter["revision_id"]))
            self._conn.execute("INSERT INTO docs_fts VALUES (?,?,?)", (chapter["chapter_id"], title, text))
            added += 1
        self._conn.commit()
        return {"schema": SCHEMA, "corpus_fingerprint": self.archive.fingerprint,
                "indexed": self._conn.execute("SELECT count(*) FROM docs").fetchone()[0], "added": added,
                "languages": list(languages)}

    def _search_docs(self, query: str, limit: int, eligible: list[str]) -> list[dict]:
        terms = [x for x in re.findall(r"[\w\u3040-\u30ff\u3400-\u9fff]+", query) if x]
        if not terms or not eligible:
            return []
        # Substring matching handles unsegmented Japanese and two-character
        # names. Per-document scores avoid corpus-wide BM25 leakage.
        pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        placeholders = ",".join("?" for _ in eligible)
        rows = self._conn.execute(
            f"SELECT *, CASE WHEN title LIKE ? ESCAPE '\\' THEN -1.0 ELSE 0.0 END AS score "
            f"FROM docs WHERE chapter_id IN ({placeholders}) AND (title LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\') "
            "ORDER BY score, chapter_order, chapter_id LIMIT ?",
            [pattern, *eligible, pattern, pattern, limit]).fetchall()
        return [dict(r) for r in rows]

    def review_candidates(self, branch: str, batch: Iterable[dict], decisions: dict[str, dict], *,
                          expected_head: str | None = None, idempotency_key: str | None = None) -> dict:
        changes = []
        for candidate in batch:
            item_id = candidate.get("id")
            decision = decisions.get(item_id, {})
            status = decision.get("status", "candidate")
            if status not in {"accepted", "rejected", "candidate", "frozen"}:
                raise KnowledgeError("invalid_record", "invalid candidate decision")
            if status in {"accepted", "frozen"} and (not decision.get("method") or not decision.get("reason")):
                raise KnowledgeError("invalid_record", "acceptance requires review method and reason")
            item = dict(candidate)
            overrides = decision.get("overrides", {})
            if set(overrides) - {"strength", "scope", "variants", "category"}:
                raise KnowledgeError("invalid_record", "unsupported review override")
            item.update(overrides)
            item["status"] = status
            item["review"] = {"method": decision.get("method", "unreviewed"),
                               "reason": decision.get("reason", ""), "overrides": overrides}
            changes.append(item)
        return self.commit(branch, changes, expected_head=expected_head,
                           idempotency_key=idempotency_key, kind="review")

    def _items_at(self, commit_id: str) -> list[dict]:
        chain, seen = [], set()
        current = commit_id
        while current and current not in seen:
            seen.add(current)
            row = self._conn.execute("SELECT * FROM commits WHERE commit_id=?", (current,)).fetchone()
            if row is None:
                raise KnowledgeError("stale_evidence", "unknown commit")
            chain.append(row)
            current = row["parent"]
        latest = {}
        for commit in reversed(chain):
            payload = json.loads(commit["payload"])
            for change in payload.get("changes", []):
                latest[change["id"]] = change
        return list(latest.values())

    def _evidence_by_id(self, evidence_id: str) -> dict:
        # Evidence is immutable and reproducible from its chapter/paragraph IDs.
        for row in self._conn.execute("SELECT payload FROM items"):
            item = json.loads(row[0])
            if evidence_id in item.get("evidence_ids", []):
                for ref in item.get("evidence", []):
                    if ref.get("id") == evidence_id:
                        return ref
        raise KnowledgeError("stale_evidence", "evidence is not present in the committed item")

    def open_view(self, *, snapshot_id: str | None = None, branch_commit: str | None = None,
                  visibility_policy: QueryPolicy, target_position: int | None = None,
                  story_anchor=None, perspective=None, purpose="translation") -> KnowledgeView:
        if story_anchor is not None or perspective is not None:
            raise KnowledgeError("unsupported_query", "story_anchor/perspective are not implemented by the legacy knowledge view; use the P6 state view")
        if snapshot_id:
            row = self._conn.execute("SELECT commit_id FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
            if row is None:
                raise KnowledgeError("not_covered", "unknown snapshot")
            commit_id = row[0]
        elif branch_commit:
            commit_id = branch_commit
        else:
            row = self._conn.execute("SELECT head FROM branches WHERE name='main'").fetchone()
            commit_id = row[0]
        return KnowledgeView(self, commit_id=commit_id, policy=visibility_policy,
                             target_position=target_position, purpose=purpose)

    def _head(self, branch: str) -> str:
        row = self._conn.execute("SELECT head FROM branches WHERE name=?", (branch,)).fetchone()
        if row is None:
            raise KnowledgeError("not_covered", "unknown branch")
        return row[0]

    def commit(self, branch: str, changes: Iterable[dict], *, expected_head: str | None = None,
               idempotency_key: str | None = None, kind="knowledge") -> dict:
        changes = [dict(c) for c in changes]
        head = self._head(branch)
        # Validate before opening the write transaction; nothing is accepted on
        # a partial failure. All derived records carry the dependency closure.
        existing_items = {i["id"]: i for i in self._items_at(head)}
        for item in changes:
            validate_item(item)
            item.setdefault("status", "candidate")
            if item["status"] not in {"candidate", "accepted", "frozen", "rejected", "retracted"}:
                raise KnowledgeError("invalid_record", "unsupported knowledge status")
            item.setdefault("evidence", [])
            if set(item["evidence_ids"]) != {e.get("id") for e in item["evidence"]}:
                raise KnowledgeError("stale_evidence", "evidence ID set mismatch")
            bound = self._conn.execute("SELECT value FROM meta WHERE key='fold_policy'").fetchone()
            policy = QueryPolicy(**json.loads(bound[0])) if bound else QueryPolicy("single_gap")
            if set(item["source_dependencies"]) & set(policy.hidden_source_ids + policy.reserved_source_ids):
                raise KnowledgeError("visibility_denied", "knowledge dependency is hidden/reserved in this fold")
            for evidence in item["evidence"]:
                if evidence.get("corpus_fingerprint") != self.archive.fingerprint:
                    raise KnowledgeError("stale_evidence", "evidence corpus mismatch")
                self.archive.validate_evidence(evidence, policy)
            deps = {d for e in item["evidence"] for d in e["source_dependencies"]}
            for parent in item.get("knowledge_dependencies", []):
                if parent not in existing_items or existing_items[parent]["status"] in {"rejected", "retracted"}:
                    raise KnowledgeError("stale_evidence", "unknown or retracted knowledge dependency")
                deps.update(existing_items[parent]["source_dependencies"])
            item["knowledge_dependency_revisions"] = {p: existing_items[p]["revision_id"] for p in item.get("knowledge_dependencies", [])}
            if not deps <= set(item["source_dependencies"]):
                raise KnowledgeError("invalid_record", "incomplete source dependency closure")
            if any(sid not in self.archive.chapters or self.archive.chapters[sid]["language"] != "ja"
                   for sid in item["source_dependencies"]):
                raise KnowledgeError("invalid_record", "unknown source dependency")
            if item["status"] in {"accepted", "frozen"} and item["kind"] == "convention":
                review = item.get("parallel_review")
                if not review:
                    raise KnowledgeError("invalid_record", "accepted convention requires parallel review")
                actual = review_range(self.archive, policy, {k: v for k, v in review.items()
                                                           if k not in {"review_id", "evidence", "policy_fingerprint"}})
                if actual["decision"] != "accepted" or not actual["purposes"].get("convention"):
                    raise KnowledgeError("invalid_record", "range does not authorize convention extraction")
                if not {e["id"] for e in actual["evidence"]} <= set(item["evidence_ids"]):
                    raise KnowledgeError("invalid_record", "review is not bound to item evidence")
                if not item.get("review", {}).get("method") or not item.get("review", {}).get("reason"):
                    raise KnowledgeError("invalid_record", "semantic review required for accepted convention")
                literal_source, literal_target = [e["text"] for e in actual["evidence"]]
                if item["source_form"] not in literal_source or item["target_form"] not in literal_target:
                    raise KnowledgeError("stale_evidence", "accepted forms must quote reviewed evidence")
                if any(v not in literal_source and v not in literal_target for v in item.get("variants", [])):
                    raise KnowledgeError("stale_evidence", "unobserved variant requires additional evidence")
            if item["status"] in {"accepted", "frozen"} and item["kind"] == "entity":
                if not item.get("review", {}).get("method") or not item.get("review", {}).get("reason"):
                    raise KnowledgeError("invalid_record", "entity acceptance requires review")
                source_text = "\n".join(e["text"] for e in item["evidence"] if e["language"] == "ja")
                if any(a not in source_text for a in [item.get("canonical", ""), *item["aliases"]]):
                    raise KnowledgeError("stale_evidence", "entity name is not observed in evidence")
            item["revision_id"] = digest({k: v for k, v in item.items() if k != "revision_id"})
        if idempotency_key:
            prior = self._conn.execute("SELECT commit_id FROM idempotency WHERE branch=? AND idem=?", (branch, idempotency_key)).fetchone()
            if prior:
                saved = json.loads(self._conn.execute("SELECT payload FROM commits WHERE commit_id=?", (prior[0],)).fetchone()[0])
                if saved["changes"] != changes:
                    raise KnowledgeError("conflict", "idempotency key reused with different content")
                return {"commit_id": prior[0], "idempotent": True, "head": prior[0]}
        if expected_head is not None and expected_head != head:
            raise KnowledgeError("stale_evidence", "branch head changed")
        commit_id = "commit:" + digest({"branch": branch, "parent": head, "changes": changes, "kind": kind})[:24]
        payload = {"changes": changes}
        try:
            with self._conn:
                self._conn.execute("INSERT INTO commits VALUES (?,?,?,?,?,?)", (commit_id, branch, head, time.time(), kind, _json(payload)))
                for item in changes:
                    self._conn.execute("INSERT INTO items(item_id,revision_id,commit_id,kind,status,payload,evidence_ids,source_dependencies) VALUES (?,?,?,?,?,?,?,?)",
                                       (item["id"], item["revision_id"], commit_id, item["kind"], item["status"], _json(item), _json(item.get("evidence_ids", [])), _json(item.get("source_dependencies", []))))
                updated = self._conn.execute("UPDATE branches SET head=? WHERE name=? AND head=?", (commit_id, branch, head))
                if updated.rowcount != 1:
                    raise KnowledgeError("conflict", "branch head changed during transaction")
                if idempotency_key:
                    self._conn.execute("INSERT INTO idempotency VALUES (?,?,?)", (branch, idempotency_key, commit_id))
        except sqlite3.IntegrityError as exc:
            raise KnowledgeError("conflict", "commit already exists or idempotency key conflicts") from exc
        return {"commit_id": commit_id, "head": commit_id, "idempotent": False, "changed": len(changes)}

    def freeze(self, branch="main") -> dict:
        head = self._head(branch)
        snapshot_id = "snapshot:" + digest({"branch": branch, "head": head, "corpus": self.archive.fingerprint})[:24]
        manifest = {"schema": SCHEMA, "branch": branch, "commit_id": head, "corpus_fingerprint": self.archive.fingerprint,
                    "items": len(self._items_at(head))}
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?,?)", (snapshot_id, head, time.time(), _json(manifest)))
        return {"snapshot_id": snapshot_id, **manifest}

    def export_snapshot(self, snapshot_id: str, destination: Path) -> dict:
        from ..storage import write_json
        row = self._conn.execute("SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KnowledgeError("not_covered", "unknown snapshot")
        result = {"snapshot_id": snapshot_id, **json.loads(row["manifest"]),
                  "records": sorted(self._items_at(row["commit_id"]), key=lambda x: x["id"])}
        result["fingerprint"] = digest(result)
        if Path(destination).exists():
            from ..storage import read_json
            if read_json(Path(destination)) != result:
                raise KnowledgeError("conflict", "snapshot export is immutable")
        write_json(Path(destination), result)
        return result

    def impact(self, item_id: str, branch="main") -> dict:
        items = self._items_at(self._head(branch))
        affected = {item_id}
        while True:
            more = {i["id"] for i in items if set(i.get("knowledge_dependencies", [])) & affected}
            if more <= affected:
                break
            affected |= more
        contexts = [r[0] for r in self._conn.execute("SELECT fingerprint, payload FROM contexts")
                    if any(i["id"] in affected for i in json.loads(r[1])["knowledge_items"])]
        return {"item_ids": sorted(affected), "context_fingerprints": sorted(contexts)}

    def record_context(self, bundle):
        with self._conn:
            self._conn.execute("INSERT OR IGNORE INTO contexts VALUES (?,?)",
                               (bundle.fingerprint, _json(bundle.dependency_manifest)))

    def fork(self, snapshot_id: str, branch: str) -> dict:
        row = self._conn.execute("SELECT commit_id FROM snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise KnowledgeError("not_covered", "unknown snapshot")
        if self._conn.execute("SELECT 1 FROM branches WHERE name=?", (branch,)).fetchone():
            raise KnowledgeError("conflict", "branch already exists")
        with self._conn:
            self._conn.execute("INSERT INTO branches VALUES (?,?)", (branch, row[0]))
        return {"branch": branch, "head": row[0], "snapshot_id": snapshot_id}

    def revise(self, branch: str, item_id: str, replacement: dict, *, expected_head=None, idempotency_key=None) -> dict:
        replacement = dict(replacement)
        replacement["id"] = item_id
        replacement["status"] = replacement.get("status", "candidate")
        return self.commit(branch, [replacement], expected_head=expected_head, idempotency_key=idempotency_key, kind="revision")

    def retract(self, branch: str, item_id: str, *, expected_head=None, idempotency_key=None) -> dict:
        current = next((x for x in self._items_at(self._head(branch)) if x.get("id") == item_id), None)
        if current is None:
            raise KnowledgeError("not_covered", "item is not present")
        replacement = dict(current)
        replacement["status"] = "retracted"
        return self.commit(branch, [replacement], expected_head=expected_head, idempotency_key=idempotency_key, kind="retraction")
