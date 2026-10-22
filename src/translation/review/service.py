"""Evidence-grounded review and separate adjudication; no reference Chinese."""
import json

from ..narrative.schemas import NarrativeError
from .protocol import parse_records, locate, bind_exact_header_ids, header_association_errors
from .requests import text_messages
from .versions import read_version
from .lookup import lookup_evidence
from .repair import decode_records, read_records
from .prompts import (AUDIT_TASK, AUDIT_ADJUDICATION, AUDIT_FIELDS, AUDIT_PROFILES,
                      AUDIT_ADJUDICATION_FEWSHOT,
                      AUDIT_SIGNAL_ADJUDICATION_FEWSHOT, review_examples)
from .signals import residue_signals
from ..references import STABLE, reference_task, reference_examples, prompt_projection

CATEGORIES = {"omission", "addition", "negation", "role", "number", "term", "tense", "speaker", "style", "other"}
SEVERITIES = {"critical", "major", "minor", "suggestion"}
REVIEW_TASK = """请独立审校完整原文和当前草稿，检查漏译、增义、否定、主体/客体、指代、条件、数字与单位、称谓一致性。
不要为了贴近某个参考措辞而改写正确表达。历史候选译名可能冲突，未审摘要不能作为硬规则。
逐条输出：
@@FINDING f1
category: omission|addition|negation|role|number|term|tense|speaker|style|other
severity: critical|major|minor|suggestion
source_ref: S标签 | 该段实际存在的逐字日文引文
target_ref: T标签 | 该段实际存在的逐字中文引文
claim: 明确说明含义差异，不仅说不自然
suggestion: 最小修复建议；不确定就明确说不确定
confidence: high|medium|low
evidence_ref: 可选，提供的L标签 | 逐字引文；没有则省略整行
needs_lookup: 可选，chapter:main:章号或node:节点ID；没有则省略整行
缺译时target_ref引用插入点邻近的真实译句，并在claim说明缺失。不得伪造引文或标签。
如果存在无法确定的问题，另输出@@UNRESOLVED u1，text: 问题及可选needs_lookup字段。
最后输出：
@@SUMMARY
reviewed_all: yes
semantic_coverage_complete: yes或no（是否已逐段检查含义覆盖）
zero_findings: yes或no
@@END
没有发现问题也须写SUMMARY和END。仅输出记录，不输出全文改写。"""

PARAGRAPH_REFERENCE_TASK = ('\n【引用协议覆盖】source_ref和target_ref优先只写一个实际存在的S/T标签；'
    '程序会绑定该段完整正文。跨段问题拆成多条意见，勿拼接标签或引文。标签可定位不代表意见正确。'
    'evidence_ref仍必须使用提供的标签与同段逐字引文，不可只给外部标签。')


