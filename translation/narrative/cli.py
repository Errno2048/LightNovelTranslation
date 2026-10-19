"""P4.1 CLI kept separate from the existing P0-P4 command handlers."""
from dataclasses import asdict
import json

from ..config import load_settings
from ..context.hierarchical import compile_hierarchical_context, explain_context
from ..knowledge import CorpusArchive
from ..providers import HTTPProvider, MockProvider
from ..storage import read_json, write_json
from . import BlockConfig, ContextSpec, NarrativePolicy, NarrativeStore, NarrativeView, TokenCounter, build_block_index
from .service import review_block_boundaries
from .summaries import mock_summary, plan_summaries, review_summaries, run_summary_plan
from .translation import run_translation
from .schemas import NarrativeError


def register(sub):
    commands = {}
    for name in ("index", "plan", "summarize", "boundaries", "review", "context", "explain", "translate", "export"):
        p = sub.add_parser("narrative-" + name, help="P4.1 " + name)
        commands[name] = p
        if name != "explain":
            p.add_argument("--db", type=__import__('pathlib').Path, required=True)
        if name not in {"explain", "export", "summarize", "translate"}:
            p.add_argument("--policy", type=__import__('pathlib').Path)
        if name not in {"explain", "export"}:
            p.add_argument("--tokenizer", type=__import__('pathlib').Path)
        p.add_argument("--output", type=__import__('pathlib').Path, required=name not in {"translate", "explain"})
    for name in ("plan", "boundaries", "context"):
        commands[name].add_argument("--index-id", required=True)
    for name in ("plan", "summarize", "translate"):
        commands[name].add_argument("--mock", action="store_true")
    for name in ("summarize", "translate"):
        commands[name].add_argument("--retry-failed", action="store_true")
    commands["summarize"].add_argument("--plan", type=__import__('pathlib').Path, required=True)
    for name in ("boundaries", "review"):
        commands[name].add_argument("--decisions", type=__import__('pathlib').Path, required=True)
    commands["index"].add_argument("--overrides", type=__import__('pathlib').Path)
    commands["index"].add_argument("--min-chapters", type=int, default=20)
    commands["index"].add_argument("--max-chapters", type=int, default=40)
    commands["index"].add_argument("--detail-chapters", type=int, default=4)
    commands["index"].add_argument("--block-tokens", type=int, default=150000)
    commands["plan"].add_argument("--max-requests", type=int, default=12)
    commands["plan"].add_argument("--max-output-tokens", type=int, default=16000)
    commands["plan"].add_argument("--max-single-input", type=int, default=100000)
    commands["plan"].add_argument("--bilingual", action="store_true", help="v2 joint bilingual summaries and metadata")
    commands["plan"].add_argument("--chapter-reviews", type=__import__('pathlib').Path)
    commands["summarize"].add_argument("--chapter-reviews", type=__import__('pathlib').Path)
    commands["plan"].add_argument("--reuse-summaries", type=__import__('pathlib').Path,
        help="Explicit JSON block-id to immutable-summary-id mapping; provider may differ")
    for name in ("context", "translate", "explain"):
        if name != "context":
            commands[name].add_argument("--bundle", type=__import__('pathlib').Path, required=True)
    commands["context"].add_argument("--summaries", type=__import__('pathlib').Path, required=True)
    commands["context"].add_argument("--chapter-reviews", type=__import__('pathlib').Path)
    commands["translate"].add_argument("--chapter-reviews", type=__import__('pathlib').Path,
        help="Explicit full-chapter review revisions; defaults to immutable IDs pinned in the bundle")
    commands["context"].add_argument("--knowledge", type=__import__('pathlib').Path)
    commands["context"].add_argument("--examples", type=__import__('pathlib').Path)
    commands["context"].add_argument("--translation-metadata", type=__import__('pathlib').Path)
    commands["context"].add_argument("--metadata-core-form", action="append", default=[])
    commands["context"].add_argument("--n-chapter", type=int, default=3)
    commands["context"].add_argument("--n-block", type=int, default=2)
    commands["context"].add_argument("--context-limit", type=int, default=128000)
    commands["context"].add_argument("--max-output-tokens", type=int, default=8192)
    commands["context"].add_argument("--without-summaries", action="store_true")
    commands["translate"].add_argument("--variant", choices=["C", "H-near", "H-tiered"], default="H-tiered")
    commands["translate"].add_argument("--dry-run", action="store_true")


