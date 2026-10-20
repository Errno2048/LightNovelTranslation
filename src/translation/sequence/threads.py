"""Model-judged unresolved-thread reconciliation at sealed L2 boundaries."""
from __future__ import annotations

import json
import re
import unicodedata

from ..budget import plan_request
from ..narrative.schemas import NarrativeError
from .workset import _canonical, _record_text, _short

THREAD_STATUSES = {'active', 'partially_resolved', 'resolved', 'superseded', 'ambiguous'}
DEMOTED_STATUSES = {'resolved', 'superseded'}
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


def _new_catalog(repo, threads, reconciled, visibility_order, visible_source_ids):
    previous = latest_thread_catalog(repo)
    revision = (previous[1].get('revision', 0) + 1) if previous else 1
    return {'schema': 'sequence-thread-catalog-v1', 'run_id': repo.run_id,
            'revision': revision, 'threads': threads,
            'reconciled_nodes': sorted(reconciled),
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
        'source_ids': list(node.get('source_ids', [])),
        'first_seen': node.get('start_position', -1),
        'last_seen': node.get('end_position', -1),
        'evidence': [],
        'resolution': '',
        'resolved_in': None,
        'revision': 0,
        'seed': {'node_revision': node.get('id'), 'record_id': record.get('id')},
    }


def _add_threads(threads, node):
    for record in node.get('records', []):
        if record.get('type') != 'NEED':
            continue
        created = _thread_from_need(None, node, record)
        current = threads.get(created['thread_id'])
        if current is None:
            threads[created['thread_id']] = created
        else:
            current['last_seen'] = max(current.get('last_seen', -1), created['last_seen'])
            current['source_ids'] = sorted(set(current.get('source_ids', [])) |
                                           set(created.get('source_ids', [])))


def _active_threads(threads):
    return [thread for thread in threads.values() if thread.get('status') not in DEMOTED_STATUSES]


def _thread_names(thread):
    text = ' '.join([thread.get('question', '')] + list(thread.get('subject', [])))
    return sorted(set(KATAKANA.findall(text)) |
                  {value for value in thread.get('subject', []) if len(value) >= 2})


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


def update_thread_catalog(repo, spec, view, caller, nodes, *, allow_model=True):
    """Reconcile sealed L2 nodes in reading order; cache each node revision."""
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
    visibility = previous[1].get('visibility_order', -1) if previous else -1
    visible = set(previous[1].get('visible_source_ids', [])) if previous else set()
    results = []
    for node in ordered:
        node_id = node.get('id') or node.get('node_id')
        already = next((value for _, value in repo.store.objects('sequence-thread-reconcile')
                        if value.get('run_id') == repo.run_id and
                        value.get('node_revision') == node_id), None)
        if already:
            results.append({'node_revision': node_id, 'status': already.get('status'),
                            'cached': True})
            continue
        _add_threads(threads, node)
        active = _relevant_threads(_active_threads(threads), node)
        result, raw = None, None
        if active and allow_model and caller is not None:
            try:
                result, raw = _reconcile_node(repo, spec, view, caller, node, active)
            except NarrativeError as exc:
                result = {'status': 'failed', 'reason': exc.kind or 'error'}
        elif active:
            result = {'status': 'skipped', 'reason': 'model_not_enabled'}
        else:
            result = {'status': 'skipped', 'reason': 'no_active_threads'}
        if result and result.get('status') == 'completed':
            for entry in result['entries']:
                thread = threads.get(entry['thread_id'])
                if not thread:
                    continue
                thread['status'] = entry['status']
                thread['resolution'] = entry['resolution']
                thread['evidence'] = entry['evidence']
                thread['resolved_in'] = [node.get('start_position'), node.get('end_position')]
                thread['revision'] = thread.get('revision', 0) + 1
        value = {'run_id': repo.run_id, 'node_revision': node_id,
                 'node_id': node.get('node_id'), 'status': result.get('status'),
                 'reason': result.get('reason'), 'generation_id': result.get('generation_id'),
                 'entries': result.get('entries', []), 'record_count': result.get('record_count')}
        repo.store.put('sequence-thread-reconcile', value)
        results.append({'node_revision': node_id, 'status': result.get('status'),
                        'reason': result.get('reason'),
                        'updated': len(result.get('entries', []))})
        reconciled.add(node_id)
        visibility = max(visibility, node.get('end_position', -1))
        visible.update(node.get('source_ids', []))
        _save_catalog(repo, _new_catalog(repo, threads, reconciled, visibility, visible))
    if not ordered:
        _save_catalog(repo, _new_catalog(repo, threads, reconciled, visibility, visible))
    catalog = latest_thread_catalog(repo)
    return {'catalog_id': catalog[0] if catalog else None,
            'revision': catalog[1].get('revision') if catalog else None,
            'threads': len(threads),
            'active': len(_active_threads(threads)),
            'resolved': len([t for t in threads.values()
                             if t.get('status') in DEMOTED_STATUSES]),
            'processed': results}


def read_thread_catalog(repo, *, before_order=None):
    row = latest_thread_catalog(repo, before_order=before_order)
    if not row:
        return {'status': 'not_built', 'catalog_id': None, 'threads': []}
    ident, value = row
    threads = list(value.get('threads', {}).values())
    active = [t for t in threads if t.get('status') not in DEMOTED_STATUSES]
    resolved = [t for t in threads if t.get('status') in DEMOTED_STATUSES]
    def compact(thread):
        return {key: thread.get(key) for key in
                ('thread_id', 'status', 'question', 'subject', 'resolution',
                 'evidence', 'last_seen', 'resolved_in')}
    return {'status': 'found', 'catalog_id': ident, 'revision': value.get('revision'),
            'visibility_order': value.get('visibility_order'),
            'thread_count': len(threads), 'active': [compact(t) for t in active],
            'resolved': [compact(t) for t in resolved],
            'reconciled_nodes': value.get('reconciled_nodes', [])}


def read_thread_evidence(repo, thread_id, *, before_order=None):
    row = latest_thread_catalog(repo, before_order=before_order)
    if not row:
        return {'status': 'not_found', 'thread_id': thread_id}
    thread = row[1].get('threads', {}).get(thread_id)
    if not thread:
        return {'status': 'not_found', 'thread_id': thread_id}
    return {'status': 'found', 'thread': thread}
