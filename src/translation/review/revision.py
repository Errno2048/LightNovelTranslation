"""Version-checked contiguous patches within the recorded semantic revision limit."""
import json

from ..narrative.schemas import NarrativeError
from ..storage import digest
from .protocol import parse_records, ids
from .requests import text_messages
from .versions import read_version, create_version, segment_translation, revision_limit
from .service import read_report
from .prompts import AUDIT_PROFILES
from .repair import decode_records, read_records
from ..references import STABLE, prompt_projection


def confirmed_findings(view, version_id, report_id, adjudication_id):
    report = read_report(view, version_id, report_id)
    a = view.store.get(adjudication_id, "review-adjudication")
    if a["version_id"] != version_id or a["report_id"] != report_id:
        raise NarrativeError("conflict", "adjudication belongs to another review/version")
    confirmed = {d["finding"]: d for d in a["decisions"] if d["decision"] == "confirmed"}
    found = [{**f, "adjudication_reason": confirmed[f["id"]]["reason"]} for f in report["findings"] if f["id"] in confirmed]
    if any(f["status"] != "grounded" for f in found):
        raise NarrativeError("invalid_record", "cannot revise from ungrounded findings")
    return found


def propose_revision(session, version_id, report_id, adjudication_id):
    v = read_version(session.view, version_id)
    if v["round"] >= revision_limit(session.view): raise NarrativeError("budget_exceeded", "semantic revision budget used")
    findings = confirmed_findings(session.view, version_id, report_id, adjudication_id)
    if not findings: raise NarrativeError("invalid_record", "no confirmed findings to revise")
    stable = v.get('reference_profile') == STABLE
    visible_findings = (prompt_projection(findings,session.view.archive.chapters.values()) if stable else findings)
    hashes = [{"target": s["alias"], "expected_text_hash": digest(s["text"])} for s in v["segments"]]
    messages = text_messages(v, "请只修复已确认的问题，保留其他正确内容和原有段落。不要参考已有中文成品。"
        "suggestion是候选建议；如与adjudication_reason中的裁决范围或要求不一致，以裁决理由为准。"
        "每项补丁输出@@PATCH p1，start和end为连续目标段的T标签；默认只修改单段。"
        "expected_text_hash必须原样复制提供的段落hash（单段补丁），findings列出本补丁修复的已确认ID。"
        "replacement是完整替换段，不含T标签，不新增段落。可输出多项不重叠补丁，最后@@END。格式：\n"
        "@@PATCH p1\nstart: T001\nend: T001\nexpected_text_hash: 已提供hash\nfindings: f1\nreplacement: 完整新段落\n@@END\n"
        "【已确认问题】\n"+json.dumps(visible_findings, ensure_ascii=False)+"\n【当前段hash】\n"+json.dumps(hashes, ensure_ascii=False))
    if stable:
        example=v['segments'][0]['alias']
        messages[-1]['content']=messages[-1]['content'].replace('start: T001','start: '+example).replace('end: T001','end: '+example)
    report = read_report(session.view,version_id,report_id)
    if report.get('review_profile') in AUDIT_PROFILES:
        messages[-1]['content'] += ('\n【修订协议 chapter-review】每个已确认finding均绑定一个目标段。'
            '必须修复每条意见所绑定的T段；不能用修改另一段冒充完成。一个段有多条意见时合并一条PATCH并列出全部ID。'
            '每条PATCH必须start=end且等于所引用finding的target_location.alias。不要修改未确认问题或仅有风格偏好的段。'
            '不要只处理同类问题的第一个例子。替换时逐句保留原段其他事实、推测程度、条件和否定。'
            '历史译法是本章一致性候选，不是已审定全书规范。完成前自查所有已列T段和占位残留。')
    if report.get('review_context_id'):
        from .context import read_review_context, context_messages
        messages[1:1] = context_messages(read_review_context(session.view,version_id,report['review_context_id']))
        messages[-1]['content'] += ('\n【本次补丁限制】提供的hash仅对应单段。每条PATCH必须start=end，'
            '跨多个段落的问题请拆为多条单段补丁，各自复制对应段的hash。不得用第一段hash冒充整个范围hash，'
            '不得计算或猜测hash。替换段仍须完整保留其中所有未被确认有误的信息。')
    if report.get('tool_result_ids'):
        from ..mvp.integration import tool_evidence
        tool_rows,_ = tool_evidence(session.view,report['tool_scope'],report['tool_result_ids'])
        messages.insert(-1,{'role':'user','content':'【本次查证正文】\n'+json.dumps(tool_rows,ensure_ascii=False)})
    adjudication = session.view.store.get(adjudication_id,'review-adjudication')
    if adjudication.get('tool_result_ids'):
        from ..mvp.integration import tool_evidence
        tool_rows,_ = tool_evidence(session.view,adjudication['tool_scope'],adjudication['tool_result_ids'])
        messages.insert(-1,{'role':'user','content':'【裁决查证正文】\n'+json.dumps(tool_rows,ensure_ascii=False)})
    if stable:
        messages[-1]['content']=messages[-1]['content'].replace('T段','中文段落')
    raw = session.call(version_id, "revision", messages)
    parsed, repair_id = decode_records(session, version_id, raw, {"PATCH"})
    data = {"schema": "translation-patch-v1", "version_id": version_id, "report_id": report_id,
            "adjudication_id": adjudication_id, "generation_id": raw["id"], "parsed": parsed, "repair_id": repair_id}
    return session.view.store.put("revision-proposal", data)


