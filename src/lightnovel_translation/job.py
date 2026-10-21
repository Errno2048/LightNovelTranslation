"""Durable translation jobs with audit, incremental knowledge and compression."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from translation.editions.sequence import build_sequence_inputs, _source_document
from translation.narrative.schemas import NarrativeError
from translation.providers import ProviderError
from translation.sequence import SequenceEngine, SequenceRepository, create_sequence
from translation.storage import atomic_text, digest, read_json, write_json, write_jsonl

from .errors import TranslationError, operation
from .models import ChapterInfo, RunResult

if TYPE_CHECKING:
    from .project import TranslationProject


def create_job(project, name, *, source, chapters, mode, references, index, parallel_pairs,
               associations, entity_matches, resolve_terminology, packet_token_budget,
               style_reference_chapters=()):
    source_id = project._edition_id(source)
    refs = [project._edition_id(e) for e in references]
    if source_id in refs or len(set(refs)) != len(refs):
        raise ValueError("reference editions must be unique and different from the source")
    if mode not in {"auto", "source_only", "parallel", "cross_edition"}:
        raise ValueError("unsupported reference mode")
    pairs = parallel_pairs or {}
    associations, entity_matches = list(associations), list(entity_matches)
    if mode == "auto":
        mode = "parallel" if pairs else "cross_edition" if refs else "source_only"
    if mode == "source_only" and (refs or pairs or associations or entity_matches or resolve_terminology):
        raise ValueError("source_only cannot accept reference materials")
    if mode == "parallel" and (not pairs or not refs or associations or entity_matches or resolve_terminology):
        raise ValueError("parallel requires reference editions and explicit historical pairs only")
    if mode == "cross_edition" and (not refs or pairs):
        raise ValueError("cross_edition requires independent reference editions, without parallel pairs")
    budgets = project.config.references.budgets(project.config.capacity, project.config.role_budgets())
    if packet_token_budget is None:
        packet_token_budget = budgets['packet_token_budget']
    if type(packet_token_budget) is not int or packet_token_budget <= 0:
        raise ValueError("packet_token_budget must be positive")
    packet_token_budget = min(packet_token_budget, budgets['packet_capacity_tokens'])
    style_ids = [c.id if isinstance(c, ChapterInfo) else c for c in style_reference_chapters]
    if style_ids and mode != 'cross_edition':
        raise ValueError('explicit style samples require cross_edition mode')
    if index is None:
        index = project.index([source_id, *refs])
    if index.project is not project or {source_id, *refs} != set(index.edition_ids):
        raise ValueError("index must contain exactly this project's source and declared reference editions")
    index._check()
    archive = index.archive
    docs = archive.documents(source_id)
    if not docs or any(d["language"] != "ja" for d in docs):
        raise ValueError("translation source must be a Japanese narrative edition")
    if any(not archive.documents(r) or any(d["language"] != "zh" for d in archive.documents(r)) for r in refs):
        raise ValueError("reference editions must be Chinese")
    selected = [c.id if isinstance(c, ChapterInfo) else c for c in chapters] if chapters is not None else [d["entry_id"] for d in docs]
    available = [d["entry_id"] for d in docs]
    if (not selected or len(set(selected)) != len(selected) or any(c not in available for c in selected)):
        raise ValueError("targets must be unique chapters of the source edition")
    start = available.index(selected[0])
    if available[start:start+len(selected)] != selected:
        raise ValueError("targets must form a consecutive reading window in source order")
    if resolve_terminology:
        project._require_generation()
    inputs = build_sequence_inputs(index._pipeline, selected, associations=associations,
        entity_matches=entity_matches, resolve_terminology=resolve_terminology,
        packet_token_budget=packet_token_budget, reference_options=asdict(project.config.references),
        terminology_budgets=budgets, style_reference_entry_ids=style_ids)
    if mode != "cross_edition":
        inputs.pop("edition_reference_packets", None)
        inputs["reference_mode"] = mode
    # Freeze all preceding Japanese bodies for navigation, independently of
    # translation alignment. Missing materialization is not a missing source.
    inputs["history_sources"] = [_source_document(archive, d["entry_id"]) for d in docs[:start]]
    inputs["history_index_profile"] = "hierarchy-v2"
    # Every preceding Web body is frozen in history_sources.  The previous
    # "index not built" marker is no longer an input gap once that body is
    # available; genuine missing-source gaps remain caller-supplied records.
    inputs["explicit_gaps"] = [
        {**gap, "source_ids":[sid for sid in gap["source_ids"]
            if sid not in {d["entry_id"] for d in docs[:start]}]}
        for gap in inputs.get("explicit_gaps", [])
        if gap.get("reason") != "Web historical index not built in the frozen pilot"]
    if mode == "parallel":
        history, used = [], set()
        reference_ids = {d["entry_id"] for r in refs for d in archive.documents(r)}
        cutoff = docs[start]["edition_order"]
        for sid, translations in pairs.items():
            if sid not in available or archive.entries[sid]["edition_order"] >= cutoff:
                raise ValueError("parallel history must strictly precede every target chapter")
            if isinstance(translations, (str, bytes)) or not translations:
                raise ValueError("each parallel value must be a nonempty list of translation chapter IDs")
            src = _source_document(archive, sid)
            target_docs = []
            for tid in translations:
                entry = archive.entries.get(tid)
                if not entry or tid not in reference_ids or entry["language"] != "zh" or tid in used:
                    raise ValueError("translation is absent, reused, or outside the declared reference editions")
                used.add(tid)
                translated = _source_document(archive, tid)
                translated.update(order=src["order"], match_key=src["match_key"], canonical_source_id=sid)
                target_docs.append(translated)
            history.append({"source": src, "translation": target_docs, "semantic_status": "caller_asserted_parallel"})
        inputs["history_pairs"] = sorted(history, key=lambda p: p["source"]["order"])
        # Caller-confirmed bilingual pairs override source-only navigation.
        represented = {p["source"]["chapter_id"] for p in history}
        for gap in inputs.get("explicit_gaps", []):
            gap["source_ids"] = [sid for sid in gap["source_ids"] if sid not in represented]
    folder = project.path / "jobs" / name
    folder.mkdir(parents=True, exist_ok=True)
    spec = project.config.sequence_spec()
    repo = create_sequence(folder / "sequence.sqlite", spec, inputs, branch_name=name,
        provider_identity=project.provider.config.public(), counter_identity=project.counter.identity)
    try:
        metadata = {"schema": "novelfetch-job-v1", "name": name, "run_id": repo.run_id,
                    "reference_mode": mode, "index_id": index.id, "source_edition": source_id,
                    "reference_editions": refs, "frozen_input_hash": digest(inputs)}
        metadata_path = folder / "job.json"
        if metadata_path.exists() and read_json(metadata_path) != metadata:
            raise TranslationError("job_conflict", "job name is bound to another frozen input")
        write_json(metadata_path, metadata)
        write_json(folder / "config.redacted.json", project.config.public())
    finally:
        repo.close()
    return TranslationJob(project, name)


class TranslationJob:
    """One frozen, resumable sequence. Working commits are not human acceptance."""
    def __init__(self, project: TranslationProject, name: str):
        project._check_open()
        self.project, self.name = project, name
        self.path = project.path / "jobs" / name
        if not (self.path / "job.json").is_file() or not (self.path / "sequence.sqlite").is_file():
            raise FileNotFoundError("translation job not found: " + name)
        self.repo = SequenceRepository(self.path / "sequence.sqlite")
        self._closed = False
        try:
            metadata = read_json(self.path / "job.json")
            if metadata.get("schema") != "novelfetch-job-v1" or metadata["run_id"] != self.repo.run_id:
                raise TranslationError("job_conflict", "job metadata differs from the frozen sequence")
            self.engine = SequenceEngine(self.repo, project.provider, project.counter)
        except BaseException:
            self.repo.close()
            raise
        project._jobs.append(self)

    def _check(self) -> None:
        self.project._check_open()
        if self._closed:
            raise TranslationError("closed", "translation job is closed")

    def status(self) -> dict[str, Any]:
        self._check()
        chapters = self.repo.chapters()
        return {"schema": "novelfetch-job-status-v1", "job_name": self.name, "run_id": self.repo.run_id,
                "complete": all(c["state"] == "committed" for c in chapters), "chapters": chapters,
                "committed_count": len(self.repo.commits()), "target_count": len(chapters),
                "heads": self.repo.heads(), "attempt_count": len(self.repo.store.attempts()),
                "reference_mode": self.repo.manifest["inputs"]["reference_mode"],
                "formal_acceptance": False, "artifact_dir": str(self.path)}

    def cost_usage(self) -> dict[str, Any]:
        """Return durable request/token totals without exposing request content."""
        self._check()
        attempts = self.repo.store.attempts()
        usage = {}
        for attempt in attempts:
            for key, value in attempt.get('usage', {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    usage[key] = usage.get(key, 0) + value
        return {'local_only': True, 'requests': len(attempts),
            'reserved_input_tokens': sum(row['input_reserved'] for row in attempts),
            'reserved_output_tokens': sum(row['output_reserved'] for row in attempts),
            'reported_usage': usage,
            'unlimited_cumulative_caps': {
                'requests': self.engine.spec.max_requests == 0,
                'input_tokens': self.engine.spec.max_input_tokens == 0,
                'output_tokens': self.engine.spec.max_output_tokens == 0}}

    def entity_catalog(self) -> dict[str, Any]:
        """Read the visible compact semantic entity catalog."""
        from translation.sequence.entities import read_entity_catalog
        self._check()
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        visible = {doc['chapter_id'] for doc in view.visible_sources()}
        return read_entity_catalog(self.repo, visible_source_ids=visible,
                                   before_order=view.target_order)

    def entity_evidence(self, entity_id: str) -> dict[str, Any]:
        """Read visible source snippets for a merged entity by stable ID."""
        from translation.sequence.entities import read_entity_evidence
        self._check()
        if not isinstance(entity_id, str) or not entity_id:
            raise ValueError('entity_id is required')
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        visible = {doc['chapter_id'] for doc in view.visible_sources()}
        return read_entity_evidence(self.repo, entity_id, visible_source_ids=visible,
                                    before_order=view.target_order)

    def _checkpoint_workset_catalog_id(self):
        checkpoint_id = self.repo.heads().get('checkpoint')
        if not checkpoint_id:
            return None
        checkpoint = self.repo.store.get(checkpoint_id)
        return (checkpoint.get('workset') or {}).get('workset_catalog_id')

    def workset_catalog(self) -> dict[str, Any]:
        """Read the compact knowledge selection summary for the current target."""
        from translation.sequence.workset import read_workset_catalog
        self._check()
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        return read_workset_catalog(self.repo, before_order=view.target_order,
                                    catalog_id=self._checkpoint_workset_catalog_id())

    def knowledge_evidence(self, short_id: str) -> dict[str, Any]:
        """Read selected or omitted knowledge by its compact workset ID."""
        from translation.sequence.workset import read_workset_evidence
        self._check()
        if not isinstance(short_id, str) or not short_id:
            raise ValueError('short_id is required')
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        return read_workset_evidence(self.repo, short_id, before_order=view.target_order,
                                     catalog_id=self._checkpoint_workset_catalog_id())

    def reconcile_threads(self) -> dict[str, Any]:
        """Reconcile unresolved threads against sealed L2 nodes; may call the model."""
        self._check()
        self.project._require_generation()
        return self.engine.reconcile_threads()

    def translate_window(self, *, size: int | None = None, concurrency: int | None = None,
                         review_concurrency: int | None = None, materialize: bool = True,
                         reuse_staged: bool = False) -> dict[str, Any]:
        """Pre-extract window terms, translate concurrently, then review/commit in order."""
        self._check()
        self.project._require_generation()
        from translation.sequence.batch import run_window
        return run_window(self, size=size, concurrency=concurrency,
                          review_concurrency=review_concurrency,
                          materialize=materialize, reuse_staged=reuse_staged)

    def translate_pipeline(self, *, windows: int = 3, size: int = 30,
                           concurrency: int | None = None,
                           review_concurrency: int | None = None,
                           lookahead: int | None = None,
                           materialize: bool = True) -> dict[str, Any]:
        """Translate windows speculatively while earlier windows are reviewed."""
        self._check()
        self.project._require_generation()
        from translation.sequence.pipeline import run_pipeline
        return run_pipeline(self, windows=windows, size=size,
                            translation_concurrency=concurrency,
                            review_concurrency=review_concurrency,
                            lookahead=lookahead, materialize=materialize)

    def thread_catalog(self) -> dict[str, Any]:
        """Read the compact unresolved-thread catalog for the current target."""
        from translation.sequence.threads import read_thread_catalog
        self._check()
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        return read_thread_catalog(self.repo, before_order=view.target_order)

    def thread_evidence(self, thread_id: str) -> dict[str, Any]:
        """Read one thread's status, resolution and evidence."""
        from translation.sequence.threads import read_thread_evidence
        self._check()
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError('thread_id is required')
        next_chapter = self.engine.next_chapter()
        view = self.engine.view(next_chapter['source_id']) if next_chapter else self.engine.view(None)
        return read_thread_evidence(self.repo, thread_id, before_order=view.target_order)

    def reference_report(self) -> dict[str, Any]:
        """Report frozen references and actual selections, without reading engine code."""
        self._check()
        mode = self.repo.manifest['inputs']['reference_mode']
        packets = self.repo.manifest['inputs'].get('edition_reference_packets', {})
        rows = []
        for chapter in self.repo.chapters():
            packet = packets.get(chapter['source_id'], {})
            context_id = chapter.get('context_id')
            if not context_id and chapter.get('commit_id'):
                context_id = self.repo.store.get(chapter['commit_id'])['context_id']
            context = self.repo.store.get(context_id) if context_id else {}
            selection = context.get('edition_reference_selection')
            planned = [d['entry_id'] for d in packet.get('style_documents', [])]
            actual = selection['style_document_ids'] if selection else None
            resolutions = self.repo.manifest['inputs'].get('edition_terminology_resolutions', {})
            resolution = resolutions.get(chapter['source_id'], {})
            rows.append({'source_id':chapter['source_id'], 'state':chapter['state'],
                'planned_style_document_ids':planned, 'actual_style_document_ids':actual,
                'style_paragraphs':sum(len(d['rows']) for d in packet.get('style_documents', [])
                    if actual is None or d['entry_id'] in actual),
                'terminology_candidates':len(packet.get('terminology', [])),
                'actual_terminology_candidates':len(selection['terminology_indices']) if selection else None,
                'terminology_lookup_status':resolution.get('status', 'disabled'),
                'terminology_search_scope':resolution.get('search_scope', []),
                'terminology_trace':resolution.get('trace', []),
                'omissions':packet.get('omissions', []) + (selection.get('omissions', []) if selection else []),
                'warnings':(['no_chinese_style_prose'] if not (planned if actual is None else actual)
                            and mode == 'cross_edition' else [])})
        return {'reference_mode':mode,
                'local_only':True, 'chapters':rows}

    def draft(self) -> dict[str, Any] | None:
        """Inspect the current uncommitted version and exact paragraph aliases locally."""
        from translation.review.versions import read_version
        self._check()
        chapter = self.engine.next_chapter()
        if not chapter or not chapter.get('version_id'):
            return None
        version = read_version(self.engine.view(chapter['source_id']), chapter['version_id'])
        return {'source_id':chapter['source_id'], 'version_id':chapter['version_id'],
                'round':version['round'], 'revision_limit':self.repo.revision_limit(chapter['source_id'], self.engine.spec.max_revisions),
                'paragraphs':[{k:s[k] for k in ('alias', 'text')} for s in version['segments']],
                'review':deepcopy(chapter.get('review')), 'last_error':deepcopy(chapter.get('last_error'))}

    @operation
    def plan_next(self) -> dict[str, Any]:
        """Inspect exact messages and budget; local checkpoint metadata may be saved."""
        self._check()
        return self.engine.plan_next()

    @operation
    def prepare_history(self) -> dict[str, Any]:
        """Build far L1 and near L2 before translation; run() also does this automatically."""
        self._check()
        self.project._require_generation()
        return self.engine.prepare_history()

    @operation
    def ensure_details(self, node_ids: list[str]) -> dict[str, Any]:
        """Materialize only explicitly requested visible L2 nodes; preserve old contexts."""
        self._check()
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError('unique detail node IDs are required')
        self.project._require_generation()
        return self.engine.prepare_history(child_ids=node_ids)

    def run(self, *, max_chapters: int | None = None, materialize: bool = False,
            on_event: Callable[[dict[str, Any]], None] | None = None) -> RunResult:
        """Translate/review/commit in order. A blocked step preserves all paid work."""
        self._check()
        if max_chapters is not None and (type(max_chapters) is not int or max_chapters <= 0):
            raise ValueError("max_chapters must be a positive integer")
        if not self.status()["complete"]:
            self.project._require_generation()
        completed, error = [], None
        state = "complete" if self.status()["complete"] else "paused_at_limit"
        while state != "complete" and (max_chapters is None or len(completed) < max_chapters):
            if on_event:
                on_event({"event": "chapter_started", "job_name": self.name,
                          "source_id": self.engine.next_chapter()["source_id"]})
            try:
                result = self.engine.advance_sequence(stop_after=1)
            except (NarrativeError, ProviderError) as exc:
                result = {"state": "blocked", "error": exc.kind, "message": str(exc)}
            completed.extend(result.get("completed", []))
            state = result["state"]
            if state == "blocked":
                error = {"code": result.get("error"), "message": result.get("message"),
                         "source_id": result.get("source_id")}
            if on_event:
                on_event({"event": "chapter_blocked" if state == "blocked" else "chapter_committed",
                          "job_name": self.name, "result": deepcopy(result)})
            if state == "blocked":
                break
        if materialize and self.repo.commits():
            self.project._require_generation()
            try:
                nodes = self.engine.materialize_pending(seal_tail=True)
                if any(n["state"] not in {"extracted", "partial"} for n in nodes):
                    state, error = "blocked", {"code": "incomplete_incremental_index", "phase": "materialization"}
            except (NarrativeError, ProviderError) as exc:
                state, error = "blocked", {"code": exc.kind, "message": str(exc), "phase": "materialization"}
        exported = self.export()
        result = RunResult(state, self.name, self.repo.run_id, len(self.repo.commits()), tuple(completed),
                           exported["artifact_dir"], error)
        write_json(self.path / "last-result.json", result.to_dict())
        return result

    def resume(self, **options) -> RunResult:
        return self.run(**options)

    @operation
    def extend_revision_budget(self, *, additional_rounds: int = 1, reason: str) -> dict[str, Any]:
        """Append an explicit recovery allowance; preserve spec, responses and total ledger."""
        self._check()
        chapter = self.engine.next_chapter()
        if not chapter or chapter['state'] != 'review_blocked' or not chapter.get('version_id'):
            raise ValueError('revision extension requires a review-blocked draft')
        if type(additional_rounds) is not int or additional_rounds <= 0 or not isinstance(reason,str) or not reason.strip():
            raise ValueError('positive additional_rounds and a reason are required')
        old = self.repo.revision_limit(chapter['source_id'], self.engine.spec.max_revisions)
        if old + additional_rounds > 8:
            raise ValueError('at most eight explicitly extended revision rounds')
        chain = chapter.get('revision_extension_ids', [])
        record = {'run_id':self.repo.run_id, 'source_id':chapter['source_id'],
                  'version_id':chapter['version_id'], 'from_limit':old, 'to_limit':old+additional_rounds,
                  'reason':reason, 'previous':chain[-1] if chain else None}
        ident = self.repo.store.put('sequence-revision-extension', record)
        self.repo.stage(chapter['source_id'], chapter['state'], revision_extension_ids=chain+[ident])
        return {'extension_id':ident, **record, 'model_called':False}

    @operation
    def submit_revision(self, replacements: dict[str, str], *, expected_version_id: str, reason: str) -> str:
        """Apply agent-supplied paragraph edits as a new version requiring full review."""
        from translation.review.versions import create_version, read_version, segment_translation
        self._check()
        chapter = self.engine.next_chapter()
        if (not chapter or chapter['state'] != 'review_blocked' or chapter.get('prepared_id') or
                chapter.get('version_id') != expected_version_id or not chapter.get('review')):
            raise ValueError('revision must target the current blocked version')
        if not isinstance(replacements,dict) or not replacements or not isinstance(reason,str) or not reason.strip():
            raise ValueError('replacements and reason are required')
        view = self.engine.view(chapter['source_id'])
        version = read_version(view, expected_version_id)
        segments = {s['alias']:s for s in version['segments']}
        if not set(replacements) <= set(segments):
            raise ValueError('replacement aliases must belong to the current draft')
        text = version['text']
        for alias in sorted(replacements, key=lambda a:segments[a]['start'], reverse=True):
            replacement = replacements[alias]
            if (not isinstance(replacement, str) or '\n' in replacement or '\r' in replacement or
                    '@@' in replacement or '```' in replacement or
                    len(segment_translation(replacement, version['source_revision'])) != 1):
                raise ValueError('each replacement must be one nonempty plain-text paragraph')
            segment = segments[alias]
            text = text[:segment['start']]+replacement+text[segment['end']:]
        if text == version['text']:
            raise ValueError('revision made no change')
        if version['round'] >= self.repo.revision_limit(chapter['source_id'], self.engine.spec.max_revisions):
            raise ValueError('extend_revision_budget before submitting another revision')
        rid = self.repo.store.put('agent-revision', {'version_id':expected_version_id,
            'replacements':replacements, 'reason':reason, 'result_hash':digest(text),
            'review':deepcopy(chapter['review']), 'requires_review':True})
        context = self.repo.store.get(version['context_id'], 'review-context')
        vid = create_version(view, context, text, parent=expected_version_id, revision_id=rid)
        self.repo.stage(chapter['source_id'], 'generated', version_id=vid, review=None, last_error=None)
        return vid

    @operation
    def checkpoint(self) -> str | None:
        """Explicitly compress committed history; this operation may call the model."""
        self._check()
        if self.engine.next_chapter() is None:
            return None
        self.project._require_generation()
        return self.engine.checkpoint(force=True)

    def translations(self) -> list[dict[str, Any]]:
        """Read committed results only, including their quality and evidence labels."""
        self._check()
        rows = []
        for ident, commit in self.repo.commits():
            self.repo.validate_commit(ident)
            source = next(d for d in self.repo.manifest["inputs"]["targets"] if d["chapter_id"] == commit["source_id"])
            rows.append({"source_id": commit["source_id"], "title": source["title"], "commit_id": ident,
                         "text": commit["text"], "quality_labels": commit["quality_labels"],
                         "unresolved": commit["unresolved"]})
        return rows

    @operation
    def explain(self, chapter: str | ChapterInfo, output: str | Path | None = None) -> dict[str, Any]:
        """Export full input/output/review artifacts, with no model calls."""
        self._check()
        ident = chapter.id if isinstance(chapter, ChapterInfo) else chapter
        if ident not in {c["source_id"] for c in self.repo.chapters()}:
            raise ValueError("chapter is not a target of this job")
        destination = Path(output).resolve() if output is not None else self.path / "io" / digest(ident)[:20]
        destination.mkdir(parents=True, exist_ok=True)
        return self.engine.explain_context(ident, destination)

    def export(self, output: str | Path | None = None, *, include_io: bool = False) -> dict[str, Any]:
        """Export UTF-8 text + JSONL and optional per-chapter exact prompts/responses."""
        self._check()
        destination = Path(output).resolve() if output is not None else self.path / "export"
        rows = self.translations()
        write_jsonl(destination / "chapters.jsonl", rows)
        atomic_text(destination / "translation.txt", "\n\n".join(r["title"] + "\n\n" + r["text"] for r in rows))
        write_json(destination / "status.json", self.status())
        if include_io:
            for c in self.repo.chapters():
                if c.get("context_id") or c["state"] == "committed":
                    self.explain(c["source_id"], destination / "io" / digest(c["source_id"])[:20])
        return {"artifact_dir": str(destination), "committed_count": len(rows), "include_io": include_io}

    def close(self) -> None:
        if not self._closed:
            self.repo.close()
            self._closed = True
            if self in self.project._jobs:
                self.project._jobs.remove(self)

    def __enter__(self) -> TranslationJob:
        self._check()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
