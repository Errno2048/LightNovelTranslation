"""Persistent semantic entity merges and bounded core-entity prompt views."""
from __future__ import annotations

import json
import re
import unicodedata

from ..budget import plan_request
from ..narrative.schemas import NarrativeError
from ..storage import digest


def normalize_name(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()


def _field(record, name):
    values = record.get('fields', {}).get(name, [])
    return [str(value) for value in values if str(value).strip()]


def _record_id(record, node):
    return digest({'node_id': node.get('node_id'), 'record_id': record.get('id'),
                   'fields': record.get('fields', {})})[:24]


def _candidate_rows(nodes):
    rows = []
    for node in nodes:
        refs = node.get('references', {})
        for record in node.get('records', []):
            if record.get('type') != 'ENTITY':
                continue
            names = _field(record, 'name')
            if len(names) != 1:
                continue
            evidence = []
            for ref in _field(record, 'ja') + _field(record, 'zh'):
                row = refs.get(ref)
                if row:
                    text = row.get('text', '')
                    mention = next((form for form in [names[0]] + _field(record, 'target_form') if form in text), '')
                    if mention:
                        at = text.find(mention)
                        start = max(0, at - 140)
                        end = min(len(text), at + len(mention) + 220)
                        excerpt = text[start:end]
                    else:
                        excerpt = text[:360]
                    evidence.append({'ref': ref, 'language': row.get('language'),
                        'chapter_id': row.get('chapter_id'), 'paragraph_id': row.get('paragraph_id'),
                        'excerpt': excerpt, 'text_hash': digest(text)})
            rows.append({'member_id': _record_id(record, node), 'node_id': node.get('node_id'),
                'node_revision': node.get('id'), 'level': node.get('level'),
                'source_ids': list(node.get('source_ids', [])), 'name': names[0],
                'kind': (_field(record, 'kind') or ['other'])[0],
                'description': (_field(record, 'description') or [''])[0],
                'target_forms': _field(record, 'target_form'), 'evidence': evidence,
                'authority': record.get('authority', 'candidate_unreviewed')})
    # A direct L1 and its later L2 descendants can repeat the same observation.
    # Collapse only byte-equivalent claims over overlapping source ranges; keep
    # the union of their citations and node revisions for auditability.
    unique = {}
    for row in rows:
        semantic = {'name': normalize_name(row['name']), 'kind': row['kind'],
            'description': ' '.join(row['description'].split()),
            'target_forms': sorted(set(row['target_forms']))}
        key = digest(semantic)
        if key not in unique:
            unique[key] = {**row, 'member_ids': [row['member_id']],
                           'node_revisions': [row['node_revision']] if row['node_revision'] else []}
        else:
            previous = unique[key]
            previous['member_ids'] = sorted(set(previous['member_ids']) | {row['member_id']})
            previous['node_revisions'] = sorted(set(previous['node_revisions']) |
                ({row['node_revision']} if row['node_revision'] else set()))
            previous['source_ids'] = sorted(set(previous['source_ids']) | set(row['source_ids']))
            previous['evidence'] = _merge_evidence(previous['evidence'], row['evidence'])
    return sorted(unique.values(), key=lambda row: (normalize_name(row['name']), row['member_id']))


def _merge_evidence(left, right):
    result = {digest(item): item for item in left}
    result.update({digest(item): item for item in right})
    return list(result.values())


def latest_entity_catalog(repo, *, visible_source_ids=None, before_order=None):
    rows = [(ident, value) for ident, value in repo.store.objects('sequence-entity-catalog')
            if value.get('run_id') == repo.run_id]
    if visible_source_ids is not None:
        visible = set(visible_source_ids)
        rows = [(ident, value) for ident, value in rows
                if set(value.get('visible_source_ids', [])) <= visible]
    if before_order is not None:
        rows = [(ident, value) for ident, value in rows
                if value.get('visibility_order', -1) < before_order]
    if not rows:
        return None
    return max(rows, key=lambda item: (item[1].get('visibility_order', -1),
                                       item[1].get('revision', 0)))


def _visible_entity_nodes(nodes, view):
    """Exclude index facts beyond this view's source-order boundary."""
    visible = {doc['chapter_id'] for doc in view.visible_sources()
               if doc['order'] < view.target_order}
    result = []
    for node in nodes:
        source_ids = set(node.get('source_ids', node.get('ordered_source_ids', [])))
        references = node.get('references', {})
        has_future_or_unknown_refs = any(
            row.get('chapter_id') not in visible for row in references.values())
        if source_ids and source_ids <= visible and not has_future_or_unknown_refs:
            result.append(node)
            continue
        allowed_refs = {ref for ref, row in references.items()
                        if row.get('chapter_id') in visible}
        records = []
        for record in node.get('records', []):
            if record.get('type') != 'ENTITY':
                continue
            fields = dict(record.get('fields', {}))
            citations = []
            record_refs = []
            has_hidden_citation = False
            for field_name in ('ja', 'zh'):
                values = fields.get(field_name, [])
                record_refs.extend(values)
                if any(ref not in allowed_refs for ref in values):
                    has_hidden_citation = True
                visible_values = [ref for ref in values if ref in allowed_refs]
                if visible_values:
                    fields[field_name] = visible_values
                    citations.extend(visible_values)
            # The description and rendering fields are node-level claims, not
            # tied to individual citations. If a record cites any hidden or
            # unknown chapter, its prose may summarize future material too.
            # Keep only records whose complete citation set is visible.
            if citations and record_refs and not has_hidden_citation:
                records.append({**record, 'fields': fields})
        if records:
            result.append({**node,
                'source_ids': sorted({references[ref]['chapter_id'] for ref in allowed_refs
                                      if references[ref].get('language') == 'ja'}),
                'summary': '',
                'records': records,
                'references': {ref: references[ref] for ref in allowed_refs}})
        # A parent summary spanning the current target may summarize future
        # chapters, so it is deliberately omitted as a whole.
    return result, visible


def _l1_context(nodes):
    return [{'node_id': node.get('node_id'), 'source_ids': node.get('source_ids', []),
             'summary': node.get('summary', '')}
            for node in sorted(nodes, key=lambda row: (row.get('end_position', 0), row.get('level', 0)))
            if node.get('level') == 1 and node.get('summary')]


def _merge_prompt(member_rows, l1_summaries):
    shared = {'visible_l1_navigation_only': l1_summaries}
    payload = {'same_name_candidates': member_rows}
    return [
        {'role': 'system', 'content': '你整理同一作品同名实体的增量观察。仅按给定日文证据与摘要判断；摘要只用于理解，不是引用证据。相同写法可能有同名歧义，必须保留冲突与不确定性。只合并同一个实体的别称、译法和描述，不创造名称或译法。输出一个JSON对象。'},
        {'role': 'user', 'content': '【当前可见的全部L1摘要，仅作共享导航】\n' +
            json.dumps(shared, ensure_ascii=False, separators=(',', ':'))},
        {'role': 'user', 'content': '【名称组及其L2摘要、局部原文证据】\n' +
            json.dumps(payload, ensure_ascii=False, separators=(',', ':')) +
            '\n返回格式：{"name":"输入中的原始名称","kind":"...","description":"合并后的简明作品内描述","target_forms":["输入中已有译法"],"member_ids":["输入成员ID"],"ambiguity":"无或具体歧义"}。每个member_id必须恰好出现一次。'}]


def _decode_json(text):
    # Models sometimes emit full-width braces/quotes/commas even when asked
    # for JSON. NFKC makes those punctuation variants parseable without
    # weakening the downstream ID and duplicate checks.
    text = unicodedata.normalize('NFKC', text).strip()
    text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text, flags=re.I)
    start, end = text.find('{'), text.rfind('}')
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_merge(text, rows):
    value = _decode_json(text)
    if not isinstance(value, dict):
        return None
    expected = sorted({member for row in rows for member in row['member_ids']})
    members = value.get('member_ids')
    if (not isinstance(members, list) or any(not isinstance(item, str) for item in members) or
            sorted(members) != expected or len(set(members)) != len(members)):
        return None
    allowed_names = {row['name'] for row in rows}
    allowed_kinds = {row['kind'] for row in rows}
    allowed_forms = {form for row in rows for form in row['target_forms']}
    if value.get('name') not in allowed_names or value.get('kind') not in allowed_kinds:
        return None
    forms = value.get('target_forms', [])
    if not isinstance(forms, list) or any(form not in allowed_forms for form in forms):
        return None
    if not isinstance(value.get('description'), str) or not value['description'].strip():
        return None
    ambiguity = value.get('ambiguity', '')
    if not isinstance(ambiguity, str):
        return None
    return {'name': value['name'], 'kind': value['kind'],
        'description': value['description'].strip(), 'target_forms': sorted(set(forms)),
        'member_ids': expected, 'ambiguity': ambiguity.strip()}


