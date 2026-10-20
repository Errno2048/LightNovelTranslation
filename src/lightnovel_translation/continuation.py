"""Bounded continuation search with durable model I/O and passage evidence."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import unicodedata

from translation.budget import RoleBudget, plan_request
from translation.editions.calls import EditionCaller
from translation.editions.prompts import summary_messages
from translation.editions.protocol import parse_summary
from translation.providers import ProviderError
from translation.narrative.schemas import NarrativeError
from translation.review.protocol import parse_records
from translation.storage import digest, read_json, write_json

from .ingestion import ReferenceSetPlan


VERSION = 'continuation-v1'


def j(value):
    # Cached JSON artifacts sort dictionary keys; prompts must do so as well.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _reference_documents(archive, edition_id, reference_set):
    volume_titles = {_fold(Path(r['path']).stem) for r in reference_set['ordered_sources']}
    # Endings/epilogues are narrative; author afterwords and book title pages are not.
    return [d for d in archive.documents(edition_id)
            if _fold(d['raw_title']) not in volume_titles and not re.match(
                r'^(?:后记|後記|あとがき|後書き|版权页|版權頁|插图|插圖|下卷预告|下卷預告|下卷试读|下卷試讀)(?:\s|$|[：:])',
                _fold(d['raw_title']))]


FIELDS = {
    'CANDIDATE': {'chapter_id', 'reason', 'thread'},
    'ANCHOR': {'source_ref', 'reference_ref', 'source_quote', 'reference_quote',
               'relation', 'thread', 'role', 'reason'},
    'BOUNDARY': {'status', 'chapter_id', 'end_anchor', 'evidence_ref', 'evidence_quote',
                 'reason', 'repeat_content', 'missing_context', 'state_compatible', 'threads_checked'},
    'THREAD': {'name', 'last_anchor', 'remaining'},
    'REQUEST': {'chapter_id', 'reason'},
}

NAV_SYSTEM = """你在用户提供的中文文库末端定位日文Web接续候选，只依据输入，不使用作品记忆。
中文可能只是第7至9卷，缺少前缀不要求从头翻译。目标是最后已提供主线卷之后。
两版可重排、改写、多线并进；回忆、未来预告不能代替当前进展。不要取最大匹配章号加一。
输入给出中文末端摘要和Web全书目录，目录只能召回，不能证明对应。
从Web目录选择最多3个有区分度的候选章节，用于读取正文；可以选择不同叙事线。
仅输出平面记录，每项chapter_id必须逐字使用目录中的ID。最后@@END。
@@CANDIDATE c1
chapter_id: 输入中的Web章节ID
thread: 叙事线或未知
reason: 标题序列与中文具体事件如何支持此候选，哪些仍需正文核验
@@END
示例：中文主线已经离开洞窟，但末尾回忆早期战斗，应寻找离开洞窟前后，而非按回忆倒退。
没有把握仍可提出待查候选并说明，不能编造正文已经证实的结论。"""

BOUNDARY_SYSTEM = """你核验中文文库已提供范围之后，日文Web应从哪里继续。仅依据输入正文与索引，不用作品记忆。
目标是after_latest_reference；中文可能只有7至9卷，不因缺少1至6卷就从Web开头补译。
Web是续译事实依据；文库可以重排、改写、只有部分对应。按叙事线比较具体行动、因果、结果和知情状态。
区分共同事件、简短提及、回忆和预告；共同事件不表示整章或整块全部被中文覆盖。
推荐按Web原始阅读顺序开始的整章；一章前面重复、后面有新内容时从该章开头开始。
综合各线进展，不取匹配到的最大章号+1。早期版本独有支线不自动成为本次续译起点。
若关键状态分歧无法直接承接，status=diverged；证据不够用REQUEST主动获取目录中的章。
所有引文必须逐字取自给定段落，不翻译、不省略号拼接；source_quote为日文，reference_quote为中文。
只允许以本轮passages中的段落为证据；摘要证据标签若未展开，需请求该章，不能引用未收到的正文。
chapter_id须选本轮全文提供的Web章。若想选未展开的章，输出REQUEST并status=insufficient_evidence。
输出以下平面记录，可省略没有的THREAD/REQUEST。字段可换行续写，最后独占一行@@END。
@@ANCHOR a1
source_ref: 已给出的日文段落标签
reference_ref: 已给出的中文段落标签
source_quote: 该日文段落中的短原文
reference_quote: 该中文段落中的短原文
relation: shared或partial或conflicting或uncertain
thread: 叙事线
role: current或recall或foreshadow或unknown
reason: 是否为同一具体事件及其覆盖限制
@@THREAD t1
name: 叙事线
last_anchor: a1
remaining: 还需核对的内容或无
@@BOUNDARY b1
status: suggested或ambiguous或diverged或insufficient_evidence或no_remaining_source
chapter_id: 本轮全文提供的Web章ID，无法推荐填none
end_anchor: 支持边界的ANCHOR ID，未知填none
evidence_ref: 推荐章中证明应从此开始的日文段落标签，未知填none
evidence_quote: 该段中逐字短引文，未知填none
reason: 为什么此处接续，哪些已覆盖哪些新展开
repeat_content: 开始时可能重复的内容或无
missing_context: 影响接续的缺失上下文或无；未提供前缀本身不是错误
state_compatible: yes或no或unknown
threads_checked: yes或no
@@REQUEST r1
chapter_id: 目录中的源文或参考文章ID
reason: 需要此章判断什么
@@END
格式示例（不是本书事实）：中文第9卷已完整展开离村，Web某章前半离村后半首次遇袭，
应推荐该混合章，repeat_content说明离村重复；如只有离村回忆则不能直接认定已覆盖。
若只看到相似战斗主题而无具体事件证据，应保留uncertain并请求上下文。"""


def _records(text, allowed):
    return parse_records(text, set(allowed), field_schema=FIELDS,
                         syntax_profile='p6-tolerant-v1', allow_preamble=True)


def _fold(value):
    return unicodedata.normalize('NFKC', value or '').strip()


def _quote_bound(row, quote):
    # Tolerate presentation whitespace only, never paraphrase or width-fold prose.
    return bool(quote) and ''.join(quote.split()) in ''.join(row['text'].split())


def parse_navigation(text, chapters):
    parsed = _records(text, {'CANDIDATE'})
    errors, candidates = list(parsed['errors']), []
    for record in parsed['records']:
        fields = record['fields']
        ident = _fold(fields.get('chapter_id'))
        if ident not in chapters or not fields.get('reason'):
            errors.append('unknown_candidate_or_missing_reason')
        elif ident not in [c['chapter_id'] for c in candidates]:
            candidates.append({**fields, 'chapter_id': ident})
    if not candidates:
        errors.append('nonempty_candidates_required')
    if len(candidates) > 3:
        errors.append('at_most_three_candidates')
    return {'candidates': candidates, 'errors': errors, 'complete': not errors}


def parse_boundary(text, source_refs, reference_refs, full_source_ids, directory_ids):
    parsed = _records(text, {'ANCHOR', 'BOUNDARY', 'THREAD', 'REQUEST'})
    errors, anchors, threads, requests = list(parsed['errors']), [], [], []
    boundaries = []
    for record in parsed['records']:
        f = record['fields']
        if record['record'] == 'ANCHOR':
            source_ref, reference_ref = _fold(f.get('source_ref')), _fold(f.get('reference_ref'))
            if (source_ref not in source_refs or reference_ref not in reference_refs or
                    not _quote_bound(source_refs.get(source_ref, {}), f.get('source_quote', '')) or
                    not _quote_bound(reference_refs.get(reference_ref, {}), f.get('reference_quote', ''))):
                errors.append('unbound_anchor:' + record['id'])
            relation = _fold(f.get('relation')).lower()
            if relation not in {'shared', 'partial', 'conflicting', 'uncertain'}:
                errors.append('invalid_anchor_relation')
            anchors.append({**f, 'id': record['id'], 'source_ref': source_ref,
                            'reference_ref': reference_ref, 'relation': relation})
        elif record['record'] == 'BOUNDARY':
            boundaries.append({k: _fold(v) if k in {'status', 'chapter_id', 'evidence_ref', 'end_anchor',
                              'state_compatible', 'threads_checked'} else v for k, v in f.items()})
        elif record['record'] == 'THREAD':
            threads.append(f)
        else:
            ident = _fold(f.get('chapter_id'))
            if ident not in directory_ids or not f.get('reason'):
                errors.append('request_not_in_directory')
            else:
                requests.append({**f, 'chapter_id': ident})
    if len(boundaries) != 1:
        errors.append('one_boundary_required')
    b = boundaries[0] if boundaries else {}
    if b.get('status') not in {'suggested', 'ambiguous', 'diverged', 'insufficient_evidence', 'no_remaining_source'}:
        errors.append('invalid_boundary_status')
    if not b.get('reason'):
        errors.append('boundary_reason_required')
    if b.get('chapter_id') not in {None, 'none'}:
        if b['chapter_id'] not in full_source_ids:
            errors.append('recommended_chapter_not_fully_read')
        row = source_refs.get(b.get('evidence_ref'))
        if not row or row['entry_id'] != b['chapter_id'] or not _quote_bound(row, b.get('evidence_quote', '')):
            errors.append('boundary_requires_quote_in_recommended_chapter')
    elif b.get('status') == 'suggested':
        errors.append('suggested_requires_chapter')
    ids = {a['id'] for a in anchors}
    if b.get('end_anchor') not in {None, 'none'} and b['end_anchor'] not in ids:
        errors.append('unknown_end_anchor')
    if b.get('state_compatible') not in {'yes', 'no', 'unknown'} or b.get('threads_checked') not in {'yes', 'no'}:
        errors.append('boundary_state_and_threads_required')
    return {'boundary': b, 'anchors': anchors, 'threads': threads, 'requests': requests,
            'errors': errors, 'complete': not errors}


def plan_continuation(index, *, source, references, reference_set, goal='after_latest_reference',
                      endpoint='latest_mainline', source_candidates=()):
    index._check()
    source = index.project._edition_id(source)
    references = [index.project._edition_id(r) for r in references]
    if goal != 'after_latest_reference' or endpoint != 'latest_mainline':
        raise ValueError('MVP supports after_latest_reference/latest_mainline')
    if len(references) != 1 or source in references or set(index.edition_ids) != {source, *references}:
        raise ValueError('continuation requires exactly one selected reference edition and its source')
    if not isinstance(reference_set, ReferenceSetPlan):
        raise TypeError('reference_set must be a ReferenceSetPlan')
    data = reference_set.to_dict()
    if digest({k: v for k, v in data.items() if k != 'fingerprint'}) != data['fingerprint']:
        raise ValueError('reference set fingerprint mismatch')
    if data['ordering_status'] != 'resolved' or data['edition_id'] != references[0]:
        raise ValueError('reference set has unresolved order or wrong edition')
    archive = index.archive
    if any(e['language'] != ('ja' if e['edition_id'] == source else 'zh') for e in archive.entries.values()):
        raise ValueError('continuation requires Japanese source and Chinese reference')
    actual = []
    ref_entries = sorted((e for e in archive.entries.values() if e['edition_id'] == references[0]),
                         key=lambda e: e['edition_order'])
    for entry in ref_entries:
        for row in entry.get('source_ranges', []):
            key = row['source_id'], row['source_sha256']
            if key not in actual:
                actual.append(key)
    expected = [(r['source_id'], r['content_fingerprint']) for r in data['ordered_sources']]
    if actual != expected:
        raise ValueError('reference set does not match imported source content/order')
    source_docs = archive.documents(source)
    reference_docs = _reference_documents(archive, references[0], data)
    last_source = expected[-1][0]
    endpoint_docs = [d for d in reference_docs if any(r['source_id'] == last_source for r in d['source_ranges'])]
    if not endpoint_docs:
        raise ValueError('last reference file contains no normalized narrative content')
    endpoint_doc = endpoint_docs[-1]
    nodes = sorted((n for n in index._pipeline.nodes.values() if n['edition_id'] == references[0] and n['level'] == 2),
                   key=lambda n: n['reading_range'])
    tail_nodes = [n for n in nodes if set(n['entry_ids']) & {d['entry_id'] for d in endpoint_docs}][-2:]
    source_ids = {d['entry_id'] for d in source_docs}
    candidates = list(source_candidates)
    if len(candidates) != len(set(candidates)) or any(c not in source_ids for c in candidates):
        raise ValueError('source_candidates must contain unique source chapter IDs')
    plan = {'schema': VERSION, 'index_id': index.id, 'corpus_fingerprint': archive.fingerprint,
            'source_edition': source, 'reference_editions': references, 'reference_set': data,
            'goal': goal, 'endpoint_policy': endpoint, 'reference_endpoint': {
                'chapter_id': endpoint_doc['entry_id'], 'title': endpoint_doc['raw_title'],
                'volume_ordinal': data['ordered_sources'][-1]['volume_ordinal'],
                'endpoint_kind': 'last_available_narrative', 'end_integrity': 'unknown'},
            'initial_reference_nodes': [n['node_id'] for n in tail_nodes],
            'source_candidates': candidates, 'local_only': True,
            'source_chapter_count': len(source_docs), 'reference_chapter_count': len(reference_docs)}
    plan['fingerprint'] = digest(plan)
    return plan


class Locator:
    def __init__(self, index, plan, max_new_nodes, max_requests, max_rounds):
        self.index, self.plan, self.archive = index, plan, index.archive
        # Keep request filenames within ordinary Windows path limits.
        self.folder = index.project.path / 'locate' / plan['fingerprint'][:16]
        self.folder.mkdir(parents=True, exist_ok=True)
        limits = asdict(index.project.config.limits)
        limits['max_requests'] = min(max_requests, limits['max_requests']) if limits['max_requests'] else max_requests
        self.caller = EditionCaller(index.project.provider, index.project.counter, index.project.config.capacity,
                                    self.folder / 'calls', **limits)
        self.max_new_nodes, self.max_rounds = max_new_nodes, max_rounds
        self.output = index.project.config.role_budgets()['indexer'].requested_output_tokens
        self.source = self.archive.documents(plan['source_edition'])
        self.reference = _reference_documents(self.archive, plan['reference_editions'][0], plan['reference_set'])
        self.source_map = {d['entry_id']: d for d in self.source}
        self.reference_map = {d['entry_id']: d for d in self.reference}
        self.all_docs = self.source_map | self.reference_map
        self.node_by_entry = {eid: n for n in index._pipeline.nodes.values() if n['level'] == 2 for eid in n['entry_ids']}
        self.summaries, self.read_ids, self.reference_read_ids, self.steps = {}, set(), set(), []
        self.last_decision = None
        write_json(self.folder / 'plan.json', plan)

    def _summary(self, node_id):
        if node_id in self.summaries:
            return self.summaries[node_id]
        path = self.folder / 'summaries' / (node_id + '.json')
        node = self.index._pipeline.nodes[node_id]
        existing = self.index._pipeline.read_summary(node_id) if set(node['entry_ids']) <= set(self.all_docs) else None
        if existing:
            artifact = existing
        elif path.exists():
            artifact = read_json(path)
        else:
            if len(list((self.folder / 'summaries').glob('*.json'))) >= self.max_new_nodes:
                raise SearchLimit('node_limit')
            docs = [self.all_docs[eid] for eid in node['entry_ids'] if eid in self.all_docs]
            messages = summary_messages(node, docs)
            messages[-1]['content'] += ('\n本次仅用于续译边界导航：省略ENTITY记录，概要软目标1200字；'
                '提取6至12个有区分度的事件，覆盖各叙事线和本块末端，明确回忆/预告。'
                'TEMPORAL和KNOWLEDGE各最多3项。无需穷举技能或人物，尽快给出规定记录。')
            # Independent extraction: no hidden/out-of-scope predecessor cache.
            outcome = self.caller.structured(VERSION + ':summary:' + node_id,
                messages, self.output,
                lambda text: parse_summary(text, node, self.archive.refs([d['entry_id'] for d in docs])))
            if outcome['status'] != 'complete':
                self.steps.append({'stage': 'summary', 'node_id': node_id, **outcome})
                raise SearchLimit('summary_failed')
            artifact = {'node': node, 'content': outcome['result'], 'generation_id': outcome['generation_id'],
                        'dependencies': node['dependencies'], 'predecessor_dependencies': [],
                        'covered_entry_ids': [d['entry_id'] for d in docs],
                        'summary_profile': 'continuation-events-v1',
                        'fingerprint': digest(outcome['result'])}
            write_json(path, artifact)
        if artifact['node'] != self.index._pipeline.nodes[node_id] or artifact['fingerprint'] != digest(artifact['content']):
            raise ValueError('stale continuation summary')
        self.archive.validate_dependencies(artifact['dependencies'])
        self.summaries[node_id] = artifact
        return artifact

    def _directory(self, docs):
        return [{'chapter_id': d['entry_id'], 'title': d['raw_title'], 'reading_order': d['edition_order'],
                 'volume': d.get('metadata', {}).get('volume_label')} for d in docs]

    def _compact(self, artifact, max_events=8):
        s = artifact['content']
        events = s['events'] if len(s['events']) <= max_events else s['events'][:max_events//2] + s['events'][-(max_events-max_events//2):]
        return {'node_id': s['node_id'], 'overview': s['overview'][:3000], 'events': events,
                'temporal_claims': s['temporal_claims'][:6], 'knowledge_claims': s['knowledge_claims'][:4],
                'navigation_omissions': len(events) != len(s['events']) or len(s['overview']) > 3000,
                'coverage': s['coverage'], 'navigation_only': True}

    def _passages(self, summaries, radius, max_events=8):
        selected = {}
        for artifact in summaries:
            for event in self._compact(artifact, max_events)['events']:
                ref = event.get('evidence_ref')
                for eid in artifact['node']['entry_ids']:
                    if eid not in self.all_docs:
                        continue
                    rows = self.all_docs[eid]['rows']
                    match = next((i for i, r in enumerate(rows) if r['ref'] == ref), None)
                    if match is not None:
                        for row in rows[max(0, match-radius):match+radius+1]:
                            selected[row['ref']] = row
        return list(selected.values())

    def _fit_messages(self, payload):
        messages = [{'role': 'system', 'content': BOUNDARY_SYSTEM}, {'role': 'user', 'content': j(payload)}]
        report = plan_request(messages, self.index.project.config.capacity, RoleBudget(self.output), self.index.project.counter)
        return messages if report['fits'] else None

    def _group_passages(self, rows):
        groups = {}
        for row in rows.values():
            groups.setdefault(row['entry_id'], []).append(row)
        return [{'chapter_id': eid, 'title': self.all_docs[eid]['raw_title'],
                 'paragraphs': [{'ref': r['ref'], 'text': r['text']} for r in sorted(items, key=lambda r: r['ordinal'])]}
                for eid, items in sorted(groups.items(), key=lambda pair: self.all_docs[pair[0]]['edition_order'])]

    def _repair_truncated(self, messages, source_refs, reference_refs, selected_ids, round_number):
        candidates = []
        prefix = VERSION + ':boundary:' + str(round_number) + ':attempt:'
        for path in self.caller.folder.glob('*.failure.json'):
            saved = read_json(path)
            identity = saved['identity']
            partial = saved.get('details', {}).get('text', '')
            if (saved['kind'] == 'truncated' and identity['task'].startswith(prefix)
                    and identity['provider'] == self.caller.provider_identity
                    and identity['request']['max_tokens'] == self.output
                    and identity['request']['messages'][:len(messages)] == messages):
                if digest(identity) != path.name.removesuffix('.failure.json'):
                    raise ValueError('saved truncated request integrity mismatch')
                candidates.append((len(partial), partial, path.name))
        if not candidates:
            return None
        _, partial, origin = max(candidates)
        records = _records(partial, {'ANCHOR', 'BOUNDARY', 'THREAD', 'REQUEST'})
        proposed = next((r['fields'].get('chapter_id') for r in records['records'] if r['record'] == 'BOUNDARY'), None)
        focused_ids = [proposed] if proposed in selected_ids else list(selected_ids[:1])
        if not focused_ids:
            return None
        source_rows = {r['ref']: r for r in source_refs.values() if r['ref'] in partial}
        for eid in focused_ids:
            source_rows.update({r['ref']: r for r in self.source_map[eid]['rows']})
        reference_rows = {r['ref']: r for r in reference_refs.values() if r['ref'] in partial}
        endpoint = self.reference_map[self.plan['reference_endpoint']['chapter_id']]
        reference_rows.update({r['ref']: r for r in endpoint['rows']})
        relevant = [a for a in self.summaries.values() if a['node']['language'] == 'zh'
                    or set(a['node']['entry_ids']) & set(focused_ids)]
        if not partial:
            reference_rows.update({r['ref']: r for r in self._passages(
                [a for a in relevant if a['node']['language'] == 'zh'], radius=1, max_events=4)})
        nearby = set(focused_ids)
        for position, doc in enumerate(self.source):
            if doc['entry_id'] in focused_ids:
                nearby.update(d['entry_id'] for d in self.source[max(0, position-2):position+3])
        payload = {'goal': self.plan['goal'], 'reference_endpoint': self.plan['reference_endpoint'],
            'reference_availability': self.plan['reference_set']['availability'],
            'previous_truncated_output_candidate_only': partial,
            'source_directory': self._directory([d for d in self.source if d['entry_id'] in nearby]),
            'reference_directory': self._directory(self.reference),
            'navigation_overviews': [{'node_id': a['node']['node_id'], 'overview': a['content']['overview'][:3000],
                'navigation_only': True} for a in relevant] if not partial else [],
            'fully_read_source_chapters': [{'chapter_id': eid, 'title': self.source_map[eid]['raw_title']} for eid in focused_ids],
            'passages': {'source': self._group_passages(source_rows), 'reference': self._group_passages(reference_rows)},
            'instruction': '上次截断输出不是已确认事实。核对给出的短引文与候选章，补全一份完整平面结果。'
                '若上次没有可见输出，则仅核对本次这个候选与中文末端能否承接，允许否定此候选或REQUEST查邻章。'
                '最多2条ANCHOR、1条BOUNDARY、1条REQUEST；不要重新穷举全书、额外叙事线或人物。'
                '不能从这些局部证据确认的state_compatible填unknown、threads_checked填no。'
                '不得为格式补全编造引文或把partial改为shared。尽快结束完整记录。'}
        repair_messages = self._fit_messages(payload)
        if repair_messages is None:
            return None
        self.last_packet_full_ids = set(focused_ids)
        self.reference_read_ids.add(endpoint['entry_id'])
        outcome = self.caller.structured(VERSION + ':boundary-repair:' + str(round_number), repair_messages, self.output,
            lambda text: parse_boundary(text, source_rows, reference_rows, set(focused_ids), set(self.all_docs)))
        return {**outcome, 'truncated_origin': origin, 'repair_scope': 'bound_passages_and_candidate_chapter',
                'visible_truncated_output': bool(partial)}

    def _assess(self, *, local_ids=(), previous=None, round_number=0):
        artifacts = list(self.summaries.values())
        refs = [a for a in artifacts if a['node']['language'] == 'zh']
        sources = [a for a in artifacts if a['node']['language'] == 'ja']
        choices = [(list(local_ids), r, e) for r, e in ((1, 8), (0, 8), (0, 4))]
        if len(local_ids) > 1:
            choices += [(list(local_ids[:1]), 0, 4)]
        for selected_ids, radius, event_limit in choices:
            source_passages = self._passages(sources, radius, event_limit)
            reference_passages = self._passages(refs, radius, event_limit)
            # Read the actual end of the selected reference, not just event samples.
            end = self.reference_map[self.plan['reference_endpoint']['chapter_id']]
            reference_passages += end['rows'][-12:]
            reference_passages += [r for eid in sorted(self.reference_read_ids) for r in self.reference_map[eid]['rows']]
            full = [self.source_map[eid] for eid in selected_ids]
            source_passages += [row for d in full for row in d['rows']]
            for doc in full:
                position = next(i for i, d in enumerate(self.source) if d['entry_id'] == doc['entry_id'])
                if position:
                    source_passages += self.source[position-1]['rows'][-4:]
                if position+1 < len(self.source):
                    source_passages += self.source[position+1]['rows'][:4]
            source_refs = {r['ref']: r for r in source_passages}
            reference_refs = {r['ref']: r for r in reference_passages}
            candidate_ids = {eid for a in sources for eid in a['node']['entry_ids']}
            nearby = set()
            for position, doc in enumerate(self.source):
                if doc['entry_id'] in candidate_ids:
                    nearby.update(d['entry_id'] for d in self.source[max(0, position-3):position+5])
            payload = {'goal': self.plan['goal'], 'reference_availability': self.plan['reference_set']['availability'],
                'reference_endpoint': self.plan['reference_endpoint'],
                'source_directory': self._directory([d for d in self.source if d['entry_id'] in nearby]),
                'reference_directory': self._directory(self.reference),
                'summaries': [self._compact(a, event_limit) for a in artifacts],
                'context_selection': {'passage_radius': radius, 'events_per_summary': event_limit,
                                      'all_summaries_are_navigation_only': True},
                'passages': {'source': self._group_passages(source_refs), 'reference': self._group_passages(reference_refs)},
                'fully_read_source_chapters': [{'chapter_id': d['entry_id'], 'title': d['raw_title']} for d in full],
                'previous_decision_navigation_only': previous, 'search_complete': False,
                'instruction': '初步选择后请REQUEST推荐章及前后章用于全文核验。' if not local_ids else '已展开候选局部全文，请核验接续边界。'}
            messages = self._fit_messages(payload)
            if messages:
                self.read_ids.update(selected_ids)
                self.last_packet_full_ids = set(selected_ids)
                outcome = self.caller.structured(VERSION + ':boundary:' + str(round_number), messages, self.output,
                    lambda text: parse_boundary(text, source_refs, reference_refs, set(selected_ids), set(self.all_docs)))
                self.steps.append({'stage': 'boundary', 'round': round_number, **outcome})
                if outcome['status'] != 'complete':
                    repaired = self._repair_truncated(messages, source_refs, reference_refs, selected_ids, round_number)
                    if repaired:
                        self.steps.append({'stage': 'boundary', 'round': round_number, **repaired})
                        outcome = repaired
                if outcome['status'] != 'complete':
                    raise SearchLimit('boundary_failed')
                return outcome['result']
        raise SearchLimit('context_limit')

    def run(self):
        for nid in self.plan['initial_reference_nodes']:
            self._summary(nid)
        centers = self.plan['source_candidates']
        if not centers:
            payload = {'reference_endpoint': self.plan['reference_endpoint'],
                'availability': self.plan['reference_set']['availability'],
                'reference_summaries': [self._compact(a, 20) for a in self.summaries.values()],
                'source_directory': self._directory(self.source)}
            outcome = self.caller.structured(VERSION + ':navigation',
                [{'role': 'system', 'content': NAV_SYSTEM}, {'role': 'user', 'content': j(payload)}], self.output,
                lambda text: parse_navigation(text, self.source_map))
            self.steps.append({'stage': 'navigation', **outcome})
            if outcome['status'] != 'complete':
                raise SearchLimit('navigation_failed')
            centers = [c['chapter_id'] for c in outcome['result']['candidates']]
        for eid in centers:
            self._summary(self.node_by_entry[eid]['node_id'])
        # Initial local windows give the model actual complete candidate chapters.
        local_ids = list(centers)
        previous = None
        for round_number in range(self.max_rounds):
            decision = self._assess(local_ids=local_ids, previous=previous, round_number=round_number)
            self.last_decision = decision
            if decision['requests']:
                progressed = False
                next_source_ids = []
                for request in decision['requests']:
                    eid = request['chapter_id']
                    progressed |= self.node_by_entry[eid]['node_id'] not in self.summaries
                    self._summary(self.node_by_entry[eid]['node_id'])
                    if eid in self.source_map:
                        next_source_ids.append(eid)
                        progressed |= eid not in self.last_packet_full_ids
                    elif eid in self.reference_map and eid not in self.reference_read_ids:
                        self.reference_read_ids.add(eid)
                        progressed = True
                if not progressed:
                    return self._result(decision, limit_reason='repeated_lookup')
                if next_source_ids:
                    local_ids = list(dict.fromkeys(next_source_ids))
                previous = decision
                continue
            return self._result(decision)
        return self._result(decision, limit_reason='round_limit')

    def _result(self, decision=None, limit_reason=None):
        decision = (self.last_decision if decision is None else decision) or {}
        boundary = decision.get('boundary', {})
        ident = boundary.get('chapter_id')
        recommendation = None
        if ident in self.source_map and ident in self.read_ids:
            d = self.source_map[ident]
            recommendation = {'chapter_id': ident, 'title': d['raw_title'], 'edition_order': d['edition_order'],
                              'position': next(i for i, s in enumerate(self.source) if s['entry_id'] == ident)}
        anchors = decision.get('anchors', [])
        shared = [a for a in anchors if a['relation'] == 'shared' and a.get('role') == 'current']
        supported = len({a['source_ref'] for a in shared}) >= 2 and len({a['reference_ref'] for a in shared}) >= 2
        repaired_locally = any(s.get('truncated_origin') and s['status'] == 'complete' for s in self.steps)
        auto = bool(not repaired_locally and not limit_reason and recommendation and supported and boundary.get('status') == 'suggested'
                    and boundary.get('state_compatible') == 'yes' and boundary.get('threads_checked') == 'yes'
                    and boundary.get('missing_context') in {'无', 'none', '无。'}
                    and not any(a['relation'] == 'conflicting' for a in anchors))
        status = 'insufficient_evidence' if limit_reason else boundary.get('status', 'insufficient_evidence')
        if status == 'no_remaining_source' and self.source[-1]['entry_id'] not in self.read_ids:
            status, limit_reason, auto = 'insufficient_evidence', 'source_end_not_read', False
        result = {'schema': VERSION, 'status': status,
            'goal': self.plan['goal'], 'reference_set_fingerprint': self.plan['reference_set']['fingerprint'],
            'reference_availability': self.plan['reference_set']['availability'],
            'reference_endpoint': self.plan['reference_endpoint'],
            'recommended_start': recommendation if status == 'suggested' else None,
            'candidate_start': recommendation,
            'last_shared_anchors': anchors, 'thread_coverage': decision.get('threads', []),
            'boundary': boundary, 'auto_eligible': auto,
            'evidence_level': 'passage_checked' if anchors else 'summary_only',
            'semantic_status': 'model_suggested_unreviewed', 'limit_reason': limit_reason,
            'search_coverage': {'search_complete': False, 'recall_methods': ['global_title_directory', 'L2_events', 'local_passages'],
                'summarized_nodes': list(self.summaries), 'fully_read_source_ids': sorted(self.read_ids),
                'fully_read_reference_ids': sorted(self.reference_read_ids),
                'remaining_requests': decision.get('requests', [])},
            'summary_fingerprints': {nid: a['fingerprint'] for nid, a in self.summaries.items()},
            'plan_fingerprint': self.plan['fingerprint'], 'steps': self.steps, 'artifact_dir': str(self.folder)}
        result['fingerprint'] = digest(result)
        write_json(self.folder / 'result.json', result)
        return result


class SearchLimit(Exception):
    pass


def locate_continuation(index, plan, *, max_new_nodes=20, max_requests=30, max_rounds=4):
    index._check()
    if any(type(v) is not int or v <= 0 for v in (max_new_nodes, max_requests, max_rounds)):
        raise ValueError('continuation limits must be positive integers')
    if (plan.get('index_id') != index.id or plan.get('corpus_fingerprint') != index.archive.fingerprint or
            digest({k: v for k, v in plan.items() if k != 'fingerprint'}) != plan.get('fingerprint')):
        raise ValueError('stale or modified continuation plan')
    index.project._require_generation()
    locator = Locator(index, plan, max_new_nodes, max_requests, max_rounds)
    try:
        return locator.run()
    except SearchLimit as exc:
        return locator._result(limit_reason=str(exc))
    except (ProviderError, NarrativeError) as exc:
        return locator._result(limit_reason=exc.kind)
