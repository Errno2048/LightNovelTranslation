"""Reference packets preserve edition boundaries through translation and review."""
from copy import deepcopy

from ..storage import digest
from .archive import document_dependency
from .prompts import j, translation_messages


def _document(doc):
    return {k: doc[k] for k in ('entry_id', 'edition_id', 'revision_id', 'raw_title', 'rows')}


def compile_reference_packet(pipeline, target_id, associations=(), entity_matches=(), *,
                             visibility='history_only', token_budget=48000, n_chapter=3,
                             allowed_reference_entry_ids=(), style_mode='full', radius=2,
                             terminology_resolution=None, local_evidence_policy='event-only-v1'):
    if visibility not in {'history_only', 'reference_assisted'} or style_mode not in {'full', 'local', 'none'}:
        raise ValueError('unknown visibility/style policy')
    archive, counter = pipeline.archive, pipeline.counter
    target = archive.read(target_id)
    nodes = pipeline.nodes
    allowed = set(allowed_reference_entry_ids)
    packet = {'schema': 'edition-reference-packet-v1', 'reference_mode': 'cross_edition',
              'visibility_policy': visibility, 'target_id': target_id, 'source_history': [],
              'parallel_examples': [], 'terminology': [], 'style_documents': [], 'navigation': [],
              'dependencies': [document_dependency(target)], 'omissions': [],
              'reference_exposure': 'may_cover_target_or_future' if visibility == 'reference_assisted' else 'certified_history_only'}
    dependencies = {target_id: document_dependency(target)}
    history = [d for d in archive.documents(target['edition_id']) if d['edition_order'] < target['edition_order']]
    for doc in history[-n_chapter:] if n_chapter else []:
        packet['source_history'].append(_document(doc))
        dependencies[doc['entry_id']] = document_dependency(doc)
    represented = {d['entry_id'] for d in packet['source_history']}
    for node in sorted(nodes.values(), key=lambda n: n['reading_range']):
        if node['level'] != 2 or node['edition_id'] != target['edition_id'] or node['reading_range'][1] >= target['edition_order']:
            continue
        if set(node['entry_ids']) <= represented:
            continue
        summary = pipeline.read_summary(node['node_id'])
        if summary:
            packet['navigation'].append({'edition_id': node['edition_id'], 'node_id': node['node_id'],
                'overview': summary['content']['overview'], 'authority': 'web_navigation_unreviewed'})
            dependencies.update({d['entry_id']: d for d in node['dependencies']})
    conflict_forms = {c['source_form'] for m in entity_matches for c in m['conflicts']}
    registry = {r['ref']: r for e in archive.entries for r in archive.read(e)['rows']}
    for match in entity_matches:
        source_entities = {e['id']: e for e in match['source_catalog']['entities']}
        for row in match['mappings']:
            if row['status'] != 'supported' or row['source_form'] not in target['text'] or row['source_form'] in conflict_forms:
                continue
            sr, rr = registry.get(row['source_usage_ref']), registry.get(row['reference_usage_ref'])
            entity_node = nodes[source_entities[row['source_entity']]['node_id']]
            if not sr or not rr:
                continue
            source_visible = archive.read(sr['entry_id'])['edition_order'] <= target['edition_order']
            if visibility == 'history_only' and (not source_visible or
                    entity_node['reading_range'][1] >= target['edition_order'] or rr['entry_id'] not in allowed):
                packet['omissions'].append({'kind': 'terminology', 'source_form': row['source_form'], 'reason': 'visibility_unproven'})
                continue
            term = {k: row[k] for k in ('source_form', 'target_form', 'source_description', 'reason')}
            term.update({k: row[k] for k in ('reference_rendering', 'rendering_kind') if k in row})
            term.update(source_usage=sr, reference_usage=rr, authority='observed_usage_candidate',
                        description_scope='whole_extraction_block')
            if term not in packet['terminology']:
                packet['terminology'].append(term)
            for ref in (sr, rr):
                dependencies[ref['entry_id']] = document_dependency(archive.read(ref['entry_id']))
            dependencies.update({d['entry_id']: d for d in entity_node['dependencies']})
    if terminology_resolution:
        resolution = terminology_resolution
        if (resolution.get('target_id') != target_id or resolution.get('fingerprint') !=
                digest({k:v for k,v in resolution.items() if k != 'fingerprint'})):
            raise ValueError('terminology resolution differs from frozen target/artifact')
        archive.validate_dependencies(resolution['dependencies'])
        packet['terminology_resolution_fingerprint'] = resolution['fingerprint']
        packet['terminology_trace'] = deepcopy(resolution['trace'])
        dependencies.update({d['entry_id']:d for d in resolution['dependencies']})
        for row in resolution.get('items', []) if resolution.get('status') == 'complete' else []:
            if row['status'] != 'candidate': continue
            sr, rr = registry.get(row.get('source_ref')), registry.get(row.get('reference_ref'))
            if (not sr or not rr or row['source_form'] not in sr['text'] or row['target_form'] not in rr['text'] or
                    sr['entry_id'] != target_id or rr['entry_id'] not in resolution.get('search_scope', [])):
                raise ValueError('terminology candidate is not bound to its actual lookup prose')
            if row['source_form'] in conflict_forms or visibility == 'history_only' and rr['entry_id'] not in allowed:
                packet['omissions'].append({'kind':'terminology', 'source_form':row['source_form'],
                    'reason':'prior_candidate_conflict' if row['source_form'] in conflict_forms else 'visibility_unproven'})
                continue
            packet['terminology'] = [term for term in packet['terminology'] if term['source_form'] != row['source_form']]
            packet['terminology'].append({'source_form':row['source_form'], 'target_form':row['target_form'],
                'source_description':row['description'], 'reason':row['reason'], 'rendering_kind':row['rendering_kind'],
                'reference_rendering':row['target_form'], 'source_usage':sr, 'reference_usage':rr,
                'authority':'observed_usage_candidate', 'description_scope':'current_source_local_prose',
                'lookup_fingerprint':resolution['fingerprint']})
    selected_ids, priorities, event_anchors = set(), {}, {}
    association_notes = []
    for association in associations:
        if association['status'] != 'complete':
            continue
        for row in association['result']['associations']:
            if row['relation'] != 'event_overlap' or target_id not in nodes[row['source_node']]['entry_ids']:
                continue
            selected_ids.add(row['reference_node'])
            if visibility == 'reference_assisted':
                association_notes.append({k: row.get(k) for k in
                    ('source_node', 'reference_node', 'reason', 'differences', 'evidence_status')})
                for member in (row['source_node'], row['reference_node']):
                    dependencies.update({d['entry_id']: d for d in nodes[member]['dependencies']})
            source_summary = pipeline.read_summary(row['source_node'])
            reference_summary = pipeline.read_summary(row['reference_node'])
            source_events = {e['id']: e for e in source_summary['content']['events']} if source_summary else {}
            reference_events = {e['id']: e for e in reference_summary['content']['events']} if reference_summary else {}
            for event_pair in [row] + row.get('additional_event_matches', []):
                if event_pair.get('relation') != 'event_overlap':
                    continue
                se = source_events.get(event_pair.get('source_event'), {})
                re = reference_events.get(event_pair.get('reference_event'), {})
                sr, rr = registry.get(se.get('evidence_ref')), registry.get(re.get('evidence_ref'))
                if rr:
                    priority = 0 if sr and sr['entry_id'] == target_id else 1
                    eid = rr['entry_id']
                    priorities[eid] = min(priorities.get(eid, 2), priority)
                    event_anchors.setdefault(eid, set()).add(rr['ref'])
    if association_notes:
        packet['block_associations'] = {'authority': 'unreviewed_cross_edition_navigation',
                                        'items': association_notes}
        packet['reference_selection_policy'] = 'event-evidence-priority-v1'

    def local_rows(doc):
        rows = doc['rows']
        anchors = event_anchors.get(doc['entry_id'], set())
        if anchors:
            hits = [i for i, r in enumerate(rows) if r['ref'] in anchors]
        else:
            terms = [t['target_form'] for t in packet['terminology']]
            hits = [i for i, r in enumerate(rows) if any(t in r['text'] for t in terms)]
        if local_evidence_policy == 'event-and-terms-v2':
            term_refs = {t['reference_usage']['ref'] for t in packet['terminology']}
            hits += [i for i,r in enumerate(rows) if r['ref'] in term_refs or
                     any(t['target_form'] in r['text'] for t in packet['terminology'])]
        keep = {i for hit in hits for i in range(max(0, hit-radius), min(len(rows), hit+radius+1))}
        return [r for i, r in enumerate(rows) if i in keep]
    if style_mode != 'none':
        seen = set()
        for nid in sorted(selected_ids):
            for eid in nodes[nid]['entry_ids']:
                if eid in seen:
                    continue
                seen.add(eid)
                if visibility == 'history_only' and eid not in allowed:
                    packet['omissions'].append({'kind': 'style_document', 'entry_id': eid, 'reason': 'visibility_unproven'})
                    continue
                doc = archive.read(eid)
                rows = doc['rows']
                if style_mode == 'local':
                    rows = local_rows(doc)
                if not rows:
                    packet['omissions'].append({'kind': 'style_document', 'entry_id': eid, 'reason': 'no_local_term_hit'})
                    continue
                packet['style_documents'].append({**_document(doc), 'rows': rows, 'node_id': nid,
                    'allowed_uses': ['style_reference'], 'semantic_status': 'cross_edition_not_parallel'})
                if len(rows) < len(doc['rows']):
                    packet['style_documents'][-1].update(body_selection='local_evidence',
                        omitted_paragraphs=len(doc['rows']) - len(rows))
                dependencies[eid] = document_dependency(doc)
            # Association selection depends on both complete summary inputs.
            for association in associations:
                if nid in association['reference_node_ids']:
                    for member in association['source_node_ids'] + association['reference_node_ids']:
                        dependencies.update({d['entry_id']: d for d in nodes[member]['dependencies']})

    # Whole-block references often exceed budget. Keep the chapter containing
    # the target's matched event before adjacent material, rather than retaining
    # a chronological prefix that can omit the event being referenced.
    term_entries = {t['reference_usage']['entry_id'] for t in packet['terminology']}
    packet['style_documents'].sort(key=lambda d: (priorities.get(d['entry_id'],
        2 if d['entry_id'] in term_entries else 3), d['node_id'],
        archive.entries[d['entry_id']]['edition_order']))

    packet['association_fingerprints'] = [a['fingerprint'] for a in associations]
    packet['entity_match_fingerprints'] = [m['fingerprint'] for m in entity_matches]
    if local_evidence_policy != 'event-only-v1':
        packet['local_evidence_policy'] = local_evidence_policy

    def fits():
        return counter.messages(translation_messages(packet, target)) <= token_budget

    # Trim optional references first. Source history is required and must not be
    # silently discarded to make room for a translated edition.
    while not fits() and packet['style_documents']:
        candidate = packet['style_documents'][-1]
        if style_mode == 'full' and 'body_selection' not in candidate:
            excerpt = local_rows(candidate)
            if 0 < len(excerpt) < len(candidate['rows']):
                candidate.update(body_selection='local_evidence',
                    omitted_paragraphs=len(candidate['rows']) - len(excerpt), rows=excerpt)
                packet['omissions'].append({'kind': 'style_document', 'entry_id': candidate['entry_id'],
                                           'reason': 'full_to_local_evidence'})
                continue
        removed = packet['style_documents'].pop()
        packet['omissions'].append({'kind': 'style_document', 'entry_id': removed['entry_id'], 'reason': 'token_budget'})
    while not fits() and packet['terminology']:
        removed = packet['terminology'].pop()
        packet['omissions'].append({'kind': 'terminology', 'source_form': removed['source_form'], 'reason': 'token_budget'})
    while not fits() and packet['navigation']:
        removed = packet['navigation'].pop(0)
        packet['omissions'].append({'kind': 'navigation', 'node_id': removed['node_id'], 'reason': 'token_budget'})
    if not fits():
        raise ValueError('required target and near source history exceed context budget')
    packet['dependencies'] = list(dependencies.values())
    packet['fingerprint'] = digest(packet)
    return packet


def review_reference_packet(packet, *, include_body=True):
    """Review and adjudication receive the identical purpose-scoped projection."""
    result = deepcopy(packet)
    result['consumer'] = 'review_or_adjudication'
    if not include_body:
        # The review payload has one shared alias->text registry. Keep edition
        # and authority metadata here without paying twice for every paragraph.
        for key in ('source_history', 'style_documents'):
            for doc in result[key]:
                doc['rows'] = [{'ref': r['ref'], 'paragraph_id': r['paragraph_id']} for r in doc['rows']]
        for term in result['terminology']:
            for key in ('source_usage', 'reference_usage'):
                term[key] = {k: term[key][k] for k in ('ref', 'paragraph_id', 'entry_id', 'edition_id')}
    return result