def _summaries_for(nodes, node_ids):
    wanted = set(node_ids)
    return [{'node_id': node.get('node_id'), 'summary': node.get('summary', '')}
            for node in nodes if node.get('node_id') in wanted and node.get('summary')]


def _semantic_merge(repo, spec, caller, rows, l1_summaries):
    names = {normalize_name(row['name']) for row in rows}
    if len(names) != 1:
        raise NarrativeError('entity_catalog_invalid', 'semantic merge received different names')
    members = sorted({member for row in rows for member in row['member_ids']})
    key = digest({'members': members, 'rows': rows, 'l1_summaries': l1_summaries})
    for ident, saved in repo.store.objects('sequence-entity-merge'):
        if saved.get('key') == key and saved.get('run_id') == repo.run_id:
            return saved['entity']
    if len(rows) == 1:
        result = {'name': rows[0]['name'], 'kind': rows[0]['kind'],
            'description': rows[0]['description'] or rows[0]['name'],
            'target_forms': rows[0]['target_forms'], 'member_ids': members,
            'ambiguity': ''}
    else:
        messages = _merge_prompt(rows, l1_summaries)
        from ..budget import plan_request
        role = spec.roles['indexer']
        budget = plan_request(messages, spec.capacity, role, caller.counter)
        if not budget['fits']:
            raise NarrativeError('entity_merge_context_exceeded',
                'same-name entity evidence exceeds one request; member count=' + str(len(rows)))
        result = None
        for attempt in range(spec.max_attempts):
            request_messages = messages if attempt == 0 else messages + [
                {'role': 'user', 'content': '上次结果未通过成员覆盖或字段校验。请只修正格式，完整保留输入成员，不猜测新译法。'}]
            raw = caller.call('entity-merge:' + key + ':attempt:' + str(attempt + 1),
                'summary', request_messages, role='indexer')
            result = _parse_merge(raw['generation']['text'], rows)
            if result is not None:
                break
        if result is None:
            raise NarrativeError('entity_merge_invalid', 'semantic entity merge failed validation; raw response retained')
    repo.store.put('sequence-entity-merge', {'run_id': repo.run_id, 'key': key,
        'member_ids': members, 'entity': result, 'semantic_status': 'candidate_unreviewed'})
    return result