def execute(args, root, base):
    name = args.command.removeprefix("narrative-")
    if name == "explain":
        result = explain_context(read_json(args.bundle))
        if args.output:
            write_json(args.output, result)
        return result
    store = NarrativeStore(args.db)
    try:
        if name == "export":
            return {"fingerprint": store.export(args.output)}
        archive = CorpusArchive(base / "corpus")
        counter = TokenCounter(args.tokenizer)
        if name == "summarize":
            plan = read_json(args.plan)
            policy = NarrativePolicy(**plan["policy"])
        elif name == "translate":
            bundle = read_json(args.bundle)
            policy = NarrativePolicy(**bundle["policy"])
        else:
            policy = NarrativePolicy(**read_json(args.policy)) if args.policy else NarrativePolicy()
        split_path = base / "evaluations/splits.json"
        if policy.target_id and split_path.exists():
            splits = read_json(split_path)
            reserved = set(splits["validation"] + splits["blind_test"])
            if not reserved <= set(policy.reserved_source_ids):
                raise NarrativeError("visibility_denied", "target policy omits corpus validation/blind-test reservations")
        reviews = []
        if name in {"context", "translate", "plan", "summarize"} and args.chapter_reviews:
            reviews = read_json(args.chapter_reviews)
        elif name == "summarize" and plan.get("version") == "summary-plan-v2":
            rids = sorted({rid for t in plan["tasks"] for p in t["pairs"] for rid in p["review_ids"]})
            reviews = [{"id": rid, **store.get(rid, "chapter-review")} for rid in rids]
        elif name == "translate":
            reviews = [{"id": rid, **store.get(rid, "chapter-review")} for rid in bundle.get("chapter_reviews", [])]
        view = NarrativeView(archive, store, policy, counter=counter, chapter_reviews=reviews)
        identity = {"model": "mock"} if getattr(args, "mock", False) else None
        if name in {"plan", "summarize", "translate"} and not identity:
            config = load_settings(root, args.config).generation
            identity = config.public()
        if name == "index":
            result = build_block_index(view, BlockConfig(args.min_chapters, args.max_chapters, args.block_tokens, args.detail_chapters),
                                       read_json(args.overrides) if args.overrides else [])
        elif name == "plan":
            result = plan_summaries(view, args.index_id, provider_identity=identity, max_requests=args.max_requests,
                                    max_output_tokens=args.max_output_tokens, max_single_input=args.max_single_input,
                                    reuse_summaries=read_json(args.reuse_summaries) if args.reuse_summaries else None,
                                    bilingual_config={} if args.bilingual else None)
        elif name == "boundaries":
            result = review_block_boundaries(view, args.index_id, read_json(args.decisions))
        elif name == "review":
            result = {"review_ids": review_summaries(view, read_json(args.decisions))}
        elif name in {"summarize", "translate"}:
            if args.mock:
                if name == "summarize" and plan.get("version") == "summary-plan-v2":
                    from .bilingual import mock_summary as mock_bilingual
                    script = [json.dumps(mock_bilingual(t["pairs"], t["ranges"]), ensure_ascii=False) for t in plan["tasks"] if not t["cached_summary_id"]]
                else:
                    script = [json.dumps(mock_summary(t["documents"], t["ranges"]), ensure_ascii=False) for t in plan["tasks"] if not t["cached_summary_id"]] if name == "summarize" else None
                provider = MockProvider(script)
            else:
                from uuid import uuid4
                def event(row):
                    write_json(args.db.parent / "provider_events" / (uuid4().hex + ".json"), row)
                provider = HTTPProvider(config, max_requests=24, recorder=event)
            if name == "summarize":
                result = run_summary_plan(view, plan, provider, provider_identity=identity, retry_failed=args.retry_failed)
            else:
                if not args.output:
                    raise ValueError("translation requires --output directory")
                return run_translation(view, bundle, provider, provider_identity=identity, variant=args.variant,
                                       output=args.output, dry_run=args.dry_run, retry_failed=args.retry_failed)
        elif name == "context":
            raw = read_json(args.summaries)
            mapping = {r["block_id"]: r["summary_id"] for r in raw["results"] if "summary_id" in r} if "results" in raw else raw
            result = compile_hierarchical_context(view, args.index_id, mapping,
                spec=ContextSpec(args.n_chapter, args.n_block, args.context_limit, args.max_output_tokens,
                                 include_summaries=not args.without_summaries), knowledge=read_json(args.knowledge) if args.knowledge else None,
                examples=read_json(args.examples) if args.examples else [])
            if args.translation_metadata:
                result = compile_hierarchical_context(view, args.index_id, mapping,
                    spec=ContextSpec(args.n_chapter, args.n_block, args.context_limit, args.max_output_tokens,
                                     include_summaries=not args.without_summaries),
                    knowledge=read_json(args.knowledge) if args.knowledge else None,
                    examples=read_json(args.examples) if args.examples else [],
                    translation_metadata=read_json(args.translation_metadata), metadata_core_forms=args.metadata_core_form)
        else:
            raise ValueError("unknown narrative command")
        write_json(args.output, result)
        return {"output": str(args.output), "index_id": result.get("index_id"), "plan_id": result.get("plan_id"),
                "fingerprint": result.get("fingerprint"), "failed": result.get("failed", 0)}
    finally:
        store.close()
