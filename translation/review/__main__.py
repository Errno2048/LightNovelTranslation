"""Explicit P5 CLI: python -m translation.review --help."""
import argparse
import json
from pathlib import Path

from ..config import load_settings
from ..knowledge import CorpusArchive
from ..narrative import NarrativeStore, NarrativeView, NarrativePolicy, TokenCounter
from ..providers import HTTPProvider, MockProvider
from ..storage import read_json
from .versions import create_version, read_version
from .requests import ReviewSession, DEFAULT_LIMITS
from .coverage import map_coverage
from .checks import run_checks
from .service import review_translation, model_adjudicate, adjudicate_findings, review_with_lookup
from .revision import propose_revision, apply_revision
from .lookup import resolve_review_queries
from .context import compile_review_context
from .acceptance import plan_acceptance, accept_translation, current_head, invalidate_acceptance, pending_outbox


def parser():
    p = argparse.ArgumentParser(description="P5 versioned review/revision and explicit local acceptance")
    p.add_argument("--root", type=Path, default=Path.cwd())
    p.add_argument("--corpus", type=Path, required=True)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--tokenizer", type=Path)
    p.add_argument("--config", type=Path)
    p.add_argument("--limits", type=Path)
    p.add_argument("--chapter-reviews", type=Path)
    p.add_argument("--mock-script", type=Path, help="JSON array of deterministic response strings; no HTTP")
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("prepare"); a.add_argument("--context", type=Path, required=True); a.add_argument("--draft", type=Path, required=True)
    for cmd in ["context", "map", "check", "run", "adjudicate", "lookup", "revise", "plan-acceptance", "accept", "status", "invalidate"]:
        a = sub.add_parser(cmd)
        a.add_argument("--version", required=True)
        if cmd == 'context':
            a.add_argument('--profile',choices=['near','hierarchical'],default='hierarchical')
            a.add_argument('--near-source',nargs='*',default=None)
            a.add_argument('--prior-context',nargs='*',default=[])
        if cmd in {"check", "plan-acceptance"}: a.add_argument("--coverage", required=True)
        if cmd in {'check', 'map'}: a.add_argument('--profile',choices=['legacy','audit-v2'],default='legacy')
        if cmd in {"adjudicate", "revise", "plan-acceptance"}: a.add_argument("--report", required=True)
        if cmd in {"revise", "plan-acceptance"}: a.add_argument("--adjudication", required=cmd == "revise")
        if cmd == "run":
            a.add_argument("--lookup-id", nargs="*", default=[])
            a.add_argument("--prior-report")
            a.add_argument("--source-only", action="store_true")
            a.add_argument('--context-profile',choices=['near','hierarchical','legacy'],default='hierarchical')
            a.add_argument('--evidence-context',help='Frozen review-evidence-context ID')
            a.add_argument('--reference-mode',choices=['literal','paragraph'],default='literal',
                           help='Experimental paragraph mode binds S/T labels to full paragraphs; external evidence still requires literal quotes')
            a.add_argument('--review-profile',choices=['legacy','audit-v2','audit-v3','audit-v4','audit-v5','audit-v6'],default='legacy',
                           help='audit-v6 adds count-consistent examples and auditable record-count metadata recovery; v5 remains available')
            a.add_argument("--resolve-lookups", action="store_true", help="One bounded model-requested evidence round")
        if cmd == "adjudicate": a.add_argument("--decisions", type=Path)
        if cmd == "lookup": a.add_argument("--query", nargs="+", required=True)
        if cmd == "plan-acceptance":
            a.add_argument("--checks", required=True)
            a.add_argument("--signal-decisions", type=Path)
            a.add_argument("--unresolved-decisions", type=Path)
        if cmd in {"plan-acceptance", "accept", "invalidate"}: a.add_argument("--expected-head")
        if cmd in {"plan-acceptance", "invalidate", "adjudicate"}:
            a.add_argument("--actor", default="local-reviewer"); a.add_argument("--reason")
        if cmd == "accept":
            a.add_argument("--plan", required=True); a.add_argument("--idempotency-key", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    store = NarrativeStore(args.db)
    provider = None
    try:
        archive = CorpusArchive(args.corpus)
        context = read_json(args.context) if args.command == "prepare" else store.get(store.get(args.version, "translation-version")["context_id"], "review-context")
        view = NarrativeView(archive, store, NarrativePolicy(**context["policy"]), counter=TokenCounter(args.tokenizer),
                             chapter_reviews=read_json(args.chapter_reviews) if args.chapter_reviews else ())
        limits = read_json(args.limits) if args.limits else DEFAULT_LIMITS
        needs_model = args.command in {"map", "run", "revise"} or (args.command == "adjudicate" and not args.decisions)
        if needs_model:
            if args.mock_script:
                provider, identity = MockProvider(read_json(args.mock_script)), {"model": "mock"}
            else:
                config = load_settings(args.root, args.config).generation
                if config.retries: raise ValueError("P5 requires generation.retries=0 in the configuration file")
                provider, identity = HTTPProvider(config, max_requests=limits["requests"]), config.public()
            session = ReviewSession(view, provider, provider_identity=identity, limits=limits)
        if args.command == "prepare": result = {"version_id": create_version(view, context, args.draft.read_text(encoding="utf-8"))}
        elif args.command == 'context': result = {'evidence_context_id':compile_review_context(view,args.version,
            profile=args.profile,near_source_ids=args.near_source,prior_context_ids=args.prior_context)}
        elif args.command == "map": result = {"coverage_id": map_coverage(session, args.version, profile=args.profile)}
        elif args.command == "check": result = {"checks_id": run_checks(view, args.version, args.coverage,profile=args.profile)}
        elif args.command == "run":
            if args.resolve_lookups:
                if args.lookup_id or args.prior_report: raise ValueError("automatic one-round lookup cannot be combined with supplied followup inputs")
                result = review_with_lookup(session, args.version, include_context=not args.source_only,
                    context_profile=args.context_profile, context_id=args.evidence_context, reference_mode=args.reference_mode,review_profile=args.review_profile)
            else: result = {"report_id": review_translation(session, args.version, lookup_ids=args.lookup_id, prior_report_id=args.prior_report, include_context=not args.source_only,
                context_profile=args.context_profile,context_id=args.evidence_context,reference_mode=args.reference_mode,review_profile=args.review_profile)}
        elif args.command == "lookup": result = {"results": resolve_review_queries(view, args.version, args.query)}
        elif args.command == "adjudicate":
            aid = adjudicate_findings(view, args.version, args.report, read_json(args.decisions), actor=args.actor, method=args.reason or "explicit_source_review") if args.decisions else model_adjudicate(session, args.version, args.report)
            result = {"adjudication_id": aid}
        elif args.command == "revise":
            proposal = propose_revision(session, args.version, args.report, args.adjudication)
            result = {"proposal_id": proposal, "version_id": apply_revision(view, args.version, proposal)}
        elif args.command == "plan-acceptance":
            result = {"plan_id": plan_acceptance(view, args.version, args.coverage, args.checks, args.report,
                adjudication_id=args.adjudication, expected_head=args.expected_head, actor=args.actor, reason=args.reason,
                signal_decisions=read_json(args.signal_decisions) if args.signal_decisions else (),
                unresolved_decisions=read_json(args.unresolved_decisions) if args.unresolved_decisions else ())}
        elif args.command == "accept":
            if store.get(args.plan, "translation-acceptance-plan")["version_id"] != args.version: raise ValueError("plan belongs to another version")
            result = {"acceptance_id": accept_translation(view, args.plan, expected_head=args.expected_head, idempotency_key=args.idempotency_key)}
        elif args.command == "invalidate":
            result = {"invalidation_id": invalidate_acceptance(store, view.policy.target_id, expected_head=args.expected_head, actor=args.actor, reason=args.reason)}
        else:
            v = read_version(view, args.version)
            head = current_head(store, v["source_id"])
            result = {"version_id": args.version, "round": v["round"], "head": head,
                "this_version_accepted": bool(head and store.get(head, "translation-acceptance")["version_id"] == args.version),
                "attempts": store.attempts(), "outbox": pending_outbox(store)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    finally:
        if isinstance(provider, HTTPProvider): provider.client.close()
        store.close()


if __name__ == "__main__":
    main()
