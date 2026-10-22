"""Direct L1 summaries; child rollup is a capacity fallback, never a prerequisite."""
from ..budget import RoleBudget, plan_request, is_context_overflow
from ..providers import ProviderError
from ..storage import digest, read_json, write_json
from .prompts import summary_messages, j
from .protocol import parse_summary


def context_overflow(error):
    return is_context_overflow(error)


def read_parent(pipeline, parent_id):
    parent = pipeline.nodes[parent_id]
    pipeline.archive.validate_dependencies(parent['dependencies'])
    path = pipeline.index_source / 'parents' / (parent_id + '.json')
    if not path.exists():
        return None
    artifact = read_json(path)
    if artifact.get('schema') == 'edition-parent':
        payload = {k: v for k, v in artifact.items() if k != 'fingerprint'}
        if artifact['fingerprint'] != digest(payload) or artifact['node'] != parent:
            raise ValueError('stale/corrupted parent summary')
        pipeline.archive.validate_dependencies(artifact['dependencies'])
        # Direct summaries remain valid when a lazy child is added later.
        if artifact['build_method'] == 'child_summary_rollup':
            for nid, fingerprint in artifact['child_revisions'].items():
                child = pipeline.read_summary(nid)
                if not child or child['fingerprint'] != fingerprint:
                    raise ValueError('parent child dependency changed')
    return artifact


def save_parent(pipeline, artifact):
    artifact = {'schema': 'edition-parent', **artifact}
    artifact['fingerprint'] = digest(artifact)
    write_json(pipeline.folder / 'parents' / 'revisions' / (artifact['fingerprint'] + '.json'), artifact)
    write_json(pipeline.folder / 'parents' / (artifact['parent_id'] + '.json'), artifact)
    return artifact


def build_parent(pipeline, parent_id, *, allow_partial=False, output_tokens=8192,
                 method=None):
    method = method or pipeline.nodes[parent_id].get('build_method', 'direct')
    if method not in {'direct_then_rollup', 'direct', 'child_summary_rollup'}:
        raise ValueError('unknown parent build method')
    parent = pipeline.nodes[parent_id]
    if parent['level'] != 1:
        raise ValueError('parent node required')
    old = read_parent(pipeline, parent_id)
    if old and old.get('coverage_complete', True) and (method == 'direct_then_rollup' or old['build_method'] == method):
        return {**old, 'cache_hit': True}
    if method == 'direct':
        plan = next((p for p in pipeline.plans if parent_id in
                     {n['node_id'] for n in p.get('parents', [])}), None)
        planning = plan.get('planning', {}) if plan else {}
        parent_ids = set(parent['entry_ids'])
        diagnostics = [d for d in planning.get('diagnostics', [])
                       if d.get('kind') in {'minimum_l1_does_not_fit', 'short_l1_range',
                                            'oversized_single_chapter'}
                       and parent_ids.intersection(d.get('entry_ids',
                           [d.get('entry_id')] if d.get('entry_id') else []))]
        if diagnostics:
            return {'status': 'index_capacity_decision_required', 'node_id': parent_id,
                'not_sent': True, 'diagnostics': diagnostics,
                'suggested_actions': ['revise_index_capacity_or_l1_minimum',
                                      'explicit_child_summary_rollup',
                                      'explicit_direct_then_rollup']}
    reason = None
    if method != 'child_summary_rollup':
        docs = [pipeline.archive.read(eid) for eid in parent['entry_ids']]
        messages = summary_messages(parent, docs)
        budget = plan_request(messages, pipeline.capacity, RoleBudget(output_tokens), pipeline.counter)
        if not budget['fits']:
            if budget['reasons'] != ['context_limit']:
                return {'status': 'blocked', 'reason': 'output_limit', 'budget': budget}
            reason = {'kind': 'local_context_limit', 'budget': budget}
        else:
            try:
                outcome = pipeline.caller.structured('edition-index:' + parent_id + ':direct',
                    messages, output_tokens, lambda text: parse_summary(text, parent, pipeline.archive.refs(parent['entry_ids'])))
            except ProviderError as error:
                if not context_overflow(error):
                    raise
                reason = {'kind': 'remote_context_limit'}
            else:
                if outcome['status'] != 'complete':
                    return {'node_id': parent_id, 'build_method': 'direct', **outcome}
                content = outcome['result']
                complete = content['coverage'].get('processed_all') == 'yes' and content['coverage'].get('omissions', '').strip() in {'', '无', '无。', 'none'}
                artifact = save_parent(pipeline, {'node': parent, 'parent_id': parent_id,
                    'overview': content['overview'], 'content': content, 'build_method': 'direct',
                    'status': 'complete' if complete else 'partial', 'coverage_complete': complete,
                    'covered_entry_ids': list(parent['entry_ids']) if complete else [],
                    'dependencies': parent['dependencies'], 'child_revisions': {}, 'child_fingerprints': [],
                    'generation_id': outcome['generation_id'], 'attempts': outcome['attempts'],
                    'budget': budget, 'semantic_status': 'model_extracted_unreviewed'})
                return artifact
        if method == 'direct':
            return {'status': 'blocked_context', 'node_id': parent_id, 'fallback_reason': reason}
        built = pipeline.ensure_index_nodes(parent['child_ids'], workers=1, output_tokens=output_tokens)
        if not built['complete'] and not allow_partial:
            return {'status': 'incomplete', 'fallback_reason': reason, 'children': built}
    elif method == 'child_summary_rollup':
        built = pipeline.ensure_index_nodes(parent['child_ids'], workers=1,
                                            output_tokens=output_tokens)
        if not built['complete'] and not allow_partial:
            return {'status': 'incomplete', 'children': built}
    catalog = pipeline.entity_catalog(parent_id)
    if catalog['missing_child_ids'] and not allow_partial:
        return {'status': 'not_built', 'missing_child_ids': catalog['missing_child_ids']}
    children = [pipeline.read_summary(nid) for nid in catalog['covered_child_ids']]
    if not children:
        return {'status': 'not_built', 'missing_child_ids': parent['child_ids']}
    messages = [{'role': 'system', 'content': '根据同一版本子块摘要生成父块概括，软目标2500字。保留回忆、因果、知情差异和覆盖缺口，不补造。只输出概括正文。'},
        {'role': 'user', 'content': j({'edition_id': parent['edition_id'],
            'coverage': {k: v for k, v in catalog.items() if k != 'entities'},
            'children': [c['content'] for c in children]})}]
    raw = pipeline.caller.call('edition-parent:' + parent_id + ':rollup', messages, output_tokens)
    covered = [eid for c in children for eid in c['node']['entry_ids']]
    complete = not catalog['missing_child_ids']
    return save_parent(pipeline, {'node': parent, 'parent_id': parent_id,
        'overview': raw['generation']['text'], 'build_method': 'child_summary_rollup',
        'status': 'complete' if complete else 'partial', 'coverage_complete': complete,
        'covered_entry_ids': covered, 'entity_catalog': catalog,
        'dependencies': [d for d in parent['dependencies'] if d['entry_id'] in covered],
        'child_revisions': {c['node']['node_id']: c['fingerprint'] for c in children},
        'child_fingerprints': [c['fingerprint'] for c in children],
        'fallback_reason': reason, 'generation_id': raw['id'], 'semantic_status': 'model_extracted_unreviewed'})