def parse_review(version, text, extra_rows=(), *, parsed=None, chapter_cross_references=True, intermediate_end=True,
                 allow_label_only=False, review_profile='legacy', signal_candidates=None, derive_count=False,
                 count_policy='strict'):
    if count_policy not in {'strict','records'}:
        raise ValueError('unknown review count policy')
    enhanced = review_profile in AUDIT_PROFILES
    allowed = {"FINDING", "UNRESOLVED", "SUMMARY"} | ({'SIGNAL'} if signal_candidates is not None else set())
    parsed = parsed or parse_records(text, allowed, allow_intermediate_end=intermediate_end,
                                    extra_fields=AUDIT_FIELDS if enhanced else None)
    findings, unresolved, errors = [], [], list(parsed["errors"])
    summaries = [dict(r["fields"]) for r in parsed["records"] if r["record"] == "SUMMARY"]
    for r in parsed["records"]:
        f = r["fields"]
        if r["record"] == "UNRESOLVED":
            unresolved.append({"id": r["id"], **f}); continue
        if r["record"] != "FINDING": continue
        source = locate(f.get("source_ref"), version["source_segments"], require_quote=not (allow_label_only or enhanced))
        target = locate(f.get("target_ref"), version["segments"], require_quote=not (allow_label_only or enhanced))
        evidence_rows = list(extra_rows)+(version["source_segments"]+version["segments"] if chapter_cross_references else [])
        evidence = locate(f.get("evidence_ref"), evidence_rows, require_quote=not enhanced) if f.get("evidence_ref") else None
        issues = []
        if not source: issues.append("source_quote_or_id_invalid")
        if not target: issues.append("target_quote_or_id_invalid")
        if f.get("evidence_ref") and not evidence: issues.append("external_evidence_invalid")
        if f.get("category") not in CATEGORIES or f.get("severity") not in SEVERITIES: issues.append("invalid_category_or_severity")
        if not f.get("claim") or not f.get("suggestion") or f.get("confidence") not in {"high", "medium", "low"}: issues.append("incomplete_finding")
        if enhanced:
            if f.get('basis') not in {'source_meaning','observed_usage','style_preference'}:
                issues.append('missing_or_invalid_basis')
            if f.get('basis') == 'observed_usage' and (not evidence or
                    evidence['paragraph_id'] not in {r['id'] for r in extra_rows}):
                issues.append('historical_usage_needs_external_paragraph')
        findings.append({"id": r["id"], **f, "source_location": source, "target_location": target,
                         "evidence_location": evidence, "grounding_errors": issues,
                         "status": "candidate" if issues else "grounded", "semantic_verdict": "not_adjudicated"})
        if enhanced:
            def bound(location, rows):
                return next((r['text'] for r in rows if location and r['id'] == location['paragraph_id']), None)
            findings[-1]['bound_paragraphs'] = {'source':bound(source,version['source_segments']),
                'target':bound(target,version['segments']), 'evidence':bound(evidence,evidence_rows)}
            findings[-1]['knowledge_authority'] = 'observed_only' if f.get('basis') == 'observed_usage' else 'no_canonical_rule'
    if len(summaries) != 1 or summaries[0].get("reviewed_all") != "yes": errors.append("incomplete_review_declaration")
    summary = summaries[0] if len(summaries) == 1 else {}
    declared_summary = dict(summary)
    if summary.get("semantic_coverage_complete") not in {"yes", "no"}: errors.append("missing_semantic_coverage_declaration")
    count_note = None
    if count_policy=='strict' and derive_count and signal_candidates is not None and len(summaries)==1 and 'zero_findings' not in summary:
        # A count is derivable without inventing a semantic coverage declaration.
        summary['zero_findings']='no' if findings else 'yes'
        count_note='missing_zero_findings_derived_from_record_count'
    expected_count = 'no' if findings else 'yes'
    count_warning = None
    if (count_policy=='records' and len(summaries)==1 and parsed['complete']
            and summary.get('zero_findings') in {'yes','no',None}):
        if summary.get('zero_findings') != expected_count:
            count_warning={'code':'finding_count_declaration_conflict' if 'zero_findings' in summary else 'missing_zero_findings',
                           'declared':summary.get('zero_findings'),'effective':expected_count,
                           'finding_count':len(findings),'action':'derive_metadata_from_records'}
            summary['zero_findings']=expected_count
    if summary.get("zero_findings") != expected_count: errors.append("finding_count_declaration_conflict")
    data = {"findings": findings, "unresolved": unresolved, "summary": summary, "errors": errors,
            "review_complete": not errors, "semantic_coverage_reviewed": not errors and summary.get("semantic_coverage_complete") == "yes"}
    if count_policy=='records':
        data.update(count_policy=count_policy,declared_summary=declared_summary,
                    protocol_warnings=[count_warning] if count_warning else [])
    if parsed.get('leading_prose'):
        # Keep tolerated pre-record prose visible for semantic evaluation; it
        # is never converted into a finding, evidence citation, or decision.
        data['unstructured_preamble'] = parsed['leading_prose']
    if signal_candidates is not None:
        from ..mvp.signals import resolutions
        data['signal_resolutions'],extra = resolutions(version,parsed,signal_candidates,findings)
        errors.extend(extra)
        data.update(review_complete=not errors,semantic_coverage_reviewed=not errors and summary.get('semantic_coverage_complete')=='yes')
    notes=list(parsed.get('notes',[]))+([count_note] if count_note else [])
    if notes: data['protocol_notes']=notes
    return data


def _contains_message_block(messages, block):
    return any(messages[start:start+len(block)] == block for start in range(len(messages)-len(block)+1))