def _entity_entry(result, rows):
    evidence = []
    for row in rows:
        evidence = _merge_evidence(evidence, row['evidence'])
    ident = 'entity:' + digest({'members': result['member_ids']})[:24]
    fields = {'name': [result['name']], 'kind': [result['kind']],
        'description': [result['description']], 'target_form': result['target_forms'],
        'ambiguity': [result['ambiguity']] if result['ambiguity'] else []}
    return {'id': ident, 'type': 'ENTITY', 'fields': fields,
        'member_ids': result['member_ids'], 'source_ids': sorted({sid for row in rows for sid in row['source_ids']}),
        'evidence': evidence, 'node_revisions': sorted({rev for row in rows for rev in row['node_revisions']}),
        'semantic_status': 'candidate_unreviewed', 'authority': 'provisional_rendering' if result['target_forms'] else 'candidate_unreviewed'}


def _core_candidate_projection(candidates, level=0):
    """Project catalog entries to the few fields needed for semantic ranking.

    Provenance and full excerpts remain in the local catalog/evidence API; they
    are intentionally not repeated for every candidate in the selector prompt.
    If the complete projection is too large, later levels shorten descriptions
    while retaining every candidate ID.
    """
    result = []
    for row in candidates:
        fields = row.get('fields', {})
        names = _field(row, 'name') or _field({'fields': fields}, 'name')
        kinds = _field(row, 'kind') or _field({'fields': fields}, 'kind')
        descriptions = _field(row, 'description') or _field({'fields': fields}, 'description')
        forms = (_field(row, 'target_form') or _field({'fields': fields}, 'target_form'))
        ambiguities = _field(row, 'ambiguity') or _field({'fields': fields}, 'ambiguity')
        description = descriptions[0] if descriptions else ''
        ambiguity = ambiguities[0] if ambiguities else ''
        projected = {'id': row['id'], 'name': names[0] if names else '',
            'kind': kinds[0] if kinds else 'other', 'description': description,
            'target_forms': list(dict.fromkeys(forms)),
            'ambiguity': ambiguity,
            'observation_count': len(row.get('source_ids', []))}
        if level >= 1:
            if len(projected['description']) > 1200:
                projected['description'] = projected['description'][:1200]
                projected['description_truncated'] = True
            projected['target_forms'] = projected['target_forms'][:16]
            if len(projected['ambiguity']) > 300:
                projected['ambiguity'] = projected['ambiguity'][:300]
                projected['ambiguity_truncated'] = True
        if level >= 2:
            if len(projected['description']) > 320:
                projected['description'] = projected['description'][:320]
                projected['description_truncated'] = True
            projected['target_forms'] = projected['target_forms'][:8]
            if len(projected['ambiguity']) > 100:
                projected['ambiguity'] = projected['ambiguity'][:100]
                projected['ambiguity_truncated'] = True
        if level >= 3:
            if len(projected['description']) > 80:
                projected['description'] = projected['description'][:80]
                projected['description_truncated'] = True
            projected['target_forms'] = projected['target_forms'][:3]
            projected.pop('ambiguity', None)
            projected.pop('observation_count', None)
        result.append(projected)
    return result


