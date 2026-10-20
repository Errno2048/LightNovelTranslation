"""Model-judged unresolved-thread reconciliation at sealed L2 boundaries."""
from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter

from ..budget import plan_request
from ..narrative.schemas import NarrativeError
from ..storage import digest
from .workset import _canonical, _record_text, _short

THREAD_STATUSES = {'active', 'partially_resolved', 'resolved', 'superseded',
                   'ambiguous', 'dormant', 'archived', 'merged'}
ACTIVE_STATUSES = {'active', 'partially_resolved', 'ambiguous'}
DEMOTED_STATUSES = {'resolved', 'superseded', 'archived', 'merged'}
DORMANT_STATUSES = {'dormant'}
CORE_ENTITY_LIMIT = 50
MERGE_SIMILARITY = 0.62
ACTIVE_SCORE = 2.0
KATAKANA = re.compile(r'[ァ-ヶー]{2,}')


def _thread_id(fields):
    return _short('T', {'fields': fields})


def _seed_key(fields):
    return _canonical(fields)


def latest_thread_catalog(repo, *, before_order=None):
    rows = [(ident, value) for ident, value in repo.store.objects('sequence-thread-catalog')
            if value.get('run_id') == repo.run_id]
    if before_order is not None:
        rows = [(ident, value) for ident, value in rows
                if value.get('visibility_order', -1) <= before_order]
    return max(rows, key=lambda row: (row[1].get('visibility_order', -1),
                                      row[1].get('revision', 0)), default=None)


def _new_catalog(repo, threads, reconciled, visibility_order, visible_source_ids,
                 reconciled_l1=()):
    previous = latest_thread_catalog(repo)
    revision = (previous[1].get('revision', 0) + 1) if previous else 1
    return {'schema': 'sequence-thread-catalog-v1', 'run_id': repo.run_id,
            'revision': revision, 'threads': threads,
            'reconciled_nodes': sorted(reconciled),
            'reconciled_l1_nodes': sorted(reconciled_l1),
            'visibility_order': visibility_order,
            'visible_source_ids': sorted(visible_source_ids)}


def _save_catalog(repo, catalog):
    ident = repo.store.put('sequence-thread-catalog', catalog)
    return ident, catalog


def _thread_from_need(repo, node, record):
    fields = record.get('fields', {})
    question = ((fields.get('description') or [''])[0])[:600]
    subject = [str(value) for value in
               (fields.get('subject') or fields.get('participants') or []) if str(value).strip()]
    return {
        'thread_id': _thread_id(fields),
        'seed_key': _seed_key(fields),
        'status': 'active',
        'question': question,
        'subject': subject[:8],
        'entity_names': [],
        'score': 0.0,
        'filter_reason': None,
        'recurrence': 1,
        'source_ids': list(node.get('source_ids', [])),
        'first_seen': node.get('start_position', -1),
        'last_seen': node.get('end_position', -1),
        'evidence': [],
        'resolution': '',
        'resolved_in': None,
        'revision': 0,
        'seed': {'node_revision': node.get('id'), 'record_id': record.get('id')},
        'seed_records': [{'node_revision': node.get('id'), 'record_id': record.get('id')}],
    }


def _thread_names(thread):
    text = ' '.join([thread.get('question', '')] + list(thread.get('subject', [])))
    return sorted(set(KATAKANA.findall(text)) |
                  {value for value in thread.get('subject', []) if len(value) >= 2})


def _bigrams(text):
    text = unicodedata.normalize('NFKC', text or '')
    return {text[index:index + 2] for index in range(len(text) - 1)}


def _similarity(left, right):
    a, b = _bigrams(left), _bigrams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _core_names(nodes):
    counts = Counter()
    for node in nodes:
        for record in node.get('records', []):
            if record.get('type') != 'ENTITY':
                continue
            fields = record.get('fields', {})
            for key in ('name', 'target_form'):
                for value in fields.get(key, []) or []:
                    value = str(value).strip()
                    if len(value) >= 2:
                        counts[value] += 1
    return {name for name, _ in counts.most_common(CORE_ENTITY_LIMIT)}


