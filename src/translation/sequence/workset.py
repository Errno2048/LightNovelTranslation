"""Compact, bounded knowledge worksets for translation checkpoints."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import threading
from collections import Counter

from ..storage import digest

KNOWLEDGE_TYPES = ('EVENT', 'STATE', 'PERSPECTIVE', 'TEMPORAL', 'NEED')
TYPE_BONUS = {'NEED': 3.0, 'TEMPORAL': 2.0, 'STATE': 2.0, 'PERSPECTIVE': 1.5, 'EVENT': 1.0}
TYPE_QUOTAS = {'NEED': 8, 'TEMPORAL': 8, 'STATE': 12, 'PERSPECTIVE': 12, 'EVENT': 20}
RECENT_GUARANTEE = {'NEED': 4, 'TEMPORAL': 4, 'STATE': 4, 'PERSPECTIVE': 4}
DEFAULT_BUDGET_TOKENS = 128000
DEFAULT_RECENT_CHAPTERS = 25
OMITTED_QUERY_IDS = 40
DEMOTED_THREAD_STATUSES = {'resolved', 'superseded', 'archived', 'merged'}
ACTIVE_THREAD_STATUSES = {'active', 'partially_resolved', 'ambiguous'}


def _thread_card(thread):
    return {'thread_id': thread.get('thread_id'), 'status': thread.get('status'),
            'question': (thread.get('question') or '')[:240],
            'subject': (thread.get('subject') or [])[:6],
            'resolution': (thread.get('resolution') or '')[:240],
            'evidence': (thread.get('evidence') or [])[:4],
            'last_seen': thread.get('last_seen'),
            'filter_reason': thread.get('filter_reason')}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _short(prefix, payload, length=8):
    raw = hashlib.sha256(_canonical(payload).encode('utf-8')).digest()
    return prefix + base64.b32encode(raw).decode('ascii').rstrip('=')[:length]


def _record_text(record):
    return _canonical(record.get('fields', {}))


def _chapter_order(record):
    best = -1
    for key in ('ja', 'zh', 'disclosed_in'):
        for value in record.get('fields', {}).get(key, []) or []:
            for found in re.findall(r'C(\d{4})', str(value)):
                best = max(best, int(found))
    for value in record.get('source_ids', []) or []:
        for found in re.findall(r'C(\d{4})', str(value)):
            best = max(best, int(found))
    return best


def _dedup(records):
    groups = {}
    for record in records:
        typ = record.get('type')
        if typ not in KNOWLEDGE_TYPES and typ != 'ENTITY':
            continue
        key = typ + '|' + _canonical(record.get('fields', {}))
        item = groups.get(key)
        alias = {'node_revision': record.get('node_revision'), 'record_id': record.get('id')}
        if item is None:
            groups[key] = {'key': key, 'record': record, 'aliases': [alias]}
        elif alias not in item['aliases']:
            item['aliases'].append(alias)
    return list(groups.values())


def _assign_ids(items, prefix):
    used = {}
    for item in items:
        payload = item.get('payload') or item.get('record')
        length = 8
        while True:
            short_id = _short(prefix, {
                'type': payload.get('type'),
                'fields': payload.get('fields', {}),
                'name': payload.get('name'),
            }, length)
            if short_id not in used or used[short_id] is item:
                break
            length += 2
        used[short_id] = item
        item['short_id'] = short_id
    return used


KNOWLEDGE_FIELD_LIMITS = {
    'description': (1, 240), 'ja': (8, 32), 'zh': (8, 32),
    'participants': (8, 80), 'subject': (8, 80), 'story_time': (2, 120),
    'disclosed_in': (4, 32), 'relation': (2, 32), 'attitude': (2, 32),
    'from_event': (2, 48), 'to_event': (2, 48),
}
ENTITY_FIELD_LIMITS = {
    'name': (1, 160), 'kind': (1, 64), 'description': (1, 200),
    'target_form': (4, 60), 'ambiguity': (1, 120),
}


def _project_fields(fields, limits):
    result = {}
    for key, (count, chars) in limits.items():
        values = [str(value)[:chars] for value in fields.get(key, []) or []
                  if isinstance(value, str) and value]
        if values:
            result[key] = values[:count]
    return result


def _card(short_id, record):
    return {'id': short_id, 'type': record.get('type'),
            'fields': _project_fields(record.get('fields', {}), KNOWLEDGE_FIELD_LIMITS),
            'authority': record.get('authority', 'candidate_unreviewed'),
            'semantic_status': record.get('semantic_status', 'candidate_unreviewed')}


def _entity_card(short_id, card):
    return {'id': short_id, 'type': 'ENTITY',
            'fields': _project_fields(card.get('fields', {}), ENTITY_FIELD_LIMITS),
            'authority': card.get('authority', 'candidate_unreviewed'),
            'semantic_status': card.get('semantic_status', 'candidate_unreviewed'),
            'prompt_projection': card.get('prompt_projection', 'entity-card-v1')}


def _target_names(entity_records, entity_cards, target_text):
    names = set()
    for record in entity_records:
        for key in ('name', 'target_form'):
            for value in record.get('fields', {}).get(key, []) or []:
                value = str(value).strip()
                if len(value) >= 2:
                    names.add(value)
    for card in entity_cards:
        for key in ('name', 'target_form'):
            for value in card.get('fields', {}).get(key, []) or []:
                value = str(value).strip()
                if len(value) >= 2:
                    names.add(value)
    return sorted(name for name in names if name in target_text)


def rank_records(items, target_names, target_order, recent_chapters):
    total = len(items)
    texts = {id(item): _record_text(item['record']) for item in items}
    frequency = Counter()
    for name in target_names:
        for item in items:
            if name in texts[id(item)]:
                frequency[name] += 1
    for item in items:
        text = texts[id(item)]
        hits = [name for name in target_names if name in text]
        name_score = sum(math.log((total + 1) / (frequency[name] + 1)) + 1.0 for name in hits)
        recent = _chapter_order(item['record'])
        recency = 0.0
        if recent >= 0:
            distance = max(0, target_order - recent)
            if distance <= recent_chapters:
                recency = 2.0 * (1.0 - distance / max(1, recent_chapters))
        typ = item['record'].get('type')
        score = 4.0 * name_score + recency + TYPE_BONUS.get(typ, 1.0)
        if typ == 'NEED':
            score += 2.0
        item.update({'score': score, 'hits': hits, 'recent': recent})
    return sorted(items, key=lambda item: (-item['score'], -item['recent'], item['key']))


def _select_knowledge(items, target_names, target_order, spec, counter, budget):
    ranked = rank_records(items, target_names, target_order,
                          spec.workset_recent_chapters or DEFAULT_RECENT_CHAPTERS)
    selected, chosen, used = [], set(), 0

    def add(item, reason):
        nonlocal used
        if item['key'] in chosen:
            return True
        card = _card(item['short_id'], item['record'])
        cost = counter.count(_canonical(card))
        if used + cost > budget:
            return False
        item['reason'] = reason
        item['card'] = card
        selected.append(item)
        chosen.add(item['key'])
        used += cost
        return True

    active_count = 0
    for item in ranked:
        if item.get('thread_status') not in ACTIVE_THREAD_STATUSES:
            continue
        if add(item, 'active_thread'):
            active_count += 1
        if active_count >= 40:
            break
    for typ, limit in RECENT_GUARANTEE.items():
        count = 0
        for item in ranked:
            if item['record'].get('type') != typ or item['key'] in chosen:
                continue
            if item['recent'] >= target_order - spec.workset_recent_chapters:
                if add(item, 'recent_window'):
                    count += 1
            if count >= limit:
                break
    progress = True
    while progress:
        progress = False
        for typ in ('NEED', 'TEMPORAL', 'STATE', 'PERSPECTIVE', 'EVENT'):
            count = sum(1 for item in selected if item['record'].get('type') == typ)
            if count >= TYPE_QUOTAS[typ]:
                continue
            for item in ranked:
                if item['key'] in chosen or item['record'].get('type') != typ:
                    continue
                if add(item, 'quota'):
                    count += 1
                    progress = True
                if count >= TYPE_QUOTAS[typ]:
                    break
    for item in ranked:
        if item['key'] in chosen:
            continue
        add(item, 'score')
    omitted = [item for item in ranked if item['key'] not in chosen]
    return selected, omitted, used, ranked


def _entity_items(entity_catalog_id, entity_cards, entity_records, target_names,
                  spec, counter, budget):
    if entity_catalog_id and entity_cards:
        items = []
        for card in entity_cards:
            payload = {'name': card.get('fields', {}).get('name', []),
                       'kind': card.get('fields', {}).get('kind', []),
                       'target_form': card.get('fields', {}).get('target_form', [])}
            items.append({'payload': payload, 'card': card,
                          'source': {'entity_id': card.get('id')}, 'type': 'ENTITY'})
        _assign_ids(items, 'E')
        for item in items:
            item['card'] = _entity_card(item['short_id'], item['card'])
            item['record'] = item['card']
            text = _canonical(item['card'].get('fields', {}))
            item['score'] = 10.0 * sum(1 for name in target_names if name in text)
        items.sort(key=lambda item: (-item['score'], item['short_id']))
        selected, omitted = [], []
        used = 0
        for item in items:
            cost = counter.count(_canonical(item['card']))
            if used + cost <= budget:
                selected.append(item)
                used += cost
            else:
                omitted.append(item)
        return selected, omitted
    deduped = _dedup(entity_records)
    for item in deduped:
        text = _record_text(item['record'])
        item['score'] = 10.0 * sum(1 for name in target_names if name in text)
    deduped.sort(key=lambda item: (-item['score'], item['key']))
    _assign_ids(deduped, 'E')
    selected, omitted = [], []
    used = 0
    limit = max(1, spec.core_entity_limit or 50)
    for item in deduped:
        card = _entity_card(item['short_id'], item['record'])
        cost = counter.count(_canonical(card))
        if len(selected) < limit and used + cost <= budget:
            item['card'] = card
            selected.append(item)
            used += cost
        else:
            omitted.append(item)
    return selected, omitted


def _catalog_entry(item, selected):
    record = item.get('record') or item.get('card') or {}
    entry = {'short_id': item['short_id'], 'type': record.get('type'),
             'source': item.get('source', {'aliases': item.get('aliases', [])}),
             'score': round(item.get('score', 0.0), 4),
             'hits': item.get('hits', []), 'recent': item.get('recent', -1)}
    if item.get('thread'):
        entry['thread_id'] = item['thread'].get('thread_id')
        entry['thread_status'] = item['thread'].get('status')
    if selected:
        limits = ENTITY_FIELD_LIMITS if entry['type'] == 'ENTITY' else KNOWLEDGE_FIELD_LIMITS
        entry['fields'] = _project_fields(record.get('fields', {}), limits)
        entry['authority'] = record.get('authority', 'candidate_unreviewed')
        entry['semantic_status'] = record.get('semantic_status', 'candidate_unreviewed')
    return entry


_CATALOG_INDEX_LOCK = threading.RLock()


def _catalog_index(repo):
    """Keep catalog metadata in memory; the payloads are large and immutable."""
    with _CATALOG_INDEX_LOCK:
        index = getattr(repo, '_workset_catalog_index', None)
        count = repo.store.count('sequence-workset-catalog')
        if index is None or len(index) != count:
            index = [(ident, value.get('run_id'), value.get('fingerprint'),
                      value.get('revision', 0))
                     for ident, value in repo.store.objects('sequence-workset-catalog')]
            repo._workset_catalog_index = index
        return index


def select_workset(repo, spec, view, nodes, target_text, target_order, *,
                   entity_catalog_id, entity_cards, entity_records, records,
                   core, unresolved, gaps, threads=None):
    counter = view.counter
    budget = max(1000, int(spec.workset_max_tokens or DEFAULT_BUDGET_TOKENS))
    target_names = _target_names(entity_records, entity_cards, target_text)
    thread_values = list((threads or {}).values())
    active_threads = sorted((thread for thread in thread_values
                             if thread.get('status') in ACTIVE_THREAD_STATUSES),
                            key=lambda thread: thread.get('last_seen', -1), reverse=True)[:40]
    resolved_threads = sorted((thread for thread in thread_values
                               if thread.get('status') in DEMOTED_THREAD_STATUSES),
                              key=lambda thread: thread.get('last_seen', -1), reverse=True)[:30]
    dormant_relevant = []
    target_corpus = target_text + ' ' + _canonical(
        [card.get('fields', {}) for card in entity_cards])
    for thread in thread_values:
        if thread.get('status') != 'dormant':
            continue
        names = thread.get('entity_names') or []
        if names and any(name in target_corpus for name in names):
            dormant_relevant.append(thread)
    dormant_relevant.sort(key=lambda thread: thread.get('last_seen', -1), reverse=True)
    dormant_relevant = dormant_relevant[:20]
    thread_payload = {'active': [_thread_card(thread) for thread in active_threads],
                      'resolved': [_thread_card(thread) for thread in resolved_threads],
                      'dormant_relevant': [_thread_card(thread) for thread in dormant_relevant]}
    fixed = [counter.count(_canonical(value))
             for value in core + unresolved + gaps + [thread_payload]]
    available = max(0, budget - sum(fixed))
    entity_selected, entity_omitted = _entity_items(
        entity_catalog_id, entity_cards, entity_records, target_names,
        spec, counter, available // 3)
    entity_cards_compact = [item['card'] for item in entity_selected]
    entity_cost = sum(counter.count(_canonical(card)) for card in entity_cards_compact)
    remaining = max(0, available - entity_cost)
    knowledge_items = [item for item in _dedup(records)
                       if item['record'].get('type') in KNOWLEDGE_TYPES]
    thread_by_key = {thread.get('seed_key'): thread
                     for thread in (threads or {}).values() if thread.get('seed_key')}
    for item in knowledge_items:
        if item['record'].get('type') != 'NEED':
            continue
        thread = thread_by_key.get(_canonical(item['record'].get('fields', {})))
        if thread:
            item['thread'] = thread
            item['thread_status'] = thread.get('status')
    knowledge_items = [item for item in knowledge_items
                       if item.get('thread_status') not in DEMOTED_THREAD_STATUSES]
    _assign_ids(knowledge_items, 'K')
    selected, omitted, knowledge_cost, ranked = _select_knowledge(
        knowledge_items, target_names, target_order, spec, counter, remaining)
    selected_ids = [item['short_id'] for item in selected]
    omitted_ids = [item['short_id'] for item in omitted]
    fingerprint = digest({
        'target_id': view.policy.target_id, 'target_order': target_order,
        'budget': budget, 'entities': [item['short_id'] for item in entity_selected],
        'selected': selected_ids, 'omitted': omitted_ids,
        'threads': sorted((thread.get('thread_id'), thread.get('status'),
                           thread.get('revision')) for thread in thread_values),
    })
    index = _catalog_index(repo)
    existing = next((ident for ident, run_id, catalog_fingerprint, revision in index
                     if run_id == repo.run_id and catalog_fingerprint == fingerprint), None)
    if existing:
        catalog_id = existing
    else:
        revision = max((entry[3] for entry in index), default=0) + 1
        catalog = {
            'schema': 'sequence-workset-catalog-v1', 'run_id': repo.run_id,
            'target_id': view.policy.target_id, 'target_order': target_order,
            'revision': revision,
            'budget_tokens': budget, 'used_tokens': sum(fixed) + entity_cost + knowledge_cost,
            'entities': [_catalog_entry(item, True) for item in entity_selected],
            'selected': [_catalog_entry(item, True) for item in selected],
            'omitted': [_catalog_entry(item, False) for item in omitted],
            'omitted_entities': [_catalog_entry(item, False) for item in entity_omitted],
            'fingerprint': fingerprint,
        }
        catalog_id = repo.store.put('sequence-workset-catalog', catalog)
        with _CATALOG_INDEX_LOCK:
            index.append((catalog_id, catalog['run_id'], catalog['fingerprint'], revision))
    selected_counts = Counter(item['record'].get('type') for item in selected)
    selected_counts['ENTITY'] += len(entity_selected)
    omitted_counts = Counter(item['record'].get('type') for item in omitted)
    omitted_counts['ENTITY'] += len(entity_omitted)
    omitted_query_ids = [item['short_id'] for item in
                         sorted(omitted, key=lambda item: -item['score'])[:OMITTED_QUERY_IDS]]
    return {
        'core': core, 'candidates': entity_cards_compact + [item['card'] for item in selected],
        'unresolved': unresolved, 'extraction_gaps': gaps,
        'entity_catalog_id': entity_catalog_id,
        'entity_candidate_count': len(entity_selected),
        'workset_catalog_id': catalog_id,
        'threads': thread_payload,
        'selection': {
            'budget_tokens': budget,
            'used_tokens': sum(fixed) + entity_cost + knowledge_cost,
            'selected_counts': dict(selected_counts),
            'omitted_counts': dict(omitted_counts),
            'omitted_query_ids': omitted_query_ids,
            'thread_counts': {'active': len(active_threads),
                              'resolved': len(resolved_threads),
                              'dormant_relevant': len(dormant_relevant)},
            'target_order': target_order,
            'recent_chapters': spec.workset_recent_chapters,
        },
        'authority': 'candidate_unreviewed',
    }


def latest_workset_catalog(repo, *, before_order=None):
    rows = [(ident, value) for ident, value in repo.store.objects('sequence-workset-catalog')
            if value.get('run_id') == repo.run_id]
    if before_order is not None:
        rows = [(ident, value) for ident, value in rows
                if value.get('target_order', -1) <= before_order]
    return max(rows, key=lambda row: (row[1].get('target_order', -1),
                                      row[1].get('revision', 0)), default=None)


def _catalog_row(repo, *, before_order=None, catalog_id=None):
    if catalog_id:
        return catalog_id, repo.store.get(catalog_id, 'sequence-workset-catalog')
    return latest_workset_catalog(repo, before_order=before_order)


def read_workset_catalog(repo, *, before_order=None, catalog_id=None):
    row = _catalog_row(repo, before_order=before_order, catalog_id=catalog_id)
    if not row:
        return {'status': 'not_built', 'catalog_id': None, 'selected': [], 'omitted': []}
    ident, value = row
    return {
        'status': 'found', 'catalog_id': ident, 'revision': value.get('revision', 0),
        'target_id': value.get('target_id'), 'target_order': value.get('target_order'),
        'budget_tokens': value.get('budget_tokens'), 'used_tokens': value.get('used_tokens'),
        'selected': [entry['short_id'] for entry in value.get('entities', []) + value.get('selected', [])],
        'omitted': [entry['short_id'] for entry in value.get('omitted_entities', []) + value.get('omitted', [])],
        'selected_counts': dict(Counter(entry['type'] for entry in
                                        value.get('entities', []) + value.get('selected', []))),
        'omitted_counts': dict(Counter(entry['type'] for entry in
                                       value.get('omitted_entities', []) + value.get('omitted', []))),
    }


def _source_record(repo, aliases):
    for alias in aliases:
        revision = alias.get('node_revision')
        record_id = alias.get('record_id')
        if not revision or not record_id:
            continue
        for kind in ('sequence-hierarchy-node', 'sequence-node'):
            try:
                node = repo.store.get(revision, kind)
            except Exception:
                continue
            for record in node.get('records', []):
                if record.get('id') == record_id:
                    return record, node
    return None, None


def read_workset_evidence(repo, short_id, *, before_order=None, catalog_id=None):
    row = _catalog_row(repo, before_order=before_order, catalog_id=catalog_id)
    if not row:
        return {'status': 'not_found', 'id': short_id}
    value = row[1]
    entries = value.get('entities', []) + value.get('selected', []) + \
        value.get('omitted_entities', []) + value.get('omitted', [])
    entry = next((item for item in entries if item.get('short_id') == short_id), None)
    if not entry:
        return {'status': 'not_found', 'id': short_id}
    fields = entry.get('fields')
    source = entry.get('source', {})
    record, node = _source_record(repo, source.get('aliases', []))
    if record is not None:
        fields = record.get('fields', {})
        source = {'aliases': source.get('aliases', []),
                  'node_id': node.get('node_id'), 'source_ids': node.get('source_ids', [])}
    if fields is None:
        return {'status': 'not_found', 'id': short_id, 'source': source}
    return {'status': 'found', 'id': short_id, 'type': entry.get('type'),
            'fields': fields, 'source': source,
            'authority': entry.get('authority', 'candidate_unreviewed'),
            'score': entry.get('score'), 'hits': entry.get('hits', []),
            'recent': entry.get('recent')}