def _core_messages(spec, l1_summaries, nearby_l2, nearby_original, candidates, keep,
                   *, projection_level=0):
    shared = {'l1_navigation_only': l1_summaries, 'nearby_l2_navigation_only': nearby_l2,
              'nearby_original_and_reference': nearby_original}
    task = {'candidate_entities': _core_candidate_projection(candidates, projection_level),
            'maximum_items': keep, 'projection_level': projection_level}
    return [
        {'role': 'system', 'content': '你从候选实体目录中选择对后续翻译最重要的人物、称谓和作品专名。只选择已给出的ID，不改写事实，不创造实体；主角、核心关系、反复出现的技能/组织优先，短期一次性实体可略。最多返回指定数量。'},
        {'role': 'user', 'content': '【共享上下文前缀：全部L1、邻近L2和邻近原文】\n' +
            json.dumps(shared, ensure_ascii=False, separators=(',', ':'))},
        {'role': 'user', 'content': '【核心实体筛选任务】\n' +
            json.dumps(task, ensure_ascii=False, separators=(',', ':')) +
            '\n只返回JSON：{"entity_ids":["输入ID"]}；ID必须来自本批候选且不重复。'}]


def _parse_core(text, candidates, keep):
    value = _decode_json(text)
    if not isinstance(value, dict) or not isinstance(value.get('entity_ids'), list):
        return None
    ids = value['entity_ids']
    allowed = {row['id'] for row in candidates}
    if (not ids or len(ids) > keep or any(not isinstance(i, str) or i not in allowed for i in ids) or
            len(ids) != len(set(ids))):
        return None
    return ids


def _select_core(repo, spec, caller, l1_summaries, nodes, candidates, view):
    limit = spec.core_entity_limit
    if len(candidates) <= min(spec.entity_catalog_threshold, limit):
        return [row['id'] for row in candidates], {'status': 'not_needed', 'model_calls': 0}
    source_docs = [doc for doc in view.visible_sources() if doc['order'] < view.target_order]
    nearby_pairs = view.read_chapters([doc['chapter_id'] for doc in source_docs[-spec.n_chapter:]]) if source_docs else []
    nearby_original = [{'source_id': pair['source']['chapter_id'], 'title': pair['source']['title'],
        'source': pair['source']['text'],
        'translation': [doc['text'] for doc in pair.get('translation', [])]} for pair in nearby_pairs]
    child_nodes = sorted((node for node in nodes if node.get('level') == 2 and node.get('summary')),
        key=lambda node: node.get('end_position', 0))[-spec.n_child:]
    nearby_l2 = [{'node_id': node.get('node_id'), 'source_ids': node.get('source_ids', []),
                  'summary': node.get('summary', '')} for node in child_nodes]
    prompt_candidates = _core_candidate_projection(candidates)
    shared_signature = digest({'protocol': 'entity-core-selector-v2', 'l1': l1_summaries,
        'l2': nearby_l2, 'nearby': nearby_original,
        'candidates': prompt_candidates, 'limit': limit})
    for ident, saved in repo.store.objects('sequence-entity-core'):
        if saved.get('run_id') == repo.run_id and saved.get('key') == shared_signature:
            return saved['entity_ids'], {'status': 'cached', 'model_calls': 0, 'revision_id': ident}

    result, selection = _core_call(repo, spec, caller, shared_signature,
        l1_summaries, nearby_l2, nearby_original, candidates, limit)
    ident = repo.store.put('sequence-entity-core', {'run_id': repo.run_id,
        'key': shared_signature, 'entity_ids': result, 'candidate_count': len(candidates),
        'limit': limit, 'model_calls': selection['model_calls'],
        'model_attempts': selection['model_attempts'],
        'projection_level': selection['projection_level'],
        'semantic_status': 'candidate_unreviewed'})
    return result, {'status': 'selected', **selection, 'revision_id': ident}