def _node_text(node):
    return (node.get('summary') or '') + ' ' + ' '.join(
        _record_text(record) for record in node.get('records', []))


def _add_threads(threads, node, core_names):
    node_text = _node_text(node)
    for thread in threads.values():
        thread.setdefault('entity_names', _thread_names(thread))
        if (thread.get('status') == 'dormant' and
                any(name in node_text for name in thread['entity_names'])):
            thread['status'] = 'active'
            thread['filter_reason'] = 'reactivated'
            thread['reactivated_in'] = node.get('end_position')
    for record in node.get('records', []):
        if record.get('type') != 'NEED':
            continue
        created = _thread_from_need(None, node, record)
        names = set(_thread_names(created))
        names.update(name for name in core_names if name in created['question'])
        created['entity_names'] = sorted(names)
        merged = False
        if names:
            for existing in threads.values():
                if existing.get('status') in {'merged', 'archived'}:
                    continue
                existing_names = set(existing.get('entity_names') or _thread_names(existing))
                if not names & existing_names:
                    continue
                if _similarity(created['question'], existing.get('question', '')) < MERGE_SIMILARITY:
                    continue
                existing['source_ids'] = sorted(set(existing.get('source_ids', [])) |
                                                set(created.get('source_ids', [])))
                existing['seed_records'] = list(existing.get('seed_records', [])) + \
                    created.get('seed_records', [])
                existing['last_seen'] = max(existing.get('last_seen', -1), created['last_seen'])
                existing['recurrence'] = existing.get('recurrence', 1) + 1
                existing['revision'] = existing.get('revision', 0) + 1
                merged = True
                break
        if merged:
            continue
        core_hits = len(names & core_names)
        recency = 1.0 if created['last_seen'] >= node.get('end_position', -1) - 25 else 0.5
        generic = 2.0 if not names else 0.0
        created['score'] = 3.0 * core_hits + 1.5 + recency - generic
        if core_hits or created['score'] >= ACTIVE_SCORE:
            created['status'] = 'active'
        else:
            created['status'] = 'dormant'
            created['filter_reason'] = 'low_salience'
        current = threads.get(created['thread_id'])
        if current is None:
            threads[created['thread_id']] = created
        else:
            current['last_seen'] = max(current.get('last_seen', -1), created['last_seen'])
            current['source_ids'] = sorted(set(current.get('source_ids', [])) |
                                           set(created.get('source_ids', [])))
            current['seed_records'] = list(current.get('seed_records', [])) + \
                created.get('seed_records', [])
            current['recurrence'] = current.get('recurrence', 1) + 1


def _pinned_threads(threads):
    return [thread for thread in threads.values() if thread.get('status') in ACTIVE_STATUSES]


def _active_threads(threads):
    return _pinned_threads(threads)


def _batch_threads(threads):
    return [thread for thread in threads.values()
            if thread.get('status') in ACTIVE_STATUSES | DORMANT_STATUSES]


def _relevant_records(node, active):
    names = set()
    for thread in active:
        names.update(_thread_names(thread))
    selected = []
    for record in node.get('records', []):
        if record.get('type') in {'SUMMARY', 'COVERAGE'}:
            continue
        text = _record_text(record)
        if not names or any(name in text for name in names):
            selected.append(record)
    return selected[:40]


def _compact_record(record):
    fields = record.get('fields', {})
    projected = {}
    for key, limit in (('description', 1), ('ja', 8), ('zh', 8),
                       ('subject', 8), ('participants', 8),
                       ('attitude', 2), ('relation', 2), ('story_time', 2)):
        values = [str(value)[:240] for value in fields.get(key, []) or []
                  if isinstance(value, str) and value]
        if values:
            projected[key] = values[:limit]
    return {'type': record.get('type'), 'fields': projected}


def _allowed_refs(node, records):
    refs = set()
    for record in records:
        fields = record.get('fields', {})
        for key in ('ja', 'zh'):
            refs.update(str(value) for value in fields.get(key, []) or [])
    return sorted(refs)[:500]