def review_translation(session, version_id, *, lookup_ids=(), prior_report_id=None, include_context=True, candidate_observations=(),
                       context_profile='hierarchical', context_id=None, reference_mode='literal', review_profile='legacy', checks_id=None):
    if review_profile not in {'legacy',*AUDIT_PROFILES}:
        raise NarrativeError('invalid_record','unknown review prompt profile')
    if reference_mode not in {'literal','paragraph'}:
        raise NarrativeError('invalid_record','unknown review reference mode')
    v = read_version(session.view, version_id)
    stable = v.get('reference_profile') == STABLE
    rows, evidence = lookup_evidence(session.view, version_id, lookup_ids)
    enhanced = review_profile in AUDIT_PROFILES
    messages = text_messages(v, AUDIT_PROFILES[review_profile][0] if enhanced else REVIEW_TASK)
    tolerant_protocol = review_profile == 'chapter-review'
    context = session.view.store.get(v["context_id"], "review-context")
    # Context is optional; source and draft remain explicit, tagged, first-class inputs.
    evidence_context = None
    if include_context and context_profile != 'legacy':
        from .context import compile_review_context, read_review_context, context_messages
        context_id = context_id or compile_review_context(session.view,version_id,profile=context_profile)
        evidence_context = read_review_context(session.view,version_id,context_id)
        rows += evidence_context['rows']
        messages[1:1] = context_messages(evidence_context)
    elif context_id:
        raise NarrativeError('invalid_record','explicit evidence context requires enriched context mode')
    if include_context and context_profile == 'legacy':
        info = [b for b in context["prompt_blocks"] if b["kind"] in {"near_chapter", "translation_metadata"}]
        messages.insert(-1, {"role": "user", "content": "【允许的前文及未审候选；不是本章真相或强制规范】\n"+json.dumps(info, ensure_ascii=False)})
        if context.get("index_id"):
            try:
                from ..narrative.multilevel import read_multilevel_index, list_children
                index = read_multilevel_index(session.view, context["index_id"])
                nodes = index["roots"]+sum([list_children(session.view, context["index_id"], n["node_id"]) for n in index["roots"]], [])
                catalog = [{"node_id": n["node_id"], "level": n["level"], "chapters": [h["match_key"] for h in n["headers"]],
                            "built_in_context": any(s["block_id"] == n["node_id"] for s in context["selected_summaries"])} for n in nodes]
                messages.insert(-1, {"role": "user", "content": "【详情目录；未建不是无事件，读取不会自动付费创建】\n"+json.dumps(catalog, ensure_ascii=False)})
            except NarrativeError as exc:
                if exc.kind != "coverage_gap": raise
    if evidence:
        messages.insert(-1, {"role": "user", "content": "【按需取证结果；引用请用L标签】\n"+json.dumps(evidence, ensure_ascii=False)})
    if prior_report_id:
        previous = read_report(session.view, version_id, prior_report_id)
        messages.insert(-1, {"role": "user", "content": "【前一轮需核验的审校候选，不能当作已确认结论】\n"+json.dumps(previous["findings"], ensure_ascii=False)})
    if candidate_observations:
        messages.insert(-1, {"role": "user", "content": "【外部原文审阅提出的待核验线索；不是正确答案】\n"
            "请逐项回到原文和草稿核验，可确认、反驳或保持不确定，不机械照抄。仍须输出实际S/T标签和逐字引文。\n"+
            json.dumps(list(candidate_observations), ensure_ascii=False)})
    signals = residue_signals(v) if enhanced else []
    if signals:
        messages.insert(-1, {'role':'user','content':'【通用规则扫描候选，不是已确认错译；逐处核对原文】\n'+json.dumps(signals,ensure_ascii=False)})
    if reference_mode == 'paragraph' and not enhanced:
        messages[-1]['content'] += PARAGRAPH_REFERENCE_TASK
    signal_candidates = None
    allowed = {"FINDING", "UNRESOLVED", "SUMMARY"}
    if checks_id:
        from .checks import read_checks
        from ..mvp.signals import SIGNAL_TASK
        signal_candidates = read_checks(session.view,version_id,checks_id)['signals']
        visible_signals=prompt_projection(signal_candidates,session.view.archive.chapters.values()) if stable else signal_candidates
        messages[-1]['content'] += SIGNAL_TASK+'\n【本版本规则信号】\n'+json.dumps(visible_signals,ensure_ascii=False)
        allowed.add('SIGNAL')
    if tolerant_protocol:
        examples = reference_examples(review_examples(review_profile,with_signals=bool(checks_id)), stable)
        messages[-1:-1] = examples
    if stable:
        messages[-1]['content'] = reference_task(messages[-1]['content'])
    raw = session.call(version_id, "review", messages)
    if raw.get('tool_result_ids'):
        tool_rows, _ = session.evidence_broker.evidence(raw['tool_result_ids'])
        rows += tool_rows
    parsed, repair_id = decode_records(session, version_id, raw, allowed,
                                       extra_fields=AUDIT_FIELDS if enhanced else None,
                                       syntax_profile='tolerant-records' if tolerant_protocol else 'legacy',
                                       allow_preamble=tolerant_protocol)
    mode = ("candidate_assisted" if candidate_observations else "followup" if prior_report_id
            else "lookup_assisted" if lookup_ids else "rule_assisted" if signals else "independent")
    data = {"schema": "review-flat", "parser_version": "review-flat-codec", "version_id": version_id, "generation_id": raw["id"],
            "lookup_ids": list(lookup_ids), "prior_report_id": prior_report_id, "repair_id": repair_id,
            "mode": mode, "candidate_observations": list(candidate_observations),
            **parse_review(v, raw["generation"]["text"], rows, parsed=parsed, allow_label_only=reference_mode=='paragraph',review_profile=review_profile,signal_candidates=signal_candidates,derive_count=bool(checks_id),
                           count_policy='records' if review_profile=='chapter-review' else 'strict')}
    if checks_id: data.update(checks_id=checks_id,signal_codec='count-normalization')
    if enhanced:
        data.update(review_profile=review_profile,
                    parser_version='review-flat-codec',
                    reference_mode='bound-paragraph',rule_candidates=signals)
    elif reference_mode != 'literal': data['reference_mode'] = reference_mode
    if evidence_context is not None:
        data['review_context_id'] = context_id
    if raw.get('tool_scope'):
        data.update(tool_scope=raw['tool_scope'],tool_result_ids=raw.get('tool_result_ids',[]))
    return session.view.store.put("review-report", data)


