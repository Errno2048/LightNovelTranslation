"""An explicit cross-edition execution path sharing the existing audit protocol."""
import re

from ..review.prompts import AUDIT_V6_TASK, AUDIT_V6_ADJUDICATION, review_examples
from ..review.protocol import locate, parse_records
from ..storage import atomic_text, digest, write_json
from .context import review_reference_packet
from .prompts import j, translation_messages


def _review_rows(pipeline, packet, target, text):
    source = [dict(id=r['paragraph_id'], alias=r['ref'], text=r['text']) for r in target['rows']]
    translated = [{'id': 'target:' + str(i+1), 'alias': 'T' + str(i+1).zfill(4), 'text': line.strip()}
                  for i, line in enumerate(re.split(r'\n\s*\n|\n', text)) if line.strip()]
    refs = {r['ref']: r for doc in packet['source_history'] + packet['style_documents'] for r in doc['rows']}
    for term in packet['terminology']:
        for key in ('source_usage', 'reference_usage'):
            row = term[key]
            refs[row['ref']] = row
    history = [dict(id=r['paragraph_id'], alias=r['ref'], text=r['text']) for r in refs.values()]
    return source, translated, history


def _parse_review(text, source, target, history):
    parsed = parse_records(text, {'FINDING', 'UNRESOLVED', 'SUMMARY'},
        extra_fields={'FINDING': {'basis'}}, syntax_profile='p6-tolerant-v1', allow_preamble=True)
    errors, findings = list(parsed['errors']), []
    for row in parsed['records']:
        if row['record'] != 'FINDING':
            continue
        f = row['fields']
        s, t = locate(f.get('source_ref'), source), locate(f.get('target_ref'), target)
        e = locate(f.get('evidence_ref'), source+target+history) if f.get('evidence_ref') else None
        if not s or not t or (f.get('evidence_ref') and not e):
            errors.append('unbound_finding:' + row['id'])
        if any(not f.get(k) for k in ('category', 'severity', 'basis', 'claim', 'suggestion', 'confidence')):
            errors.append('incomplete_finding:' + row['id'])
        findings.append({'id': row['id'], **f, 'source_binding': s, 'target_binding': t, 'evidence_binding': e})
    summaries = [r for r in parsed['records'] if r['record'] == 'SUMMARY']
    if len(summaries) != 1:
        errors.append('one_review_summary_required')
    else:
        fields = summaries[0]['fields']
        if fields.get('reviewed_all') != 'yes' or fields.get('semantic_coverage_complete') not in {'yes', 'no'}:
            errors.append('review_coverage_required')
        if fields.get('zero_findings') != ('no' if findings else 'yes'):
            errors.append('zero_findings_mismatch')
    return {'findings': findings, 'records': parsed['records'], 'errors': errors, 'complete': not errors,
            'semantic_coverage_claimed': bool(summaries and summaries[0]['fields'].get('semantic_coverage_complete') == 'yes')}


def _parse_decisions(text, findings):
    parsed = parse_records(text, {'DECISION'}, syntax_profile='p6-tolerant-v1', allow_inline_end=True,
                           allow_preamble=True)
    errors, decisions, seen = list(parsed['errors']), [], set()
    expected = {f['id'] for f in findings}
    for row in parsed['records']:
        f = row['fields']
        ident = f.get('finding')
        if ident not in expected or ident in seen or f.get('decision') not in {'confirmed', 'dismissed', 'unresolved'} or not f.get('reason'):
            errors.append('invalid_adjudication:' + row['id'])
        seen.add(ident)
        decisions.append(f)
    if seen != expected:
        errors.append('missing_adjudication')
    return {'decisions': decisions, 'errors': errors, 'complete': not errors}