def _core_call(repo, spec, caller, key, l1, l2, nearby, candidates, keep):
    result, stats = _core_select_all(repo, spec, caller, key, l1, l2, nearby,
                                     candidates, keep, 'full', 0)
    if result is not None:
        return result, stats

    # Very large catalogs are packed by measured request budget. Each request
    # keeps the exact same L1/L2/nearby-prose prefix; only candidate rows vary.
    # A final tournament pass compares the union rather than dropping a fixed
    # tail of entries.
    remaining = sorted(candidates,
        key=lambda row: digest({'selector': key, 'entity_id': row['id']}))
    calls = attempts = 0
    projection_level = 0
    round_number = 0
    while len(remaining) > keep:
        round_number += 1
        projection_level, batches = _pack_core_batches(
            spec, caller, l1, l2, nearby, remaining, keep)
        quotas = _batch_quotas(batches, keep)
        selected = []
        for i, (batch, quota) in enumerate(zip(batches, quotas)):
            ids, used_attempts = _core_select_all(repo, spec, caller, key,
                l1, l2, nearby, batch, quota,
                f'round-{round_number}-batch-{i}', projection_level)
            if ids is None:
                raise NarrativeError('entity_core_context_exceeded',
                    'packed core entity batch no longer fits its measured request budget')
            by_id = {row['id']: row for row in batch}
            selected.extend(by_id[ident] for ident in ids)
            calls += used_attempts['model_calls']
            attempts += used_attempts['model_attempts']
        if len(selected) >= len(remaining):
            raise NarrativeError('entity_core_selection_invalid',
                'core selector failed to reduce an over-limit candidate set')
        remaining = selected
    if not remaining:
        raise NarrativeError('entity_core_selection_invalid', 'core selector returned an empty core list')
    return [row['id'] for row in remaining], {'model_calls': calls,
        'model_attempts': attempts, 'projection_level': projection_level}


def _core_select_all(repo, spec, caller, key, l1, l2, nearby, candidates,
                     keep, batch_id, minimum_projection=0):
    last_budget = None
    for projection_level in range(minimum_projection, 4):
        messages = _core_messages(spec, l1, l2, nearby, candidates, keep,
                                  projection_level=projection_level)
        budget = plan_request(messages, spec.capacity, spec.roles['indexer'], caller.counter)
        last_budget = budget
        if not budget['fits']:
            continue
        for attempt in range(spec.max_attempts):
            request_messages = messages if attempt == 0 else messages + [
                {'role': 'user', 'content': '上次结果未通过ID、重复项或数量校验。请只输出有效JSON，最多返回指定数量。'}]
            task = 'entity-core:' + key + ':projection-' + str(projection_level)
            if batch_id != 'full':
                task += ':' + batch_id
            raw = caller.call(task + ':attempt:' + str(attempt + 1),
                'summary', request_messages, role='indexer')
            result = _parse_core(raw['generation']['text'], candidates, keep)
            if result is not None:
                return result, {'model_calls': 1, 'model_attempts': attempt + 1,
                                'projection_level': projection_level,
                                'budget': budget}
        raise NarrativeError('entity_core_selection_invalid',
            'core entity selection failed validation; raw response retained')
    return None, {'model_calls': 0, 'model_attempts': 0,
                  'projection_level': 3, 'budget': last_budget}