def read_report(view, version_id, report_id):
    v = read_version(view, version_id)
    report = view.store.get(report_id, "review-report")
    if report["version_id"] != version_id:
        raise NarrativeError("conflict", "review belongs to another text version")
    raw = view.store.get(report["generation_id"], "review-generation")
    reference_mode = report.get('reference_mode', 'literal')
    enhanced = report.get('review_profile') in AUDIT_PROFILES
    task = raw['request']['messages'][-1]['content']
    actual_profile = next((p for p in AUDIT_PROFILES if task.startswith('【审校协议 '+p+'】')), 'legacy')
    if report.get('review_profile','legacy') != actual_profile:
        raise NarrativeError('invalid_record', 'review profile differs from actual model input')
    if enhanced:
        profile = report.get('review_profile')
        expected_parser = 'review-flat-codec'
        if (reference_mode != 'bound-paragraph' or report.get('parser_version') != expected_parser
                or not task.startswith(reference_task(AUDIT_PROFILES[report['review_profile']][0])
                    if v.get('reference_profile')==STABLE else AUDIT_PROFILES[report['review_profile']][0])
                or report.get('rule_candidates') != residue_signals(v)):
            raise NarrativeError('invalid_record','enhanced review protocol binding differs')
        if profile == 'chapter-review':
            messages = raw['request']['messages']
            required = [reference_examples(review_examples(profile,with_signals=bool(report.get('checks_id'))),
                v.get('reference_profile')==STABLE)]
            if any(not _contains_message_block(messages, block) for block in required):
                raise NarrativeError('invalid_record','audit format examples differ from actual model input')
    elif (reference_mode not in {'literal', 'paragraph'} or
            task.endswith(PARAGRAPH_REFERENCE_TASK) != (reference_mode == 'paragraph')):
        raise NarrativeError('invalid_record', 'review reference mode differs from actual model input')
    rows, _ = lookup_evidence(view, version_id, report["lookup_ids"])
    if report.get('tool_scope'):
        from ..mvp.integration import tool_evidence
        if report.get('tool_result_ids') != raw.get('tool_result_ids') or report['tool_scope'] != raw.get('tool_scope'):
            raise NarrativeError('invalid_record','tool evidence differs from model request')
        tool_rows,_ = tool_evidence(view,report['tool_scope'],report['tool_result_ids'])
        rows += tool_rows
    if report.get('review_context_id'):
        from .context import read_review_context, context_messages
        evidence_context = read_review_context(view,version_id,report['review_context_id'])
        prefix = context_messages(evidence_context)
        # Include the boundary: a shorter context sharing the same guide must
        # not relabel a generation that actually received extra documents.
        if raw['request']['messages'][1:2+len(prefix)] != prefix+[text_messages(v, '')[1]]:
            raise NarrativeError('invalid_record','report context differs from actual model input')
        rows += evidence_context['rows']
    current = report.get("parser_version") == "review-flat-codec"
    tolerant_protocol = report.get('parser_version') == 'review-flat-codec'
    signal_candidates = None
    allowed = {"FINDING", "UNRESOLVED", "SUMMARY"}
    if report.get('checks_id'):
        from .checks import read_checks
        signal_candidates = read_checks(view,version_id,report['checks_id'])['signals']
        allowed.add('SIGNAL')
    parsed = read_records(view, version_id, report["generation_id"], report.get("repair_id"), allowed,
                          allow_intermediate_end=current,extra_fields=AUDIT_FIELDS if enhanced else None,
                          syntax_profile='tolerant-records' if tolerant_protocol else 'legacy',
                          allow_preamble=tolerant_protocol)
    expected = parse_review(v, raw["generation"]["text"], rows, parsed=parsed,
                            chapter_cross_references=report.get("parser_version") in {"review-flat-codec", "review-flat-codec", "review-flat-codec", "review-flat-codec", "review-flat-codec"}, intermediate_end=current,
                            allow_label_only=report.get('reference_mode')=='paragraph',review_profile=report.get('review_profile','legacy'),signal_candidates=signal_candidates,
                            derive_count=report.get('signal_codec')=='count-normalization',
                            count_policy='records' if report.get('review_profile')=='chapter-review' else 'strict')
    if any(report[k] != val for k, val in expected.items()) or raw["version_id"] != version_id:
        raise NarrativeError("invalid_record", "review differs from raw output/version")
    return report


