"""Explicit acceptance: one SQLite transaction, CAS heads, idempotency and outbox."""
import json

from ..narrative.schemas import NarrativeError
from ..storage import digest
from .versions import read_version
from .coverage import read_coverage
from .checks import read_checks
from .service import read_report
from .prompts import AUDIT_PROFILES


def ensure_schema(store):
    # execute(), unlike executescript(), does not implicitly commit an active transaction.
    for sql in ["CREATE TABLE IF NOT EXISTS accepted_heads(source_id TEXT PRIMARY KEY, acceptance_id TEXT)",
                "CREATE TABLE IF NOT EXISTS acceptance_keys(key TEXT PRIMARY KEY, action_digest TEXT NOT NULL, result_id TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS review_outbox(event_id TEXT PRIMARY KEY, acceptance_id TEXT NOT NULL, payload TEXT NOT NULL, published INTEGER NOT NULL DEFAULT 0)"]:
        store.db.execute(sql)


def current_head(store, source_id):
    ensure_schema(store)
    row = store.db.execute("SELECT acceptance_id FROM accepted_heads WHERE source_id=?", (source_id,)).fetchone()
    return row[0] if row else None


def lineage_open_questions(view, version_id):
    """A clean new report cannot erase unresolved evidence questions from repairs."""
    value, questions = read_version(view, version_id), {}
    while value.get("parent"):
        if value['revision_id'].startswith('agent-revision:'):
            revision = view.store.get(value['revision_id'], 'agent-revision')
            if revision['version_id'] != value['parent'] or revision['result_hash'] != value['text_hash']:
                raise NarrativeError('invalid_record', 'agent revision differs from its version lineage')
            proposal = revision['review']
        else:
            revision = view.store.get(value["revision_id"], "translation-revision")
            proposal = view.store.get(revision["proposal_id"], "revision-proposal")
        report = read_report(view, value["parent"], proposal["report_id"])
        adjudication = view.store.get(proposal["adjudication_id"], "review-adjudication")
        decided = {d["finding"]: d["decision"] for d in adjudication["decisions"]}
        open_items = report["unresolved"] + [f for f in report["findings"]
            if decided.get(f["id"]) not in {"confirmed", "dismissed"}]
        for item in open_items:
            ident = "lineage:"+digest({"report_id": proposal["report_id"], "issue_id": item["id"]})[:24]
            questions[ident] = {"id": ident, "version_id": value["parent"],
                "report_id": proposal["report_id"], "issue": item}
        value = read_version(view, value["parent"])
    return [questions[key] for key in sorted(questions)]


def plan_acceptance(view, version_id, coverage_id, checks_id, report_id, *, adjudication_id=None,
                    expected_head=None, actor, reason, signal_decisions=(), unresolved_decisions=(), knowledge_candidates=()):
    v = read_version(view, version_id)
    coverage, checks, report = read_coverage(view, version_id, coverage_id), read_checks(view, version_id, checks_id), read_report(view, version_id, report_id)
    if checks["coverage_id"] != coverage_id:
        raise NarrativeError("conflict", "checks bind a different coverage record")
    blockers = list(checks["hard_failures"])
    if report.get('review_profile') in AUDIT_PROFILES and checks.get('profile') not in {'coarse','record-checks'}:
        blockers.append('enhanced_checks_required')
    if not coverage["mapping_complete"]: blockers.append("mapping_incomplete")
    if not report["review_complete"] or not report["semantic_coverage_reviewed"]: blockers.append("semantic_review_incomplete")
    decisions = {}
    if adjudication_id:
        a = view.store.get(adjudication_id, "review-adjudication")
        if a["version_id"] != version_id or a["report_id"] != report_id:
            raise NarrativeError("conflict", "adjudication from another version")
        decisions = {d["finding"]: d for d in a["decisions"]}
        if not signal_decisions and a.get('checks_id')==checks_id:
            signal_decisions = a.get('signal_decisions',[])
    for f in report["findings"]:
        d = decisions.get(f["id"], {})
        # Any confirmed issue remains open on this version. Repair requires a new review.
        if d.get("decision") != "dismissed" or not d.get("reason"):
            blockers.append("unresolved_finding:"+f["id"])
    inherited = lineage_open_questions(view, version_id)
    for records, required, label in [(signal_decisions, [s["id"] for s in checks["signals"]], "signal"),
                                      (unresolved_decisions, [u["id"] for u in report["unresolved"]+inherited], "unresolved")]:
        resolved = {d.get("id") for d in records if d.get("decision") == "dismissed" and d.get("reason")}
        if len({d.get("id") for d in records}) != len(records) or resolved-set(required):
            raise NarrativeError("invalid_record", "unknown or duplicate "+label+" decision")
        blockers += [label+":"+i for i in required if i not in resolved]
    if not actor or not reason: raise NarrativeError("invalid_record", "acceptance actor and reason required")
    data = {"schema": "translation-acceptance-plan-v1", "policy_version": "explicit-reviewed",
        "version_id": version_id, "source_id": v["source_id"], "coverage_id": coverage_id, "checks_id": checks_id,
        "report_id": report_id, "adjudication_id": adjudication_id, "expected_head": expected_head,
        "actor": actor, "reason": reason, "signal_decisions": list(signal_decisions), "unresolved_decisions": list(unresolved_decisions),
        "knowledge_candidates": list(knowledge_candidates), "blockers": sorted(set(blockers)),
        "state": "eligible_for_acceptance" if not blockers else "review_required"}
    if inherited:
        data["lineage_open_questions"] = inherited
    return view.store.put("translation-acceptance-plan", data)