def _pack_core_batches(spec, caller, l1, l2, nearby, candidates, keep):
    last_budget = None
    for level in range(4):
        batches, current = [], []
        for candidate in candidates:
            trial = current + [candidate]
            messages = _core_messages(spec, l1, l2, nearby, trial, keep,
                                      projection_level=level)
            last_budget = plan_request(messages, spec.capacity, spec.roles['indexer'], caller.counter)
            if last_budget['fits']:
                current = trial
                continue
            if current:
                batches.append(current)
                current = [candidate]
                single = _core_messages(spec, l1, l2, nearby, current, keep,
                                        projection_level=level)
                last_budget = plan_request(single, spec.capacity,
                                           spec.roles['indexer'], caller.counter)
                if not last_budget['fits']:
                    break
            else:
                break
        else:
            if current:
                batches.append(current)
            if len(batches) <= keep:
                return level, batches
    raise NarrativeError('entity_core_context_exceeded',
        'complete core entity set cannot be ranked within request capacity; candidates=' +
        str(len(candidates)) + '; last_budget=' + str(last_budget))


def _batch_quotas(batches, keep):
    if len(batches) > keep:
        raise NarrativeError('entity_core_context_exceeded',
            'candidate batches exceed the number of available core slots')
    quotas = [1] * len(batches)
    remaining = keep - len(batches)
    capacities = [max(0, len(batch) - 1) for batch in batches]
    total = sum(capacities)
    if remaining <= 0 or not total:
        return quotas
    shares = [remaining * count / total for count in capacities]
    for i, share in enumerate(shares):
        quotas[i] += min(capacities[i], int(share))
    remaining = keep - sum(quotas)
    order = sorted(range(len(batches)),
                   key=lambda i: (shares[i] % 1, capacities[i]), reverse=True)
    while remaining:
        progressed = False
        for i in order:
            if quotas[i] < len(batches[i]):
                quotas[i] += 1
                remaining -= 1
                progressed = True
                if not remaining:
                    break
        if not progressed:
            break
    return quotas


def update_entity_catalog(repo, spec, view, caller, nodes):
    """Update only changed same-name groups and rebuild the bounded core view."""
    nodes, allowed_source_ids = _visible_entity_nodes(nodes, view)
    visible_source_ids = {source_id for node in nodes for source_id in
                          node.get('source_ids', node.get('ordered_source_ids', []))}
    visibility_order = max((doc['order'] for doc in view.visible_sources()
                            if doc['chapter_id'] in allowed_source_ids), default=-1)
    rows = _candidate_rows(nodes)
    previous = latest_entity_catalog(repo, visible_source_ids=allowed_source_ids,
                                     before_order=view.target_order)
    previous_groups = previous[1].get('groups', {}) if previous else {}
    l1_summaries = _l1_context(nodes)
    grouped = {}
    for row in rows:
        # Same spelling does not imply the same kind: a person, spell, title,
        # or organization with an identical surface form must remain distinct.
        grouped.setdefault(_entity_group_key(row['name'], row['kind']), []).append(row)
    groups, merged = {}, []
    for key, members in sorted(grouped.items()):
        old = previous_groups.get(key)
        if old is None:
            # Read the previous name-only catalog shape when it is unambiguous.
            # If current observations distinguish multiple kinds, rebuilding
            # those groups from their source records is safer than carrying a
            # legacy mixed-kind merge into either one.
            name_key = normalize_name(members[0]['name'])
            current_kinds = {normalize_name(row['kind']) for row in rows
                             if normalize_name(row['name']) == name_key}
            legacy = previous_groups.get(name_key)
            legacy_kinds = {normalize_name(kind) for entity in (legacy or {}).get('entities', [])
                            for kind in _field(entity, 'kind')}
            if legacy and len(current_kinds) == 1 and legacy_kinds == current_kinds:
                old = legacy
        old_member_ids = set(old.get('member_ids', [])) if old else set()
        current_member_ids = {member for row in members for member in row['member_ids']}
        additions = []
        for row in members:
            new_ids = set(row['member_ids']) - old_member_ids
            if new_ids:
                additions.append({**row, 'member_ids': sorted(new_ids)})
        if old and not additions and current_member_ids <= old_member_ids:
            entries = old['entities']
        else:
            merge_inputs = []
            if old and old.get('entities'):
                prior = old['entities'][0]
                prior_fields = prior.get('fields', {})
                merge_inputs.append({'member_id': 'previous:' + digest(prior)[:24],
                    'member_ids': prior.get('member_ids', []),
                    'node_revisions': prior.get('node_revisions', []),
                    'source_ids': prior.get('source_ids', []),
                    'name': (prior_fields.get('name') or [''])[0],
                    'kind': (prior_fields.get('kind') or ['other'])[0],
                    'description': (prior_fields.get('description') or [''])[0],
                    'target_forms': prior_fields.get('target_form', []),
                    'evidence': prior.get('evidence', []),
                    'previous_entity_id': prior['id']})
            merge_inputs.extend(additions if old else members)
            result = _bounded_semantic_merge(repo, spec, caller, merge_inputs, l1_summaries)
            entries = [_entity_entry(result, merge_inputs)]
        member_signature = digest(sorted(current_member_ids))
        groups[key] = {'member_signature': member_signature, 'member_ids': sorted(current_member_ids),
                       'entities': entries}
        merged.extend(entries)
    merged.sort(key=lambda row: (normalize_name(_field(row, 'name')[0]), row['id']))
    core_ids, core_status = _select_core(repo, spec, caller, l1_summaries, nodes, merged, view)
    revision = (previous[1].get('revision', 0) + 1) if previous else 1
    # A cache hit changes the diagnostic status from "selected" to "cached"
    # and reports zero calls. Those runtime counters must not create a new
    # semantic catalog revision when the selected IDs and selector artifact
    # have not changed.
    fingerprint = digest({'groups': groups, 'core_ids': core_ids,
                          'core_selection_revision': core_status.get('revision_id')})
    if previous and previous[1].get('fingerprint') == fingerprint:
        return previous[0], previous[1]
    value = {'schema': 'sequence-entity-catalog-v1', 'run_id': repo.run_id,
        'revision': revision, 'fingerprint': fingerprint, 'groups': groups,
        'entities': merged, 'core_entity_ids': core_ids, 'core_status': core_status,
        'visible_source_ids': sorted(visible_source_ids), 'visibility_order': visibility_order,
        'raw_observation_count': len(rows), 'deduplicated_entity_count': len(merged),
        'semantic_status': 'candidate_unreviewed'}
    ident = repo.store.put('sequence-entity-catalog', value)
    return ident, value