def apply_revision(view, version_id, proposal_id, *, validate_only=False):
    v = read_version(view, version_id)
    p = view.store.get(proposal_id, "revision-proposal")
    if p["version_id"] != version_id: raise NarrativeError("conflict", "patch targets another version")
    if p["parsed"]["errors"]: raise NarrativeError("invalid_review", "patch protocol invalid")
    raw = view.store.get(p["generation_id"], "review-generation")
    if p["parsed"] != read_records(view, version_id, p["generation_id"], p.get("repair_id"), {"PATCH"}) or raw["version_id"] != version_id:
        raise NarrativeError("invalid_record", "patch differs from raw response")
    findings = confirmed_findings(view, version_id, p["report_id"], p["adjudication_id"])
    report = read_report(view,version_id,p['report_id'])
    enhanced = report.get('review_profile') in AUDIT_PROFILES
    allowed = {f["id"] for f in findings}
    targets = {f['id']:f['target_location']['alias'] for f in findings}
    by_alias = {s["alias"]: i for i, s in enumerate(v["segments"])}
    replacements, touched, handled = [], set(), set()
    for record in p["parsed"]["records"]:
        f = record["fields"]
        start, end = by_alias.get(f.get("start")), by_alias.get(f.get("end"))
        refs = ids(f.get("findings", ""))
        if start is None or end is None or end < start or not refs or set(refs)-allowed:
            raise NarrativeError("invalid_record", "invalid patch range/finding")
        if enhanced and (start != end or any(targets[ident] != f['start'] for ident in refs)):
            raise NarrativeError('invalid_record','patch must cover the exact paragraph of every referenced finding')
        positions = set(range(start, end+1))
        if touched & positions: raise NarrativeError("conflict", "overlapping patches")
        a, b = v["segments"][start]["start"], v["segments"][end]["end"]
        old = v["text"][a:b]
        new = f.get("replacement", "")
        if digest(old) != f.get("expected_text_hash"):
            raise NarrativeError("conflict", "expected text hash does not match current text")
        if len(segment_translation(new, v["source_revision"])) != end-start+1 or "@@" in new or "```" in new:
            raise NarrativeError("invalid_record", "patch cannot delete/add paragraphs or machine markup")
        touched |= positions; handled.update(refs)
        replacements.append({"start": a, "end": b, "replacement": new, "old_hash": digest(old), "findings": refs})
    if allowed-handled: raise NarrativeError("invalid_review", "patch omitted confirmed findings")
    text = v["text"]
    for patch in sorted(replacements, key=lambda r: r["start"], reverse=True):
        text = text[:patch["start"]]+patch["replacement"]+text[patch["end"]:]
    if text == v["text"]: raise NarrativeError("invalid_review", "revision made no change")
    previous = v
    while previous.get("parent"):
        previous = read_version(view, previous["parent"])
        if previous["text_hash"] == digest(text): raise NarrativeError("invalid_review", "revision oscillates to previous text")
    if validate_only:
        return None
    revision = {"version_id": version_id, "proposal_id": proposal_id, "patches": replacements,
                "result_hash": digest(text), "resolved_candidates": sorted(handled), "status": "requires_regression_review"}
    rid = view.store.put("translation-revision", revision)
    context = view.store.get(v["context_id"], "review-context")
    report = read_report(view, version_id, p["report_id"])
    extra = [dep for ident in report.get("lookup_ids", [])
             for dep in view.store.get(ident, "review-lookup")["dependencies"]]
    if report.get('review_context_id'):
        from .context import read_review_context
        extra += read_review_context(view,version_id,report['review_context_id'])['dependencies']
    if report.get('tool_result_ids'):
        from ..mvp.integration import tool_evidence
        _,deps = tool_evidence(view,report['tool_scope'],report['tool_result_ids'])
        extra += deps
    adjudication = view.store.get(p['adjudication_id'],'review-adjudication')
    if adjudication.get('tool_result_ids'):
        from ..mvp.integration import tool_evidence
        _,deps = tool_evidence(view,adjudication['tool_scope'],adjudication['tool_result_ids'])
        extra += deps
    return create_version(view, context, text, parent=version_id, revision_id=rid,
                          additional_dependencies=extra)