def _insert(store, kind, data):
    ident = kind+":"+digest(data)
    store.db.execute("INSERT OR IGNORE INTO objects VALUES(?,?,?)", (ident, kind, json.dumps(data, ensure_ascii=False, sort_keys=True)))
    return ident


def accept_translation(view, plan_id, *, expected_head, idempotency_key):
    store = view.store
    ensure_schema(store)
    p = store.get(plan_id, "translation-acceptance-plan")
    if not idempotency_key or p["expected_head"] != expected_head:
        raise NarrativeError("conflict", "explicit head/key must agree with acceptance plan")
    action = digest({"plan": plan_id, "expected_head": expected_head})
    replay = store.db.execute("SELECT action_digest,result_id FROM acceptance_keys WHERE key=?", (idempotency_key,)).fetchone()
    if replay:
        if replay[0] != action: raise NarrativeError("conflict", "idempotency key reused for another action")
        # Idempotent retries acknowledge the historical action, not a new active head.
        return replay[1]
    kwargs = {k: p[k] for k in ("adjudication_id", "expected_head", "actor", "reason", "signal_decisions", "unresolved_decisions", "knowledge_candidates")}
    if plan_acceptance(view, p["version_id"], p["coverage_id"], p["checks_id"], p["report_id"], **kwargs) != plan_id or p["blockers"]:
        raise NarrativeError("review_required", "acceptance plan is stale or has unresolved blockers")
    # Holding the write lock protects CAS and every acceptance record in one database.
    store.db.execute("BEGIN IMMEDIATE")
    try:
        replay = store.db.execute("SELECT action_digest,result_id FROM acceptance_keys WHERE key=?", (idempotency_key,)).fetchone()
        if replay:
            if replay[0] != action: raise NarrativeError("conflict", "idempotency key reused for another action")
            store.db.commit()
            return replay[1]
        row = store.db.execute("SELECT acceptance_id FROM accepted_heads WHERE source_id=?", (p["source_id"],)).fetchone()
        if (row[0] if row else None) != expected_head: raise NarrativeError("conflict", "accepted head changed")
        # Recheck source and dependency permissions after acquiring the write lock.
        read_version(view, p["version_id"])
        data = {"schema": "translation-acceptance-v1", "plan_id": plan_id, "version_id": p["version_id"],
                "source_id": p["source_id"], "previous_head": expected_head, "actor": p["actor"], "reason": p["reason"],
                "knowledge_status": "candidates_only_not_promoted"}
        aid = _insert(store, "translation-acceptance", data)
        store.db.execute("INSERT INTO accepted_heads VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET acceptance_id=excluded.acceptance_id", (p["source_id"], aid))
        event = {"acceptance_id": aid, "candidates": p["knowledge_candidates"], "promotion": "requires_separate_review"}
        store.db.execute("INSERT INTO review_outbox(event_id,acceptance_id,payload) VALUES(?,?,?)", ("acceptance:"+aid, aid, json.dumps(event, ensure_ascii=False)))
        store.db.execute("INSERT INTO acceptance_keys VALUES(?,?,?)", (idempotency_key, action, aid))
        store.db.commit()
        return aid
    except BaseException:
        store.db.rollback()
        raise


def invalidate_acceptance(store, source_id, *, expected_head, actor, reason):
    ensure_schema(store)
    if not actor or not reason or not expected_head: raise NarrativeError("invalid_record", "named revocation requires current head")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        row = store.db.execute("SELECT acceptance_id FROM accepted_heads WHERE source_id=?", (source_id,)).fetchone()
        if not row or row[0] != expected_head: raise NarrativeError("conflict", "revocation head changed")
        ident = _insert(store, "translation-invalidation", {"source_id": source_id, "acceptance_id": expected_head, "actor": actor, "reason": reason})
        store.db.execute("UPDATE accepted_heads SET acceptance_id=NULL WHERE source_id=?", (source_id,))
        store.db.commit()
    except BaseException:
        store.db.rollback()
        raise
    return ident


def pending_outbox(store):
    ensure_schema(store)
    return [{"event_id": r[0], "acceptance_id": r[1], "payload": json.loads(r[2]),
             "active": current_head(store, store.get(r[1], "translation-acceptance")["source_id"]) == r[1]}
            for r in store.db.execute("SELECT event_id,acceptance_id,payload FROM review_outbox WHERE published=0").fetchall()]


def publish_outbox(store, event_id, consumer):
    """Consumer must be idempotent by event_id; this is a projection, not a distributed transaction."""
    found = [e for e in pending_outbox(store) if e["event_id"] == event_id]
    if not found or not found[0]["active"]: raise NarrativeError("conflict", "outbox event is absent or no longer active")
    consumer(event_id, found[0]["payload"])
    with store.db:
        store.db.execute("UPDATE review_outbox SET published=1 WHERE event_id=?", (event_id,))
