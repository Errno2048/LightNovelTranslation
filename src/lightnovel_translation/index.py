"""Explicit generation and read-only operations on independent edition indexes."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from typing import Any, Iterable, TYPE_CHECKING

from translation.editions import EditionPipeline, plan_edition_index
from translation.storage import digest, read_json, write_json

from .errors import TranslationError, operation
from .models import ChapterInfo

if TYPE_CHECKING:
    from .project import TranslationProject


class Index:
    def __init__(self, project: TranslationProject, edition_ids: list[str]):
        self.project, self.edition_ids = project, edition_ids
        self.archive = project._archive(edition_ids)
        output_tokens = project.config.role_budgets()["indexer"].requested_output_tokens
        self.plans = [plan_edition_index(self.archive, ident, project.config.capacity, project.counter,
                                        project.config.index, output_tokens=output_tokens) for ident in edition_ids]
        identity = {"schema": "library-index-v1", "plans": [p["fingerprint"] for p in self.plans],
                    "provider": project.provider.config.public()}
        self.id = digest(identity)
        # Leave room for the engine's hash-named call files on Windows.
        self.path = project.path / "indexes" / self.id[:24]
        manifest = self.path / "index.json"
        if manifest.exists() and read_json(manifest) != identity:
            raise TranslationError("index_conflict", "index directory belongs to a different identity")
        self._pipeline = EditionPipeline(self.archive, self.plans, project.provider, project.counter,
            project.config.capacity, self.path, **asdict(project.config.limits))
        write_json(self.path / "index.json", identity)

    def _check(self) -> None:
        self.project._check_open()
        if self.project._archive(self.edition_ids).fingerprint != self.archive.fingerprint:
            raise ValueError("index input changed")

    def plan(self) -> dict[str, Any]:
        self._check()
        return {"index_id": self.id, "artifact_dir": str(self.path), "local_only": True,
                "plans": deepcopy(self.plans)}

    def nodes(self, *, edition: str | None = None, level: int = 2) -> list[dict[str, Any]]:
        self._check()
        if level not in {1, 2}:
            raise ValueError("index level must be 1 or 2")
        if edition is not None and edition not in self.edition_ids:
            raise ValueError("unknown indexed edition")
        return deepcopy([n for p in self.plans for n in p["parents" if level == 1 else "children"]
                         if edition is None or n["edition_id"] == edition])

    def select_nodes(self, chapters: Iterable[str | ChapterInfo], *, neighbors: int = 0) -> list[dict[str, Any]]:
        """Find L2 nodes for stable chapter IDs, optionally including native neighbors."""
        self._check()
        if type(neighbors) is not int or neighbors < 0:
            raise ValueError('neighbors must be a nonnegative integer')
        ids = {c.id if isinstance(c, ChapterInfo) else c for c in chapters}
        if not ids or not ids <= set(self.archive.entries):
            raise ValueError('chapters must be nonempty and belong to this index')
        chosen = set()
        for edition in self.edition_ids:
            nodes = self.nodes(edition=edition)
            for i, node in enumerate(nodes):
                if ids.intersection(node['entry_ids']):
                    chosen.update(n['node_id'] for n in nodes[max(0,i-neighbors):i+neighbors+1])
        return [n for n in self.nodes() if n['node_id'] in chosen]

    @operation
    def prepare_references(self, source_chapters, reference_chapters, *, workers=1, batch_size=20):
        """Build explicit local candidates, associate L2s and match their L1 catalogs."""
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError('batch_size must be a positive integer')
        source = self.select_nodes(source_chapters)
        reference = self.select_nodes(reference_chapters)
        self._nodes([n['node_id'] for n in source], level=2, language='ja')
        self._nodes([n['node_id'] for n in reference], level=2, language='zh')
        built = self.build([n['node_id'] for n in source+reference], workers=workers)
        result = {'status':'incomplete', 'build':built, 'associations':[], 'entity_matches':[],
                  'source_nodes':[n['node_id'] for n in source],
                  'reference_nodes':[n['node_id'] for n in reference]}
        if not built['complete']:
            return result
        association = self.associate(result['source_nodes'], result['reference_nodes'])
        result['associations'] = [association]
        refs = sorted({n['parent_id'] for n in reference})
        result['entity_matches'] = [self.match_entities(p, refs, batch_size=batch_size)
                                   for p in sorted({n['parent_id'] for n in source})]
        result['status'] = ('complete' if association['status'] == 'complete' and
            all(m['status'] == 'complete' for m in result['entity_matches']) else 'incomplete')
        return result

    @operation
    def resolve_terminology(self, chapter, *, associations=(), entity_matches=(), source_forms=()):
        """Look up names in supplied Chinese prose; missing indexes do not prevent lookup."""
        from translation.editions.terminology import resolve_target_terminology
        self._check()
        ident = chapter.id if isinstance(chapter, ChapterInfo) else chapter
        if ident not in self.archive.entries or self.archive.entries[ident]['language'] != 'ja':
            raise ValueError('terminology target must be an indexed Japanese chapter')
        self.project._require_generation()
        options = self.project.config.references
        budgets = options.budgets(self.project.config.capacity, self.project.config.role_budgets())
        return resolve_target_terminology(self._pipeline, ident, associations, entity_matches,
            source_forms=source_forms, max_terms=options.terminology_max_terms,
            input_tokens=budgets['terminology_input_tokens'], output_tokens=budgets['terminology_output_tokens'],
            max_documents=options.terminology_max_documents, radius=options.terminology_radius,
            discover=options.discover_terms)

    def _nodes(self, ids: Iterable[str], *, level: int, language: str | None = None) -> list[str]:
        ids = list(ids)
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("node IDs must be nonempty and unique")
        for ident in ids:
            node = self._pipeline.nodes.get(ident)
            if not node or node["level"] != level or (language and node["language"] != language):
                raise ValueError("node has the wrong level/language or is absent from this index")
        return ids

    @operation
    def build(self, node_ids: Iterable[str] | None = None, *, workers: int = 1,
              include_parents: bool = False) -> dict[str, Any]:
        """Build selected L2 nodes, serially within L1 and concurrently across L1."""
        self._check()
        if type(workers) is not int or workers <= 0:
            raise ValueError("workers must be a positive integer")
        ids = self._nodes(node_ids if node_ids is not None else [n["node_id"] for n in self.nodes()], level=2)
        self.project._require_generation()
        output = self.project.config.role_budgets()["indexer"].requested_output_tokens
        result = self._pipeline.ensure_index_nodes(ids, workers=workers, output_tokens=output)
        if include_parents:
            parents = sorted({self._pipeline.nodes[n]["parent_id"] for n in ids})
            result["parents"] = [self._pipeline.build_parent(n, output_tokens=output) for n in parents]
            result["complete"] = result["complete"] and all("overview" in p for p in result["parents"])
        return result

    def read(self, node_id: str) -> dict[str, Any]:
        self._check()
        node = self._pipeline.nodes.get(node_id)
        if not node:
            raise ValueError("unknown index node")
        if node["level"] == 2:
            return self._pipeline.lookup_reference(node_id)
        path = self.path / "parents" / (node_id + ".json")
        if not path.is_file():
            return {"status": "not_built", "summary": None, "suggested_build_nodes": node["child_ids"]}
        summary = read_json(path)
        catalog = self._pipeline.entity_catalog(node_id)
        fingerprints = [self._pipeline.read_summary(n)["fingerprint"] for n in catalog["covered_child_ids"]]
        if summary["child_fingerprints"] != fingerprints:
            raise TranslationError("stale_summary", "parent summary differs from current children")
        return {"status": "found", "summary": summary, "suggested_build_nodes": []}

    def entity_catalog(self, parent_id: str) -> dict[str, Any]:
        self._check()
        self._nodes([parent_id], level=1)
        return self._pipeline.entity_catalog(parent_id)

    @operation
    def plan_continuation(self, *, source, references, reference_set,
                          goal="after_latest_reference", endpoint="latest_mainline",
                          source_candidates=()) -> dict[str, Any]:
        """Plan a bounded continuation search locally; never call a model."""
        from .continuation import plan_continuation
        return plan_continuation(self, source=source, references=references, reference_set=reference_set,
                                 goal=goal, endpoint=endpoint, source_candidates=source_candidates)

    @operation
    def locate_continuation(self, plan, *, max_new_nodes=20, max_requests=30, max_rounds=4) -> dict[str, Any]:
        """Find an evidence-backed start; retain ambiguity, search limits and I/O."""
        from .continuation import locate_continuation
        return locate_continuation(self, plan, max_new_nodes=max_new_nodes,
                                   max_requests=max_requests, max_rounds=max_rounds)

    @operation
    def build_parent(self, parent_id: str, *, allow_partial: bool = False) -> dict[str, Any]:
        """Roll up existing child summaries; never implicitly build missing children."""
        self._check()
        self._nodes([parent_id], level=1)
        self.project._require_generation()
        return self._pipeline.build_parent(parent_id, allow_partial=allow_partial,
            output_tokens=self.project.config.role_budgets()["indexer"].requested_output_tokens)

    @operation
    def associate(self, source_nodes: Iterable[str], reference_nodes: Iterable[str]) -> dict[str, Any]:
        """LLM association of explicit L2 candidates, never a proof of equivalence."""
        self._check()
        source = self._nodes(source_nodes, level=2, language="ja")
        reference = self._nodes(reference_nodes, level=2, language="zh")
        self.project._require_generation()
        return self._pipeline.match_blocks(source, reference,
            output_tokens=self.project.config.role_budgets()["indexer"].requested_output_tokens)

    @operation
    def match_entities(self, source_parent: str, reference_parents: Iterable[str], *, batch_size: int = 40) -> dict[str, Any]:
        self._check()
        self._nodes([source_parent], level=1, language="ja")
        references = self._nodes(reference_parents, level=1, language="zh")
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.project._require_generation()
        return self._pipeline.match_entities(source_parent, references, batch_size=batch_size,
            output_tokens=self.project.config.role_budgets()["indexer"].requested_output_tokens)