class CrossEditionTranslator:
    def __init__(self, pipeline):
        self.pipeline = pipeline

    def translate(self, target_id, packet, *, name='reference', output_tokens=8192,
                  review=True, repair=False):
        pipeline = self.pipeline
        pipeline.archive.validate_dependencies(packet['dependencies'])
        if packet['target_id'] != target_id or packet['fingerprint'] != digest({k: v for k, v in packet.items() if k != 'fingerprint'}):
            raise ValueError('reference packet belongs to another target or was modified')
        target = pipeline.archive.read(target_id)
        raw = pipeline.caller.call('edition-translation:' + name, translation_messages(packet, target), output_tokens)
        result = {'schema': 'cross-edition-translation-v1', 'source_id': target_id,
                  'source_revision': target['revision_id'], 'source_edition': target['edition_id'],
                  'reference_mode': 'cross_edition', 'visibility_policy': packet['visibility_policy'],
                  'packet': packet, 'generation_id': raw['id'], 'text': raw['generation']['text'],
                  'status': 'working', 'independent_quality_confirmed': False}
        folder = pipeline.folder / 'translations' / (digest({'name': name, 'target': target_id, 'packet': packet['fingerprint']})[:24])
        def checkpoint():
            write_json(folder / 'result.json', result)
            atomic_text(folder / 'translation.txt', result['text'] + '\n')
        checkpoint()
        if review:
            result['review'] = self.review(target, packet, result['text'], output_tokens=output_tokens)
            checkpoint()
            initial = result['review']
            if initial['status'] == 'complete' and initial['result']['findings']:
                source, translated, history = _review_rows(pipeline, packet, target, result['text'])
                payload = {'context': review_reference_packet(packet, include_body=False), 'source': source,
                           'translation': translated, 'allowed_history': history,
                           'findings': initial['result']['findings']}
                messages = [{'role': 'system', 'content': AUDIT_V6_ADJUDICATION + self._edition_rules()},
                            {'role': 'user', 'content': j(payload)}]
                adjudication = pipeline.caller.structured('edition-adjudication', messages, output_tokens,
                    lambda text: _parse_decisions(text, initial['result']['findings']))
                result['adjudication'] = adjudication
                checkpoint()
                confirmed = {d['finding'] for d in (adjudication.get('result') or {}).get('decisions', [])
                             if d['decision'] == 'confirmed'} if adjudication['status'] == 'complete' else set()
                if repair and confirmed:
                    repairs = [f for f in initial['result']['findings'] if f['id'] in confirmed]
                    repair_payload = {k: v for k, v in payload.items() if k != 'findings'}
                    messages = [{'role': 'system', 'content': '根据Web原文和已确认意见最小修复中文译文，保留其他正确内容，只输出完整译文。' + self._edition_rules()},
                                {'role': 'user', 'content': j({**repair_payload, 'confirmed_findings': repairs})}]
                    revised = pipeline.caller.call('edition-revision', messages, output_tokens)
                    result.update(original_text=result['text'], text=revised['generation']['text'],
                                  revision_generation_id=revised['id'])
                    checkpoint()
                    result['final_review'] = self.review(target, packet, result['text'], output_tokens=output_tokens)
        result['fingerprint'] = digest(result)
        checkpoint()
        return result

    @staticmethod
    def _edition_rules():
        return ('\n【跨版本证据规则】Web与文库的差异本身不是错译。仅凭文库事件、关系、等级或身份不能要求修改Web。'
                '文库只能支持对象相同后的实际名称/风格；发现冲突请按Web判断。'
                'source_ref使用本次给出的J开头段落标签，target_ref使用T标签，evidence_ref使用资料实际标签，不自行构造S/R/L。')

    def review(self, target, packet, text, *, output_tokens=8192):
        source, translated, history = _review_rows(self.pipeline, packet, target, text)
        messages = [{'role': 'system', 'content': AUDIT_V6_TASK + self._edition_rules()}]
        # Examples teach the existing wire syntax, while this task supplies a
        # frozen native-edition registry instead of canonical chapter aliases.
        messages += review_examples('audit-v6')
        messages += [{'role': 'user', 'content': j({'context': review_reference_packet(packet, include_body=False),
                    'source': source, 'translation': translated, 'allowed_history': history})}]
        return self.pipeline.caller.structured('edition-review', messages, output_tokens,
            lambda output: _parse_review(output, source, translated, history))