def adjudicate_findings(view, version_id, report_id, decisions, *, actor, method, signal_decisions=None, tool_result_ids=(), tool_scope=None):
    report = read_report(view, version_id, report_id)
    if not actor or not method or len({d["finding"] for d in decisions}) != len(decisions):
        raise NarrativeError("invalid_record", "unique decisions and named adjudicator required")
    by_id = {f["id"]: f for f in report["findings"]}
    for d in decisions:
        if d.get("finding") not in by_id or d.get("decision") not in {"confirmed", "dismissed", "unresolved"} or not d.get("reason"):
            raise NarrativeError("invalid_record", "invalid finding decision")
        if d["decision"] == "confirmed" and by_id[d["finding"]]["status"] != "grounded":
            raise NarrativeError("invalid_record", "ungrounded finding cannot be confirmed")
        if (report.get('review_profile') in AUDIT_PROFILES and d['decision']=='confirmed' and
                (by_id[d['finding']].get('basis')=='style_preference' or by_id[d['finding']]['severity']=='suggestion')):
            raise NarrativeError('invalid_record','style preference is not a confirmed translation error')
    data = {"version_id": version_id, "report_id": report_id,"decisions": decisions, "actor": actor, "method": method}
    if signal_decisions is not None:
        if not report.get('checks_id') or any(not r.get('grounded') for r in signal_decisions):
            raise NarrativeError('invalid_record','signal decisions need bound checks and grounded references')
        from .checks import read_checks
        signals = read_checks(view,version_id,report['checks_id'])['signals']
        if len(signal_decisions)!=len(signals) or {r['id'] for r in signal_decisions}!={s['id'] for s in signals}:
            raise NarrativeError('invalid_record','signal decisions incomplete')
        data.update(checks_id=report['checks_id'],signal_decisions=signal_decisions)
    if tool_scope: data.update(tool_scope=tool_scope,tool_result_ids=list(tool_result_ids))
    return view.store.put('review-adjudication',data)