def _relevant_threads(active, node):
    text = (node.get('summary') or '') + ' ' + ' '.join(
        _record_text(record) for record in node.get('records', []))
    result = []
    for thread in active:
        names = _thread_names(thread)
        if names and any(name in text for name in names):
            result.append(thread)
        elif not names and len(active) <= 10:
            result.append(thread)
    return result[:40]


def _messages(active, node, records, allowed_refs):
    cards = [{'thread_id': thread['thread_id'], 'question': thread['question'],
              'subject': thread['subject'], 'status': thread.get('status', 'active')}
             for thread in active]
    payload = {'active_threads': cards,
               'node': {'node_id': node.get('node_id'),
                        'summary': (node.get('summary') or '')[:2500],
                        'records': [_compact_record(record) for record in records]},
               'allowed_evidence_refs': allowed_refs}
    return [
        {'role': 'system', 'content':
         '你维护同一作品中的未决问题。只根据给定节点摘要、记录和允许的证据引用判断；'
         '没有直接证据时必须保持 active 或 ambiguous。不得创造引用，不得把推测写成事实。'
         '只输出一个 JSON 对象：{"threads":[{"thread_id":"...","status":'
         '"active|partially_resolved|resolved|superseded|ambiguous",'
         '"resolution":"...","evidence":["C....J..."]}]}。未改变的线程可以省略。'},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
    ]


def _decode_json(text):
    text = unicodedata.normalize('NFKC', text).strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None


def _parse(text, active, allowed_refs):
    value = _decode_json(text)
    if not isinstance(value, dict) or not isinstance(value.get('threads'), list):
        return None
    allowed_ids = {thread['thread_id'] for thread in active}
    allowed_refs = set(allowed_refs)
    result = []
    for row in value['threads']:
        if not isinstance(row, dict):
            continue
        thread_id = row.get('thread_id')
        status = row.get('status')
        if thread_id not in allowed_ids or status not in THREAD_STATUSES:
            continue
        evidence = row.get('evidence', [])
        if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
            continue
        evidence = [ref for ref in evidence if ref in allowed_refs]
        resolution = row.get('resolution', '')
        if not isinstance(resolution, str):
            resolution = ''
        if status in {'resolved', 'partially_resolved', 'superseded'} and not evidence:
            continue
        if status in {'resolved', 'partially_resolved', 'superseded'} and not resolution.strip():
            continue
        result.append({'thread_id': thread_id, 'status': status,
                       'resolution': resolution.strip(), 'evidence': evidence})
    return result