def _entity_group_key(name, kind):
    return normalize_name(name) + '\x1f' + normalize_name(kind or 'other')


def _entity_prompt_card(entity):
    """Bound the actual translation prompt card without changing stored facts."""
    fields = entity.get('fields', {})

    def values(name, *, limit, item_chars):
        result = []
        for value in fields.get(name, []):
            if not isinstance(value, str) or not value:
                continue
            result.append(value[:item_chars])
            if len(result) >= limit:
                break
        return result

    projected_fields = {
        'name': values('name', limit=1, item_chars=160),
        'kind': values('kind', limit=1, item_chars=64),
        'description': values('description', limit=1, item_chars=320),
        'target_form': values('target_form', limit=8, item_chars=80),
        'ambiguity': values('ambiguity', limit=1, item_chars=120),
    }
    return {'id': entity['id'], 'type': 'ENTITY', 'fields': projected_fields,
        'authority': entity.get('authority', 'candidate_unreviewed'),
        'semantic_status': entity.get('semantic_status', 'candidate_unreviewed'),
        'prompt_projection': 'entity-card-v1'}


def entity_catalog_for_prompt(repo, nodes, fallback_records=()):
    visible_source_ids = {source_id for node in nodes for source_id in
                          node.get('source_ids', node.get('ordered_source_ids', []))}
    catalog_row = latest_entity_catalog(repo, visible_source_ids=visible_source_ids)
    if catalog_row:
        catalog_id, catalog = catalog_row
        by_id = {row['id']: row for row in catalog.get('entities', [])}
        return catalog_id, [_entity_prompt_card(by_id[ident])
                            for ident in catalog.get('core_entity_ids', []) if ident in by_id]
    # Before any semantic catalog materializes, at least collapse exact copy
    # rows; a missing catalog never deletes the original candidate evidence.
    seen, result = set(), []
    for row in fallback_records:
        key = digest({'type': row.get('type'), 'fields': row.get('fields')})
        if key not in seen:
            seen.add(key)
            result.append(_entity_prompt_card(row))
    return None, result


