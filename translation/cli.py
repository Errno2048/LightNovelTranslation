"""Run with python -m translation. Use --help for explicit stage commands."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .alignment import apply_review, create_example_catalog, rules, semantic_probe, semantic_window
from .alignment.paragraphs import paragraph_candidates
from .config import load_settings
from .corpus import ingest, load_chapters
from .corpus.reference_audit import audit_references
from .corpus.sections import apply_section_patch
from .corpus.rebuilt import rebind_section_patch, verify_rebuilt_import
from .corpus.quality import apply_integrity_holds, scan_quality
from .evaluation import create_splits, prepare, reports
from .probe import probe
from .providers import HTTPProvider, MockProvider, ProviderError
from .storage import read_json, read_jsonl, write_json, write_jsonl
from .workflow import run
from .knowledge import CorpusArchive, KnowledgeBase, KnowledgeError, QueryPolicy
from .knowledge.bootstrap import bootstrap_p4
from .knowledge.evidence import input_manifest
from .knowledge.extraction import plan_enrichment, run_enrichment
from .context import compile_context
from .ingestion import (apply_structure_patch, export_corpus as export_import_corpus,
                         extract_sources, inspect_input, propose_structure,
                         normalize_input, read_source_range, validate_import)


def parser():
    p = argparse.ArgumentParser(description="Novel alignment and translation pipeline")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--book", default="tenken")
    p.add_argument("--config", type=Path)
    sub = p.add_subparsers(dest="command", required=True)
    import_cmd = sub.add_parser("import", help="Normalize generic EPUB/TXT inputs")
    import_sub = import_cmd.add_subparsers(dest="import_command", required=True)
    import_inspect = import_sub.add_parser("inspect")
    import_inspect.add_argument("--manifest", type=Path, required=True)
    import_inspect.add_argument("--output", type=Path)
    import_extract = import_sub.add_parser("extract")
    import_extract.add_argument("--manifest", type=Path, required=True)
    import_extract.add_argument("--output", type=Path, required=True)
    import_plan = import_sub.add_parser("plan")
    import_plan.add_argument("--run", type=Path, required=True)
    import_validate = import_sub.add_parser("validate")
    import_validate.add_argument("--run", type=Path, required=True)
    import_validate.add_argument("--revision")
    import_export = import_sub.add_parser("export")
    import_export.add_argument("--run", type=Path, required=True)
    import_export.add_argument("--output", type=Path, required=True)
    import_export.add_argument("--revision")
    import_read = import_sub.add_parser("read")
    import_read.add_argument("--run", type=Path, required=True)
    import_read.add_argument("--block-id", nargs="+", required=True)
    import_read.add_argument("--context", type=int, default=2)
    import_apply = import_sub.add_parser("apply")
    import_apply.add_argument("--run", type=Path, required=True)
    import_apply.add_argument("--patch", type=Path, required=True)
    import_run = import_sub.add_parser("run")
    import_run.add_argument("--manifest", type=Path, required=True)
    import_run.add_argument("--output", type=Path, required=True)
    for name in ["ingest", "align", "probe", "splits", "scan-quality"]:
        sub.add_parser(name)
    audit = sub.add_parser("reference-audit", help="Check cached reference order; optionally create an isolated repair branch")
    audit.add_argument("--repair-book")
    sections = sub.add_parser("reference-sections", help="Apply reviewed, revision-bound structural paragraph classifications")
    sections.add_argument("--patch", type=Path, required=True)
    sections.add_argument("--repair-book", required=True)
    verification = sub.add_parser('verify-rebuild', help='Verify new cache import against audited order; optionally rebind reviewed structural sections')
    verification.add_argument('--previous-book', required=True)
    verification.add_argument('--rebuild-folder', type=Path, required=True)
    verification.add_argument('--section-patch', type=Path)
    verification.add_argument('--repair-book')
    review = sub.add_parser("review")
    review.add_argument("--patch", type=Path, required=True)
    examples = sub.add_parser("examples")
    examples.add_argument("--review-file", type=Path, required=True)
    semantic = sub.add_parser("semantic-probe")
    semantic.add_argument("--keys", nargs="+", required=True)
    window = sub.add_parser("align-window")
    window.add_argument("--source-keys",nargs="+",required=True)
    window.add_argument("--target-keys",nargs="+",required=True)
    paragraphs = sub.add_parser('paragraph-candidates', help='P4a development pilot; requires reviewed chapter correspondence')
    paragraphs.add_argument('--keys', nargs='+', required=True)
    paragraphs.add_argument('--max-requests', type=int, default=20)
    preparation = sub.add_parser("prepare")
    preparation.add_argument("--keys", nargs="+", required=True)
    preparation.add_argument("--experiment", default="smoke")
    preparation.add_argument("--mode", choices=["single_gap","block_gap","sequential"], default="single_gap")
    preparation.add_argument("--cutoff-order", type=int)
    preparation.add_argument("--knowledge-db", type=Path)
    preparation.add_argument("--snapshot")
    preparation.add_argument("--example-catalog", type=Path)
    preparation.add_argument("--context-budget", type=int, default=60000)
    execution = sub.add_parser("run")
    execution.add_argument("--experiment", default="smoke")
    execution.add_argument("--run", default="smoke-glm")
    execution.add_argument("--mock", action="store_true")
    execution.add_argument("--dry-run", action="store_true")
    execution.add_argument("--retry-failed", action="store_true")
    execution.add_argument("--variants", choices=["A","B","C"], nargs="+", default=["A","B"])
    execution.add_argument("--max-tasks", type=int, default=24)
    execution.add_argument("--max-requests", type=int, default=30)
    execution.add_argument("--max-output-tokens", type=int, default=8192)
    execution.add_argument("--max-input-estimate", type=int, default=60000)
    execution.add_argument("--max-total-tokens", type=int, default=500000)
    report = sub.add_parser("report")
    report.add_argument("--experiment", default="smoke")
    report.add_argument("--run", default="smoke-glm")
    index = sub.add_parser("knowledge-index")
    index.add_argument("--db", type=Path)
    index.add_argument("--languages", nargs="+", default=["ja"])
    index.add_argument("--force", action="store_true")
    search = sub.add_parser("knowledge-search")
    search.add_argument("--db", type=Path)
    search.add_argument("--query", required=True)
    search.add_argument("--limit", type=int, default=10)
    search.add_argument("--mode", choices=["single_gap","block_gap","sequential"], default="single_gap")
    search.add_argument("--hidden-source-id", nargs="*", default=[])
    search.add_argument("--reserved-source-id", nargs="*", default=[])
    search.add_argument("--cutoff-order", type=int)
    freeze = sub.add_parser("knowledge-freeze")
    freeze.add_argument("--db", type=Path)
    freeze.add_argument("--branch", default="main")
    freeze.add_argument("--export", type=Path)
    plan = sub.add_parser("knowledge-plan")
    plan.add_argument("--db", type=Path, required=True)
    plan.add_argument("--needs", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--target-order", type=int)
    plan.add_argument("--cutoff-order", type=int)
    plan.add_argument("--mode", choices=["single_gap","block_gap","sequential"], default="single_gap")
    plan.add_argument("--hidden-source-id", nargs="*", default=[])
    plan.add_argument("--reserved-source-id", nargs="*", default=[])
    plan.add_argument("--donor-source-id", nargs="*")
    plan.add_argument("--max-requests", type=int, default=8)
    plan.add_argument("--max-output-tokens", type=int, default=1200)
    extract = sub.add_parser("knowledge-extract")
    extract.add_argument("--plan", type=Path, required=True)
    extract.add_argument("--cache", type=Path, required=True)
    extract.add_argument("--mock", action="store_true")
    extract.add_argument("--max-requests", type=int, default=8)
    extract.add_argument("--output", type=Path)
    extract.add_argument("--retry-failed", action="store_true")
    extract.add_argument("--repair-cached", action="store_true", help="Locally unwrap an exact JSON fence in a cached failed response")
    review_kb = sub.add_parser("knowledge-review")
    review_kb.add_argument("--db", type=Path, required=True)
    review_kb.add_argument("--batch", type=Path, required=True)
    review_kb.add_argument("--decisions", type=Path, required=True)
    review_kb.add_argument("--expected-head", required=True)
    review_kb.add_argument("--idempotency-key", required=True)
    review_kb.add_argument("--branch", default="main")
    explain = sub.add_parser("knowledge-explain")
    explain.add_argument("--db", type=Path, required=True)
    explain.add_argument("--snapshot")
    explain.add_argument("--item-id", required=True)
    context = sub.add_parser("context-build")
    context.add_argument("--db", type=Path, required=True)
    context.add_argument("--task", type=Path, required=True)
    context.add_argument("--output", type=Path, required=True)
    context.add_argument("--target-order", type=int)
    context.add_argument("--cutoff-order", type=int)
    context.add_argument("--mode", choices=["single_gap","block_gap","sequential"], default="single_gap")
    context.add_argument("--hidden-source-id", nargs="*", default=[])
    context.add_argument("--snapshot")
    context.add_argument("--token-budget", type=int, default=60000)
    bootstrap = sub.add_parser("knowledge-bootstrap", help="Build a review-gated P4 evidence catalog")
    bootstrap.add_argument("--output", type=Path, required=True)
    bootstrap.add_argument("--splits", type=Path)
    bootstrap.add_argument("--target-key", nargs="+", required=True)
    bootstrap.add_argument("--donor-map", type=Path, required=True,
                           help="JSON object mapping each target key to an explicit donor-key list")
    bootstrap.add_argument("--review-manifest", type=Path,
                           help="Optional revision-bound p4-donor-review-v1 manifest")
    from .narrative.cli import register
    register(sub)
    return p


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    root = args.root.resolve()
    if not args.book or any(c in args.book for c in "/\\") or args.book in {".",".."}:
        raise ValueError("book must be a single directory name")
    for attr in ["run", "experiment", "repair_book", "previous_book"]:
        value = getattr(args,attr,None)
        if args.command != "import" and value and (any(c in value for c in "/\\") or value in {".",".."}):
            raise ValueError("run/experiment must be single directory names")
    base = root / "artifacts/translation" / args.book
    try:
        if args.command == "import":
            if args.import_command in {"inspect", "extract"}:
                manifest_path = args.manifest.resolve()
                manifest = read_json(manifest_path)
                if args.import_command == "inspect":
                    result = inspect_input(manifest, base=manifest_path.parent)
                    if args.output:
                        write_json(args.output, result)
                else:
                    result = extract_sources(manifest, args.output.resolve(), base=manifest_path.parent)
            elif args.import_command == "plan":
                result = propose_structure(args.run.resolve())
            elif args.import_command == "validate":
                result = validate_import(args.run.resolve(), args.revision)
            elif args.import_command == "export":
                result = export_import_corpus(args.run.resolve(), args.output.resolve(), args.revision)
            elif args.import_command == "read":
                result = read_source_range(args.run.resolve(), args.block_id, context=args.context)
            elif args.import_command == "apply":
                result = apply_structure_patch(args.run.resolve(), read_json(args.patch))
            elif args.import_command == "run":
                manifest_path = args.manifest.resolve()
                result = normalize_input(read_json(manifest_path), args.output.resolve(), base=manifest_path.parent)
            else:  # pragma: no cover - argparse enforces the choices
                raise ValueError("unknown import command")
            if args.import_command in {"plan", "apply"}:
                # Full-book structures can be tens of MB; keep stdout usable
                # and leave all blocks/candidates in the saved revision.
                result = {"schema": result["schema"], "status": result["status"],
                          "fingerprint": result["fingerprint"], "entry_count": len(result["entries"]),
                          "candidate_count": len(result["candidates"]), "issues": result["issues"],
                          "structure_path": str(args.run.resolve() / "structure.json")}
        elif args.command.startswith("narrative-"):
            from .narrative.cli import execute
            result = execute(args, root, base)
        elif args.command == "probe":
            result = probe(load_settings(root,args.config),base/"provider")
        elif args.command == "ingest":
            manifest = ingest(root,base/"corpus")
            result = {k:v for k,v in manifest.items() if k not in {"documents","raw_cache_documents"}}
        elif args.command == "reference-audit":
            branch = root / "artifacts/translation" / args.repair_book if args.repair_book else None
            if branch == base:
                raise ValueError("repair branch must differ from the input book")
            result = audit_references(root, base, branch=branch)
        elif args.command == "reference-sections":
            result = apply_section_patch(root, base, read_json(args.patch), root / "artifacts/translation" / args.repair_book)
        elif args.command == 'verify-rebuild':
            if bool(args.section_patch) != bool(args.repair_book):
                raise ValueError('Section patch and repair book must be supplied together')
            previous = root / 'artifacts/translation' / args.previous_book
            if previous == base:
                raise ValueError('Previous audited corpus must be separate from the fresh import')
            result = verify_rebuilt_import(root, base, previous, args.rebuild_folder)
            split = previous / 'evaluations/splits.json'
            if split.exists():
                frozen = read_json(split)
                current = base / 'evaluations/splits.json'
                if current.exists() and read_json(current) != frozen:
                    raise ValueError('Frozen chapter split differs')
                write_json(current, frozen)
            if args.section_patch:
                patch = rebind_section_patch(previous, base, read_json(args.section_patch))
                write_json(base / 'verification/section_patch.json', patch)
                destination = root / 'artifacts/translation' / args.repair_book
                result['sections'] = apply_section_patch(root, base, patch, destination)
                result['quality_holds'] = apply_integrity_holds(destination)
                rules(load_chapters(destination / 'corpus'), destination / 'alignment')
                result['quality'] = scan_quality(destination)
        elif args.command == 'scan-quality':
            result = scan_quality(base)
        elif args.command == "align":
            result = rules(load_chapters(base/"corpus"),base/"alignment")
        elif args.command == "review":
            apply_review(base/"alignment",read_json(args.patch),load_chapters(base/"corpus"))
            result = {"review_applied":str(args.patch)}
        elif args.command == "examples":
            result = {"examples":len(create_example_catalog(load_chapters(base/"corpus"),base/"corpus/paragraphs.jsonl",read_json(args.review_file),base/"alignment"))}
        elif args.command == "splits":
            result = create_splits(load_chapters(base/"corpus"),base/"evaluations")
        elif args.command == "semantic-probe":
            settings = load_settings(root,args.config)
            provider = HTTPProvider(settings.embedding,max_requests=4)
            result = semantic_probe(load_chapters(base/"corpus"),args.keys,provider,base/"alignment")
        elif args.command == "align-window":
            config = load_settings(root,args.config).embedding
            chapters = load_chapters(base/"corpus")
            index = {(c["language"],c["match_key"]):c for c in chapters if c["kind"]=="narrative"}
            window_result = semantic_window([index["ja",k] for k in args.source_keys],[index["zh",k] for k in args.target_keys],HTTPProvider(config,max_requests=4),base/"alignment")
            result = {"path":window_result["path"],"group_relations":[g["relation"] for g in window_result["groups"]],"usage":window_result["usage"],"report":str(base/"alignment/window_candidates.json")}
        elif args.command == 'paragraph-candidates':
            from uuid import uuid4
            def paragraph_event(event):
                write_json(base / 'alignment/paragraphs/provider_events' / (uuid4().hex + '.json'), event)
            provider = HTTPProvider(load_settings(root, args.config).embedding, max_requests=args.max_requests, recorder=paragraph_event)
            result = paragraph_candidates(base, args.keys, provider)
        elif args.command == "prepare":
            chapters = load_chapters(base/"corpus")
            groups = read_jsonl(base/"alignment/groups.jsonl")
            catalog = read_jsonl(args.example_catalog or (base/"alignment/example_catalog.jsonl"))
            splits = read_json(base/"evaluations/splits.json")
            context_builder = None
            kb = None
            if args.knowledge_db:
                if args.mode == "single_gap" and len(args.keys) != 1:
                    raise ValueError("P4 single-gap knowledge requires one target and independent database per fold")
                archive = CorpusArchive(base / "corpus")
                kb = KnowledgeBase(archive, args.knowledge_db)
                source_lookup = {c["match_key"]: c for c in chapters if c["language"] == "ja"}
                hidden = [source_lookup[k]["chapter_id"] for k in args.keys]
                policy = QueryPolicy(args.mode, hidden, splits["validation"] + splits["blind_test"], args.cutoff_order,
                                     sorted({e["source_chapter_id"] for e in catalog if e.get("status") == "reviewed_prefix"}))
                snapshot = args.snapshot or kb.freeze()["snapshot_id"]
                def context_builder(task):
                    view = kb.open_view(snapshot_id=snapshot, visibility_policy=policy, target_position=task["source"]["order"])
                    return compile_context(task, view, token_budget=args.context_budget)
            result = prepare(chapters, groups, catalog, splits, args.keys, base/"evaluations"/args.experiment,
                             mode=args.mode, cutoff_order=args.cutoff_order, context_builder=context_builder)
            if kb:
                kb.close()
        elif args.command == "knowledge-index":
            archive = CorpusArchive(base / "corpus")
            db = args.db or (base / "knowledge/p4/knowledge.sqlite")
            kb = KnowledgeBase(archive, db)
            result = kb.build_index(languages=tuple(args.languages), force=args.force)
            kb.close()
        elif args.command == "knowledge-search":
            archive = CorpusArchive(base / "corpus")
            db = args.db or (base / "knowledge/p4/knowledge.sqlite")
            splits = read_json(base / "evaluations/splits.json")
            reserved = sorted(set(args.reserved_source_id + splits["validation"] + splits["blind_test"]))
            policy = QueryPolicy(args.mode, args.hidden_source_id, reserved, args.cutoff_order)
            kb = KnowledgeBase(archive, db)
            view = kb.open_view(visibility_policy=policy)
            result = {"query": args.query, "results": view.search(args.query, limit=args.limit), "view_fingerprint": view.fingerprint}
            kb.close()
        elif args.command == "knowledge-freeze":
            archive = CorpusArchive(base / "corpus")
            db = args.db or (base / "knowledge/p4/knowledge.sqlite")
            kb = KnowledgeBase(archive, db)
            result = kb.freeze(args.branch)
            if args.export:
                result = kb.export_snapshot(result["snapshot_id"], args.export)
            kb.close()
        elif args.command == "knowledge-plan":
            archive = CorpusArchive(base / "corpus")
            kb = KnowledgeBase(archive, args.db)
            splits = read_json(base / "evaluations/splits.json")
            policy = QueryPolicy(args.mode, args.hidden_source_id, sorted(set(args.reserved_source_id + splits["validation"] + splits["blind_test"])),
                                 cutoff_order=args.cutoff_order, allowed_translation_source_ids=args.donor_source_id)
            view = kb.open_view(visibility_policy=policy, target_position=args.target_order)
            needs = read_json(args.needs)
            if isinstance(needs, dict):
                needs = needs.get("needs", [])
            result = plan_enrichment(view, needs, max_requests=args.max_requests, max_output_tokens=args.max_output_tokens)
            write_json(args.output, result)
            kb.close()
        elif args.command == "knowledge-extract":
            plan_data = read_json(args.plan)
            archive = CorpusArchive(base / "corpus")
            if archive.fingerprint != plan_data.get("corpus_fingerprint"):
                raise KnowledgeError("stale_evidence", "extraction plan corpus changed")
            policy = QueryPolicy(**plan_data["policy"])
            for task in plan_data["tasks"]:
                for e in task["evidence"]:
                    archive.validate_evidence(e, policy)
            if args.mock:
                provider = MockProvider(['{"items": [], "uncertainties": ["mock: no semantic extraction"]}'] * len(plan_data["tasks"]))
            else:
                from uuid import uuid4
                def extraction_event(event):
                    write_json(args.cache.parent / "provider_events" / (uuid4().hex + ".json"), event)
                provider = HTTPProvider(load_settings(root, args.config).generation, max_requests=args.max_requests, recorder=extraction_event)
            result = run_enrichment(plan_data, provider, args.cache, retry_failed=args.retry_failed, repair_cached=args.repair_cached)
            if args.output:
                write_json(args.output, result)
        elif args.command == "knowledge-review":
            archive = CorpusArchive(base / "corpus")
            kb = KnowledgeBase(archive, args.db)
            policy_file = args.db.parent / "policy.json"
            if policy_file.exists():
                kb.bind_policy(QueryPolicy(**read_json(policy_file)))
            batch = read_json(args.batch)
            if isinstance(batch, dict):
                batch = [item for row in batch.get("results", []) for item in row["candidates"]]
            result = kb.review_candidates(args.branch, batch, read_json(args.decisions), expected_head=args.expected_head,
                                          idempotency_key=args.idempotency_key)
            kb.close()
        elif args.command == "knowledge-explain":
            archive = CorpusArchive(base / "corpus")
            kb = KnowledgeBase(archive, args.db)
            splits = read_json(base / "evaluations/splits.json")
            policy = QueryPolicy("single_gap", reserved_source_ids=splits["validation"] + splits["blind_test"])
            result = kb.open_view(snapshot_id=args.snapshot, visibility_policy=policy).explain(args.item_id)
            kb.close()
        elif args.command == "context-build":
            archive = CorpusArchive(base / "corpus")
            kb = KnowledgeBase(archive, args.db)
            task = read_json(args.task)
            splits = read_json(base / "evaluations/splits.json")
            legacy = task.get("visibility", {"mode": args.mode, "hidden_source_ids": args.hidden_source_id, "cutoff_order": args.cutoff_order})
            policy = QueryPolicy(**legacy)
            policy.reserved_source_ids = sorted(set(policy.reserved_source_ids + splits["validation"] + splits["blind_test"]))
            view = kb.open_view(snapshot_id=args.snapshot, visibility_policy=policy, target_position=args.target_order or task["source"].get("order"))
            bundle = compile_context(task, view, token_budget=args.token_budget)
            write_json(args.output, bundle.public())
            result = {"fingerprint": bundle.fingerprint, "token_accounting": bundle.token_accounting,
                      "unresolved_requirements": bundle.unresolved_requirements}
            kb.close()
        elif args.command == "knowledge-bootstrap":
            splits_path = args.splits or (base / "evaluations/splits.json")
            donor_map = read_json(args.donor_map)
            result = bootstrap_p4(base / "corpus", args.output, splits_path,
                                  target_keys=args.target_key, donor_keys=donor_map,
                                  review_manifest_path=args.review_manifest)
        elif args.command == "run":
            settings = load_settings(root,args.config) if not args.mock else None
            events = []
            destination = base/"runs"/args.run
            def record_event(event):
                from uuid import uuid4
                from .storage import write_json
                events.append(event)
                write_json(destination/"provider_events"/(uuid4().hex+".json"),event)
            provider = MockProvider() if args.mock else HTTPProvider(settings.generation,max_requests=args.max_requests,recorder=record_event)
            result = run(base/"evaluations"/args.experiment/"generation",destination,provider,
                         public_config={"model":"mock"} if args.mock else settings.generation.public(),
                         variants=tuple(dict.fromkeys(args.variants)),dry_run=args.dry_run,max_tasks=args.max_tasks,
                         max_output_tokens=args.max_output_tokens,max_input_estimate=args.max_input_estimate,
                         max_total_tokens=args.max_total_tokens,retry_failed=args.retry_failed)
        else:
            result = reports(base/"evaluations"/args.experiment,base/"runs"/args.run)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if args.command == "import" and (result.get("ok") is False or result.get("status") in {"needs_resolution", "unsupported"}):
            return 1
        if args.command == "run" and not args.dry_run and (result.get("failed",0) or result.get("skipped_failed",0)):
            return 1
        if args.command == "knowledge-extract" and result.get("failed", 0):
            return 1
        if args.command == "narrative-summarize" and result.get("failed", 0):
            return 1
        return 0
    except (ProviderError, KnowledgeError, ValueError, FileNotFoundError) as exc:
        # Never print arbitrary raw provider or credential-file data.
        print(f"{type(exc).__name__}: {exc}",file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