def _reconcile_node(repo, spec, view, caller, node, active):
    records = _relevant_records(node, active)
    allowed = _allowed_refs(node, records)
    messages = _messages(active, node, records, allowed)
    report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
    while not report['fits'] and len(records) > 8:
        records = records[:max(8, len(records) // 2)]
        allowed = _allowed_refs(node, records)
        messages = _messages(active, node, records, allowed)
        report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
    if not report['fits']:
        return {'status': 'skipped', 'reason': 'thread_reconcile_context_exceeded'}, None
    raw = caller.call('thread-reconcile:' + node.get('id', node.get('node_id', '')),
                      'summary', messages, role='indexer',
                      max_tokens=min(8192, spec.roles['indexer'].requested_output_tokens))
    text = raw['generation']['text']
    parsed = _parse(text, active, allowed)
    if parsed is None:
        return {'status': 'invalid', 'generation_id': raw['id'],
                'raw_text': text[:4000]}, raw
    return {'status': 'completed', 'generation_id': raw['id'],
            'entries': parsed, 'record_count': len(records)}, raw


def _filter_threads(threads, core_names, visibility):
    """Deterministic L1-side merge and dormant demotion before any model call."""
    for thread in threads.values():
        if thread.get('status') in DEMOTED_STATUSES:
            continue
        names = set(thread.get('entity_names') or _thread_names(thread))
        thread['entity_names'] = sorted(names)
        core_hits = len(names & core_names)
        recurrence = thread.get('recurrence', 1)
        recency = 1.0 if thread.get('last_seen', -1) >= visibility - 25 else 0.5
        generic = 2.0 if not names else 0.0
        thread['score'] = 3.0 * core_hits + 1.5 * min(recurrence, 3) + recency - generic
        if (thread.get('status') == 'active' and not core_hits and
                recurrence < 2 and thread['score'] < 3.0):
            thread['status'] = 'dormant'
            thread['filter_reason'] = 'low_salience'
    active = sorted((thread for thread in threads.values()
                     if thread.get('status') in ACTIVE_STATUSES | DORMANT_STATUSES),
                    key=lambda thread: (thread.get('first_seen', -1),
                                        thread.get('thread_id')))
    merged = 0
    for index, left in enumerate(active):
        if left.get('status') in DEMOTED_STATUSES:
            continue
        left_names = set(left.get('entity_names') or [])
        if not left_names:
            continue
        for right in active[index + 1:]:
            if right.get('status') in DEMOTED_STATUSES:
                continue
            right_names = set(right.get('entity_names') or [])
            if not left_names & right_names:
                continue
            if _similarity(left.get('question', ''), right.get('question', '')) < MERGE_SIMILARITY:
                continue
            left['source_ids'] = sorted(set(left.get('source_ids', [])) |
                                        set(right.get('source_ids', [])))
            left['seed_records'] = list(left.get('seed_records', [])) + \
                list(right.get('seed_records', []))
            left['recurrence'] = left.get('recurrence', 1) + right.get('recurrence', 1)
            left['evidence'] = sorted(set(left.get('evidence', [])) |
                                      set(right.get('evidence', [])))
            left['revision'] = left.get('revision', 0) + 1
            right['status'] = 'merged'
            right['merged_into'] = left['thread_id']
            right['revision'] = right.get('revision', 0) + 1
            merged += 1
    return {'dormant': len([thread for thread in threads.values()
                            if thread.get('status') in DORMANT_STATUSES]),
            'merged': merged}


def _sealed_l1_nodes(repo, spec, view, nodes):
    from .hierarchy import plan_hierarchy
    from .knowledge import current_index
    saved = {node.get('node_id'): node for node in nodes}
    plans = []
    try:
        plans.append(plan_hierarchy(repo, spec, view))
    except Exception:
        pass
    if repo.all_commits():
        try:
            plans.append(current_index(repo, spec, view))
        except Exception:
            pass
    result = {}
    for plan in plans:
        for node in plan.get('nodes', []):
            if node.get('level') != 1:
                continue
            saved_node = saved.get(node.get('node_id'))
            if not saved_node:
                continue
            node_id = str(node.get('node_id'))
            if node_id.startswith('hierarchy-parent:'):
                if saved_node.get('end_position', -1) < view.target_order:
                    result[saved_node['id']] = saved_node
            elif node.get('state') == 'sealed':
                result[saved_node['id']] = saved_node
    return sorted(result.values(), key=lambda node: (node.get('start_position', -1),
                                                     node.get('end_position', -1)))


def _l1_children(l1, nodes):
    return sorted((node for node in nodes
                   if node.get('level') == 2 and node.get('parent_id') == l1.get('node_id')),
                  key=lambda node: (node.get('start_position', -1), node.get('end_position', -1)))


def _l1_candidates(threads, l1, text):
    items = []
    for thread in threads.values():
        status = thread.get('status')
        if status not in ACTIVE_STATUSES | DORMANT_STATUSES:
            continue
        if thread.get('last_seen', -1) > l1.get('end_position', -1):
            continue
        names = set(thread.get('entity_names') or _thread_names(thread))
        relevant = any(name in text for name in names) if names else False
        if status == 'dormant' and not relevant:
            continue
        if status in ACTIVE_STATUSES and not relevant and \
                thread.get('last_seen', -1) < l1.get('end_position', -1) - 50:
            continue
        items.append({'thread': thread, 'card': {
            'thread_id': thread['thread_id'], 'status': status,
            'question': (thread.get('question') or '')[:240],
            'subject': (thread.get('subject') or [])[:6],
            'score': thread.get('score', 0), 'last_seen': thread.get('last_seen'),
            'evidence_count': len(thread.get('evidence') or [])}})
    items.sort(key=lambda item: (item['thread'].get('status') not in ACTIVE_STATUSES,
                                 -item['thread'].get('score', 0),
                                 -item['thread'].get('last_seen', -1)))
    return items[:80]


def _l1_context(l1, children, names):
    summaries = [{'node_id': child.get('node_id'), 'summary': child.get('summary', '')}
                 for child in children if child.get('summary')][:12]
    records = []
    for child in children:
        for record in child.get('records', []):
            if record.get('type') in {'SUMMARY', 'COVERAGE'}:
                continue
            text = _record_text(record)
            if not names or any(name in text for name in names):
                records.append(record)
    return summaries, records[:40]


def _l1_messages(l1, summaries, records, allowed, cards):
    payload = {
        'l1': {'node_id': l1.get('node_id'), 'summary': (l1.get('summary') or '')[:3000]},
        'l2_summaries': summaries,
        'records': [_compact_record(record) for record in records],
        'threads': cards,
        'allowed_evidence_refs': allowed,
    }
    return [
        {'role': 'system', 'content':
         '你在 L1 索引封口时批量整理未决问题。只根据给定 L1/L2 摘要、记录和允许的证据引用判断；'
         '没有直接证据时必须保持 active 或 dormant。可以合并语义相同的问题，但不能合并不同人物、不同时间或不同结论。'
         '对于不涉及核心人物/当前 L1 主线、与其他线程重复、或只是本段临时推测的问题，action 用 dormant；'
         '不要只返回 resolved/partially_resolved。'
         '只输出一个 JSON 对象：{"threads":[{"thread_id":"...","action":'
         '"resolved|partially_resolved|active|dormant|ambiguous|merge",'
         '"merge_into":"T...","resolution":"...","evidence":["C....J..."]}]}。'
         '未改变的线程可以省略。resolved/partially_resolved 必须有证据和说明。'},
        {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, separators=(',', ':'))},
    ]


def _parse_l1(text, cards, allowed):
    value = _decode_json(text)
    if not isinstance(value, dict) or not isinstance(value.get('threads'), list):
        return None
    allowed_ids = {item['card']['thread_id'] for item in cards}
    allowed_refs = set(allowed)
    result = []
    for row in value['threads']:
        if not isinstance(row, dict):
            continue
        thread_id = row.get('thread_id')
        action = row.get('action') or row.get('status')
        if thread_id not in allowed_ids or action not in THREAD_STATUSES | {'merge'}:
            continue
        if action == 'merge':
            target = row.get('merge_into')
            if target not in allowed_ids or target == thread_id:
                continue
            result.append({'thread_id': thread_id, 'action': 'merge', 'merge_into': target})
            continue
        evidence = row.get('evidence', [])
        if not isinstance(evidence, list) or any(not isinstance(ref, str) for ref in evidence):
            continue
        evidence = [ref for ref in evidence if ref in allowed_refs]
        resolution = row.get('resolution', '')
        if not isinstance(resolution, str):
            resolution = ''
        if action in {'resolved', 'partially_resolved'} and (not evidence or not resolution.strip()):
            continue
        result.append({'thread_id': thread_id, 'action': action,
                       'resolution': resolution.strip(), 'evidence': evidence})
    return result


def _apply_l1(threads, entries, l1):
    updated = 0
    for entry in entries:
        thread = threads.get(entry['thread_id'])
        if not thread:
            continue
        if entry['action'] == 'merge':
            target = threads.get(entry.get('merge_into'))
            if not target:
                continue
            target['source_ids'] = sorted(set(target.get('source_ids', [])) |
                                          set(thread.get('source_ids', [])))
            target['seed_records'] = list(target.get('seed_records', [])) + \
                list(thread.get('seed_records', []))
            target['evidence'] = sorted(set(target.get('evidence', [])) |
                                        set(thread.get('evidence', [])))
            target['revision'] = target.get('revision', 0) + 1
            thread['status'] = 'merged'
            thread['merged_into'] = entry['merge_into']
            thread['revision'] = thread.get('revision', 0) + 1
            updated += 1
            continue
        thread['status'] = entry['action']
        if entry.get('resolution'):
            thread['resolution'] = entry['resolution']
        if entry.get('evidence'):
            thread['evidence'] = entry['evidence']
        if entry['action'] in {'resolved', 'partially_resolved'}:
            thread['resolved_in'] = [l1.get('start_position'), l1.get('end_position')]
        thread['revision'] = thread.get('revision', 0) + 1
        updated += 1
    return updated


def _reconcile_l1(repo, spec, view, caller, l1, nodes, threads):
    children = _l1_children(l1, nodes)
    summary_text = ' '.join([l1.get('summary') or ''] +
                            [child.get('summary') or '' for child in children])
    candidates = _l1_candidates(threads, l1, summary_text)
    if not candidates:
        return {'status': 'skipped', 'reason': 'no_candidates'}
    names = set()
    for item in candidates:
        names.update(item['thread'].get('entity_names') or _thread_names(item['thread']))
    summaries, records = _l1_context(l1, children, names)
    allowed = _allowed_refs(None, records)
    pending = list(candidates)
    batches, updated = 0, 0
    while pending:
        batch = []
        for item in pending:
            trial = batch + [item]
            messages = _l1_messages(l1, summaries, records, allowed,
                                    [row['card'] for row in trial])
            report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
            if batch and not report['fits']:
                break
            batch = trial
        if not batch:
            if len(records) > 8:
                records = records[:max(8, len(records) // 2)]
                allowed = _allowed_refs(None, records)
                continue
            return {'status': 'skipped', 'reason': 'l1_reconcile_context_exceeded'}
        messages = _l1_messages(l1, summaries, records, allowed,
                                [row['card'] for row in batch])
        ids = [row['thread']['thread_id'] for row in batch]
        key = digest({'protocol': 'thread-l1-v1', 'l1': l1.get('id'),
                      'threads': ids})
        existing = next((value for _, value in repo.store.objects('sequence-thread-l1-reconcile')
                         if value.get('run_id') == repo.run_id and
                         value.get('key') == key), None)
        if existing:
            parsed = existing.get('entries', [])
            status = existing.get('status')
        else:
            try:
                raw = caller.call('thread-l1:' + key, 'summary', messages, role='indexer',
                                  max_tokens=min(8192, spec.roles['indexer'].requested_output_tokens))
            except NarrativeError as exc:
                repo.store.put('sequence-thread-l1-reconcile', {
                    'run_id': repo.run_id, 'key': key, 'l1': l1.get('id'),
                    'thread_ids': ids, 'status': 'failed', 'reason': exc.kind or 'error',
                    'entries': []})
                return {'status': 'failed', 'reason': exc.kind or 'error'}
            text = raw['generation']['text']
            parsed = _parse_l1(text, batch, allowed)
            status = 'completed' if parsed is not None else 'invalid'
            repo.store.put('sequence-thread-l1-reconcile', {
                'run_id': repo.run_id, 'key': key, 'l1': l1.get('id'),
                'thread_ids': ids, 'status': status, 'generation_id': raw['id'],
                'entries': parsed or [], 'raw_text': text[:4000]})
        if status == 'invalid':
            return {'status': 'invalid', 'reason': 'parse_failed', 'batches': batches}
        updated += _apply_l1(threads, parsed or [], l1)
        batches += 1
        done = {row['thread']['thread_id'] for row in batch}
        pending = [row for row in pending if row['thread']['thread_id'] not in done]
    return {'status': 'completed', 'batches': batches, 'updated': updated,
            'candidates': len(candidates)}


def update_thread_catalog(repo, spec, view, caller, nodes, *, allow_model=True):
    """Create filtered threads from L2 nodes; batch-judge them at sealed L1 boundaries."""
    visible_ids = {doc['chapter_id'] for doc in view.visible_sources()
                   if doc['order'] < view.target_order}
    ordered = sorted((node for node in nodes
                      if node.get('level') == 2 and
                      set(node.get('source_ids', [])) <= visible_ids and
                      node.get('state') in {'extracted', 'partial'}),
                     key=lambda node: (node.get('start_position', -1),
                                       node.get('end_position', -1),
                                       node.get('id', '')))
    previous = latest_thread_catalog(repo)
    threads = dict(previous[1].get('threads', {})) if previous else {}
    reconciled = set(previous[1].get('reconciled_nodes', [])) if previous else set()
    reconciled_l1 = set(previous[1].get('reconciled_l1_nodes', [])) if previous else set()
    visibility = previous[1].get('visibility_order', -1) if previous else -1
    visible = set(previous[1].get('visible_source_ids', [])) if previous else set()
    core_names = _core_names(nodes)
    for node in ordered:
        _add_threads(threads, node, core_names)
        reconciled.add(node.get('id') or node.get('node_id'))
        visibility = max(visibility, node.get('end_position', -1))
        visible.update(node.get('source_ids', []))
    filter_stats = _filter_threads(threads, core_names, visibility)
    _save_catalog(repo, _new_catalog(repo, threads, reconciled, visibility, visible,
                                     reconciled_l1))
    l1_results = []
    if allow_model and caller is not None:
        for l1 in _sealed_l1_nodes(repo, spec, view, nodes):
            if l1.get('id') in reconciled_l1:
                l1_results.append({'l1': l1.get('node_id'), 'status': 'cached'})
                continue
            try:
                result = _reconcile_l1(repo, spec, view, caller, l1, nodes, threads)
            except NarrativeError as exc:
                result = {'status': 'failed', 'reason': exc.kind or 'error'}
            l1_results.append({'l1': l1.get('node_id'), **result})
            reconciled_l1.add(l1.get('id'))
            visibility = max(visibility, l1.get('end_position', -1))
            visible.update(l1.get('source_ids', []))
            _save_catalog(repo, _new_catalog(repo, threads, reconciled,
                                             visibility, visible, reconciled_l1))
    catalog = latest_thread_catalog(repo)
    return {'catalog_id': catalog[0] if catalog else None,
            'revision': catalog[1].get('revision') if catalog else None,
            'threads': len(threads),
            'active': len(_pinned_threads(threads)),
            'dormant': len([thread for thread in threads.values()
                            if thread.get('status') in DORMANT_STATUSES]),
            'resolved': len([thread for thread in threads.values()
                             if thread.get('status') in {'resolved', 'superseded'}]),
            'merged': len([thread for thread in threads.values()
                           if thread.get('status') == 'merged']),
            'filter': filter_stats,
            'processed': l1_results}


def read_thread_catalog(repo, *, before_order=None):
    row = latest_thread_catalog(repo, before_order=before_order)
    if not row:
        return {'status': 'not_built', 'catalog_id': None, 'threads': []}
    ident, value = row
    threads = list(value.get('threads', {}).values())
    active = [t for t in threads if t.get('status') in ACTIVE_STATUSES]
    dormant = [t for t in threads if t.get('status') in DORMANT_STATUSES]
    resolved = [t for t in threads if t.get('status') in {'resolved', 'superseded'}]
    def compact(thread):
        return {key: thread.get(key) for key in
                ('thread_id', 'status', 'question', 'subject', 'resolution',
                 'evidence', 'last_seen', 'resolved_in', 'score', 'filter_reason',
                 'merged_into')}
    return {'status': 'found', 'catalog_id': ident, 'revision': value.get('revision'),
            'visibility_order': value.get('visibility_order'),
            'thread_count': len(threads), 'active': [compact(t) for t in active],
            'dormant': [compact(t) for t in dormant],
            'resolved': [compact(t) for t in resolved],
            'reconciled_nodes': value.get('reconciled_nodes', []),
            'reconciled_l1_nodes': value.get('reconciled_l1_nodes', [])}


def read_thread_evidence(repo, thread_id, *, before_order=None):
    row = latest_thread_catalog(repo, before_order=before_order)
    if not row:
        return {'status': 'not_found', 'thread_id': thread_id}
    thread = row[1].get('threads', {}).get(thread_id)
    if not thread:
        return {'status': 'not_found', 'thread_id': thread_id}
    return {'status': 'found', 'thread': thread}