def read_entity_catalog(repo, *, visible_source_ids=None, before_order=None):
    row = latest_entity_catalog(repo, visible_source_ids=visible_source_ids,
                                before_order=before_order)
    if not row:
        return {'status': 'not_built', 'entity_count': 0, 'entities': [], 'core_entity_ids': []}
    ident, value = row
    # The catalog is intended for inspection and planning, not as a bulk
    # evidence export. Full member provenance and excerpts remain available
    # through read_entity_evidence() by stable entity ID.
    entities = []
    for entity in value['entities']:
        compact = {key: field for key, field in entity.items()
                   if key not in {'member_ids', 'node_revisions', 'evidence', 'source_ids'}}
        compact['member_count'] = len(entity.get('member_ids', []))
        compact['evidence_count'] = len(entity.get('evidence', []))
        compact['source_count'] = len(entity.get('source_ids', []))
        entities.append(compact)
    return {'status': 'found', 'catalog_id': ident, 'revision': value['revision'],
        'entity_count': len(value['entities']), 'core_entity_ids': value['core_entity_ids'],
        'core_status': value['core_status'], 'entities': entities,
        'raw_observation_count': value['raw_observation_count'],
        'semantic_status': value['semantic_status']}


def read_entity_evidence(repo, entity_id, *, visible_source_ids=None, before_order=None):
    row = latest_entity_catalog(repo, visible_source_ids=visible_source_ids,
                                before_order=before_order)
    if not row:
        return {'status': 'not_found', 'entity_id': entity_id}
    for entity in row[1].get('entities', []):
        if entity['id'] == entity_id:
            return {'status': 'found', 'entity': entity}
    return {'status': 'not_found', 'entity_id': entity_id}


def _bounded_semantic_merge(repo, spec, caller, rows, l1_summaries):
    """Merge a long name group in token-budgeted steps, preserving member IDs."""
    if len(rows) <= 1:
        if rows and rows[0].get('previous_entity_id'):
            row = rows[0]
            fields = {'name': [row['name']], 'kind': [row['kind']],
                'description': [row['description']], 'target_form': row['target_forms']}
            return {'name': row['name'], 'kind': row['kind'], 'description': row['description'],
                'target_forms': row['target_forms'], 'member_ids': row['member_ids'], 'ambiguity': ''}
        return _semantic_merge(repo, spec, caller, rows, l1_summaries)
    pending = list(rows)
    merged = []
    current = []
    for row in pending:
        trial = current + [row]
        messages = _merge_prompt(trial, l1_summaries)
        report = plan_request(messages, spec.capacity, spec.roles['indexer'], caller.counter)
        if current and not report['fits']:
            merged.append(_semantic_merge(repo, spec, caller, current, l1_summaries))
            current = [row]
        else:
            current = trial
        single = _merge_prompt(current, l1_summaries)
        if not plan_request(single, spec.capacity, spec.roles['indexer'], caller.counter)['fits']:
            raise NarrativeError('entity_merge_context_exceeded',
                'one entity observation and shared L1 prefix exceed request capacity')
    if current:
        merged.append(_semantic_merge(repo, spec, caller, current, l1_summaries))
    while len(merged) > 1:
        combined = []
        for start in range(0, len(merged), 2):
            pair = merged[start:start + 2]
            if len(pair) == 1:
                combined.append(pair[0])
                continue
            pseudo = []
            for result in pair:
                pseudo.append({'member_id': 'aggregate:' + digest(result)[:24],
                    'member_ids': result['member_ids'], 'node_revisions': result.get('node_revisions', []),
                    'source_ids': result.get('source_ids', []), 'name': result['name'],
                    'kind': result['kind'], 'description': result['description'],
                    'target_forms': result['target_forms'], 'evidence': result.get('evidence', [])})
            messages = _merge_prompt(pseudo, l1_summaries)
            if not plan_request(messages, spec.capacity, spec.roles['indexer'], caller.counter)['fits']:
                combined.extend(pair)
                continue
            combined.append(_semantic_merge(repo, spec, caller, pseudo, l1_summaries))
        if len(combined) >= len(merged):
            # Preserve a bounded list of grouped records rather than silently
            # dropping groups when the shared prompt leaves no merge capacity.
            raise NarrativeError('entity_merge_context_exceeded',
                'semantic merge rollup cannot fit with the shared L1 prefix')
        merged = combined
    return merged[0]