def model_adjudicate(session, version_id, report_id, *, protocol_profile=None, no_op_proposal_id=None):
    v, report = read_version(session.view, version_id), read_report(session.view, version_id, report_id)
    stable = v.get('reference_profile') == STABLE
    enhanced = report.get('review_profile') in AUDIT_PROFILES
    prompt_profile = protocol_profile or report.get('review_profile')
    if protocol_profile is not None and protocol_profile not in AUDIT_PROFILES:
        raise NarrativeError('invalid_record', 'unknown adjudication prompt profile')
    legacy_task = ("请独立核验下面的审校意见。引文存在只代表可定位，不能据此直接确认。"
        "逐项判断原文是否支持其差异解释；风格偏好或无依据设定不可确认。每项输出：\n@@DECISION d1\nfinding: f1\n"
        "decision: confirmed|dismissed|unresolved\nreason: 依据原文明确解释判断\n最后@@END。所有finding必须给决定。\n")
    visible_findings=prompt_projection(report['findings'],session.view.archive.chapters.values()) if stable else report['findings']
    messages = text_messages(v, (AUDIT_PROFILES[prompt_profile][1] if enhanced or protocol_profile else legacy_task)+json.dumps(visible_findings,ensure_ascii=False))
    tolerant_protocol = prompt_profile in {'chapter-review','chapter-review'}
    rows, evidence = lookup_evidence(session.view, version_id, report["lookup_ids"])
    if report.get('review_context_id'):
        from .context import read_review_context, context_messages
        messages[1:1] = context_messages(read_review_context(session.view,version_id,report['review_context_id']))
    if evidence: messages.insert(-1, {"role": "user", "content": json.dumps(evidence, ensure_ascii=False)})
    if report.get('tool_result_ids'):
        from ..mvp.integration import tool_evidence
        tool_rows,_ = tool_evidence(session.view,report['tool_scope'],report['tool_result_ids'])
        messages.insert(-1,{'role':'user','content':'【审校查证得到的原文证据】\n'+json.dumps(tool_rows,ensure_ascii=False)})
    allowed = {'DECISION'}
    if report.get('checks_id'):
        from .checks import read_checks
        from ..mvp.signals import SIGNAL_TASK,resolutions
        signals = read_checks(session.view,version_id,report['checks_id'])['signals']
        signal_payload={'signals':signals,'review':report['signal_resolutions']}
        if stable:
            signal_payload=prompt_projection(signal_payload,session.view.archive.chapters.values())
        messages[-1]['content'] += SIGNAL_TASK+'\n【规则信号与审校候选处置】\n'+json.dumps(signal_payload,ensure_ascii=False)
        template=[]
        for f in report['findings']:
            template.append('@@DECISION '+f['id']+'\nfinding: '+f['id']+'\ndecision: confirmed|dismissed|unresolved\nreason: 具体原文依据及修复方向')
        for signal in signals:
            template.append('@@SIGNAL '+signal['id']+'\nsignal: '+signal['id']+
                '\ndecision: confirmed|dismissed|unresolved\nreason: 具体判断依据\nsource_ref: 实际S标签\ntarget_ref: 实际T标签')
        messages[-1]['content'] += ('\n【P6裁决输出ID协议 v2，覆盖前述示例编号】\n'
            '逐条复制下面完整ID，保留finding或signal字段；记录头也用同一个完整ID。不要改成d1/s1。'
            'decision只用三个英文值。不得省略任何记录；只替换占位说明，不照抄候选判断。'
            '先核对绑定正文，再核对意见和建议各自是否成立；声称原文使用「」或『』时必须与绑定正文逐字一致。'
            '若confirmed的SIGNAL关联错译，另补finding字段指向对应finding。\n'+'\n\n'.join(template)+'\n@@END')
        allowed.add('SIGNAL')
    elif tolerant_protocol:
        template=[]
        for finding in report['findings']:
            template.append('@@DECISION '+finding['id']+'\nfinding: '+finding['id']+
                '\ndecision: confirmed|dismissed|unresolved\nreason: 具体原文依据及判断理由')
        messages[-1]['content'] += ('\n【当前候选的逐条ID模板】记录头和finding字段使用同一个完整ID；'
            '每条模板均须作出且仅作出一项判断，不得使用d1、d2等替代ID。\n'+
            '\n\n'.join(template)+'\n@@END')
    if tolerant_protocol:
        examples = AUDIT_ADJUDICATION_FEWSHOT + (AUDIT_SIGNAL_ADJUDICATION_FEWSHOT if report.get('checks_id') else [])
        messages[-1:-1] = reference_examples(examples, stable)
    if stable:
        messages[-1]['content'] = reference_task(messages[-1]['content'])
    if no_op_proposal_id:
        # This recovery is deliberately narrow: validate the immutable patch
        # normally, and only reconsider a fully valid but unchanged result.
        from .revision import apply_revision
        proposal = session.view.store.get(no_op_proposal_id, 'revision-proposal')
        if proposal['version_id'] != version_id or proposal['report_id'] != report_id:
            raise NarrativeError('conflict', 'no-op feedback belongs to another review/version')
        try:
            apply_revision(session.view, version_id, no_op_proposal_id, validate_only=True)
        except NarrativeError as exc:
            if exc.kind != 'invalid_review' or str(exc) != 'invalid_review: revision made no change':
                raise
        else:
            raise NarrativeError('invalid_record', 'feedback requires an unchanged revision')
        previous = session.view.store.get(proposal['adjudication_id'], 'review-adjudication')
        feedback = {'previous_decisions': previous['decisions'], 'patches': proposal['parsed']['records']}
        if stable:
            feedback = prompt_projection(feedback, session.view.archive.chapters.values())
        messages[-1]['content'] += ('\n【无变化修订的唯一一次复核】\n'
            '程序已验证下列补丁的定位、hash和范围均有效，但所有replacement与当前正文逐字相同。'
            '这不证明原文正确，也不证明意见错误。请重新核验原日文、当前中文、意见和修复建议的实际差异。'
            '若原稿本来正确，应dismissed并解释；确有错误则confirmed并说明实际差异和修复方向；'
            '证据不足则unresolved。不能为完成修订而编造差异或强行改写。仍需重新输出全部DECISION及SIGNAL。\n'
            + json.dumps(feedback, ensure_ascii=False))
    raw = session.call(version_id, "adjudication", messages,
        **({'task_family': 'no-op-adjudication:'+no_op_proposal_id} if no_op_proposal_id else {}))
    parsed = parse_records(raw["generation"]["text"], allowed,allow_inline_end=enhanced,
                           syntax_profile='tolerant-records' if tolerant_protocol else 'legacy',
                           extra_fields={'DECISION':{'source_ref','target_ref'}} if tolerant_protocol else None)
    if tolerant_protocol:
        expected={'DECISION':{f['id'] for f in report['findings']},
                  'SIGNAL':{s['id'] for s in signals} if report.get('checks_id') else set()}
        notes=bind_exact_header_ids(parsed,expected)
        parsed['errors'].extend(header_association_errors(parsed))
        if notes:
            session.view.store.put('review-protocol-recovery',{'version_id':version_id,'generation_id':raw['id'],
                'parser':'exact-header-id','notes':notes,'raw_unchanged':True})
    if report.get('checks_id') and not tolerant_protocol:
        notes=[]
        expected={'DECISION':{f['id'] for f in report['findings']},'SIGNAL':{s['id'] for s in signals}}
        enums={'确认':'confirmed','確定':'confirmed','驳回':'dismissed','駁回':'dismissed','否决':'dismissed','未决':'unresolved','不确定':'unresolved'}
        for record in parsed['records']:
            f=record['fields']; field='finding' if record['record']=='DECISION' else 'signal'
            if field not in f and record['id'] in expected[record['record']]:
                f[field]=record['id'];notes.append('bind_exact_header_id:'+record['id'])
            if f.get('decision') in enums:
                f['decision']=enums[f['decision']];notes.append('closed_enum_alias:'+record['id'])
        if notes:
            session.view.store.put('review-protocol-recovery',{'version_id':version_id,'generation_id':raw['id'],
                'parser':'decision-header','notes':notes,'raw_unchanged':True})
    if parsed.get('notes'):
        session.view.store.put('review-protocol-recovery',{'version_id':version_id,'generation_id':raw['id'],
            'parser':'decision-tolerant-syntax' if tolerant_protocol else 'decision-inline-end',
            'notes':parsed['notes'],'raw_unchanged':True})
    decisions = [r["fields"] for r in parsed["records"] if r['record']=='DECISION']
    if parsed["errors"] or {d.get("finding") for d in decisions} != {f["id"] for f in report["findings"]}:
        raise NarrativeError("invalid_review", "adjudication incomplete; raw retained")
    grounded = {f["id"] for f in report["findings"] if f["status"] == "grounded"}
    by_id = {f['id']:f for f in report['findings']}
    for decision in decisions:
        if decision.get('decision') not in {'confirmed','dismissed','unresolved'} or not decision.get('reason'):
            raise NarrativeError('invalid_review','invalid adjudication value or missing reason; raw retained')
        # Optional redundant locators may be retained, but cannot redirect a
        # finding to another paragraph or turn invalid quotes into evidence.
        for field,segments,location in (('source_ref',v['source_segments'],'source_location'),
                                       ('target_ref',v['segments'],'target_location')):
            if field not in decision: continue
            bound=locate(decision[field],segments)
            original=by_id[decision['finding']].get(location)
            if not bound or not original or bound['paragraph_id']!=original['paragraph_id']:
                raise NarrativeError('invalid_review','conflicting adjudication locator; raw retained')
    quarantined = []
    policy_changes = []
    for decision in decisions:
        if decision.get("decision") == "confirmed" and decision.get("finding") not in grounded:
            quarantined.append(dict(decision))
            decision.update(decision="unresolved", reason="model confirmation lacks valid grounding; "+decision.get("reason", ""))
        elif (enhanced and decision.get('decision')=='confirmed' and
                (by_id[decision['finding']].get('basis')=='style_preference' or by_id[decision['finding']]['severity']=='suggestion')):
            policy_changes.append(dict(decision))
            decision.update(decision='dismissed',reason='policy: style preference is optional, not a translation error; '+decision.get('reason',''))
    if quarantined:
        session.view.store.put("review-decision-quarantine", {"version_id": version_id, "generation_id": raw["id"], "original_decisions": quarantined})
    if policy_changes:
        session.view.store.put('review-decision-policy',{'version_id':version_id,'generation_id':raw['id'],
            'policy':'chapter-review-no-style-enforcement','original_decisions':policy_changes})
    signal_decisions = None
    if report.get('checks_id'):
        signal_decisions,errors = resolutions(v,parsed,signals,report['findings'])
        confirmed = {d['finding'] for d in decisions if d['decision']=='confirmed'}
        if errors or any(r['decision']=='confirmed' and r.get('finding') not in confirmed for r in signal_decisions):
            raise NarrativeError('invalid_review','signal adjudication incomplete or conflicting; raw retained')
    # The historical method name means a separate HTTP request; assistance and
    # detection independence are recorded by the report's mode/prior_report_id.
    return adjudicate_findings(session.view, version_id, report_id, decisions,
        actor="model:"+session.provider_identity["model"],
        method="independent_request:"+raw["id"]+";prompt_profile:"+str(prompt_profile)
            + (';no_op_proposal:'+no_op_proposal_id if no_op_proposal_id else ''),signal_decisions=signal_decisions,
        tool_scope=raw.get('tool_scope'),tool_result_ids=raw.get('tool_result_ids',[]))


def review_with_lookup(session, version_id, *, include_context=True,
                       context_profile='hierarchical', context_id=None, reference_mode='literal',review_profile='legacy'):
    """One model-requested evidence round; cannot recurse or silently build nodes."""
    from .lookup import resolve_review_queries
    first = review_translation(session, version_id, include_context=include_context,
        context_profile=context_profile, context_id=context_id, reference_mode=reference_mode,review_profile=review_profile)
    report = read_report(session.view, version_id, first)
    queries = list(dict.fromkeys(x["needs_lookup"] for x in report["findings"]+report["unresolved"] if x.get("needs_lookup")))
    if not queries:
        return {"initial_report_id": first, "final_report_id": first, "lookup_ids": []}
    # Surface over-budget requests; never silently select two and pretend all were answered.
    evidence = resolve_review_queries(session.view, version_id, queries)
    lookup_ids = [e["id"] for e in evidence]
    final = review_translation(session, version_id, lookup_ids=lookup_ids, prior_report_id=first,
        include_context=include_context, context_profile=context_profile,
        context_id=report.get('review_context_id', context_id), reference_mode=reference_mode,review_profile=review_profile)
    return {"initial_report_id": first, "final_report_id": final, "lookup_ids": lookup_ids}
