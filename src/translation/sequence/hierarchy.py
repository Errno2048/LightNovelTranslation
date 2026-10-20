"""History bootstrap, immutable L1/L2 revisions and bounded lazy refinement."""
import json

from ..budget import plan_request, calibration_profile, read_calibration, is_context_overflow
from ..mvp.recovery import ExtractionRecovery
from ..narrative.schemas import NarrativeError
from ..providers import ProviderError
from ..narrative.service import dependency
from ..storage import digest
from .requests import sequence_limits, SequenceCaller

PROFILE = 'hierarchy-v2'


def enabled(repo):
    return repo.manifest['inputs'].get('history_index_profile') in {PROFILE, 'hierarchy-v1'}


def usable(node):
    return bool(node.get('summary') and node.get('state') in {'extracted', 'partial'})


def _layout(repo, spec, view):
    """Fix boundaries once; future bodies are only sized locally, never extracted."""
    from .knowledge import joint_messages
    for _, saved in repo.store.objects('sequence-hierarchy-plan'):
        if saved['run_id'] == repo.run_id:
            return saved
    inputs = repo.manifest['inputs']
    sources = {d['chapter_id']: d for d in inputs.get('history_sources', []) + inputs['targets']}
    historical = {p['source']['chapter_id']: p for p in inputs.get('history_pairs', [])}
    sources.update({cid: p['source'] for cid, p in historical.items()})
    for node in inputs.get('history_nodes', []):
        sources.update({d['chapter_id']: d for d in node['dependencies'] if d['language'] == 'ja'})
    pairs = [historical.get(d['chapter_id'], {'source': d, 'translation': []})
             for d in sorted(sources.values(), key=lambda d: d['order'])]
    groups, diagnostics = [], []
    expected_l1_count = 0

    def pair_tokens(pair):
        docs = [pair['source']] + pair.get('translation', [])
        return sum(view.counter.count(doc.get('title', '') + '\n' + doc.get('text', '')) for doc in docs)

    def l1_report(members):
        messages, _ = joint_messages(members, [], [], reference_profile=spec.reference_profile)
        report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
        return report, report['fits'] and report['safe_input_tokens'] <= \
            report['input_limit'] * spec.planning_utilization

    sections, section = [], []
    for pair in pairs:
        if section and pair['source']['order'] != section[-1]['source']['order'] + 1:
            sections.append(section)
            section = []
        section.append(pair)
    if section:
        sections.append(section)
    for segment in sections:
        minimum = min(spec.parent_min_chapters, spec.parent_max_chapters)
        expected_l1_count += max(1, round(len(segment) / minimum))
        cursor = 0
        while cursor < len(segment):
            remaining = segment[cursor:]
            if spec.index_layout == 'token-v2' and len(remaining) >= minimum:
                report, fits = l1_report(remaining[:minimum])
                if not fits:
                    diagnostics.append({'kind': 'minimum_l1_does_not_fit',
                        'source_ids': [p['source']['chapter_id'] for p in remaining[:minimum]],
                        'minimum_chapters': minimum, 'budget': report,
                        'suggested_actions': ['explicit_child_summary_rollup',
                                              'revise_index_capacity_or_l1_minimum']})
            members = []
            for pair in remaining[:spec.parent_max_chapters]:
                trial = members + [pair]
                if spec.index_layout == 'token-v2':
                    _, fits = l1_report(trial)
                    if members and not fits:
                        break
                else:
                    fits = True
                members.append(pair)
                if spec.index_layout != 'token-v2' and len(members) >= spec.parent_max_chapters:
                    break
            if not members:
                raise NarrativeError('index_planning_failed', 'token planner failed to advance')
            if (spec.index_layout == 'token-v2' and len(members) < minimum and
                    len(remaining) >= minimum):
                report, _ = l1_report(members)
                diagnostics.append({'kind': 'short_l1_before_minimum',
                    'source_ids': [p['source']['chapter_id'] for p in members],
                    'minimum_chapters': minimum, 'budget': report})
            groups.append(members)
            cursor += len(members)

    nodes, roots = [], []
    for gi, members in enumerate(groups):
        rid = 'hierarchy-parent:' + digest({'run': repo.run_id, 'start': members[0]['source']['chapter_id']})[:24]
        children, current = [], []
        expected_children = min(spec.expected_l2_count,
            max(1, round(len(members) / spec.l2_prepartition_min_chapters)))
        target_child_tokens = max(1, sum(pair_tokens(pair) for pair in members) // expected_children)
        current_tokens = 0

        def flush():
            if current:
                children.append(list(current))
                current.clear()

        for pair in members:
            trial = current + [pair]
            messages, _ = joint_messages(trial, [], [], reference_profile=spec.reference_profile)
            report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
            target_reached = (spec.index_layout == 'token-v2' and current and
                len(children) < expected_children - 1 and current_tokens >= target_child_tokens)
            if current and (target_reached or len(current) >= spec.child_max_chapters or
                            not report['fits'] or report['safe_input_tokens'] >
                            report['input_limit'] * spec.planning_utilization):
                flush()
                current_tokens = 0
                messages, _ = joint_messages([pair], [], [], reference_profile=spec.reference_profile)
                report = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter)
            # An oversized child is diagnosed only if actually needed; its
            # existence must not prevent trying a direct parent request.
            current.append(pair)
            current_tokens += pair_tokens(pair)
        flush()
        root = {'node_id': rid, 'parent_id': None, 'level': 1,
            'ordered_source_ids': [p['source']['chapter_id'] for p in members],
            'start_position': members[0]['source']['order'], 'end_position': members[-1]['source']['order'],
            'state': 'sealed' if len(members) == spec.parent_max_chapters or gi < len(groups)-1 else 'open',
            'expected_l2_count': expected_children,
            'short_range': len(members) < min(spec.parent_min_chapters, spec.parent_max_chapters),
            'children': [], 'preferred_build_method': spec.parent_build_mode,
            'layout': spec.parent_build_mode}
        roots.append(rid)
        nodes.append(root)
        for ci, child in enumerate(children):
            cid = 'hierarchy-child:' + digest({'parent': rid, 'start': child[0]['source']['chapter_id']})[:24]
            root['children'].append(cid)
            nodes.append({'node_id': cid, 'parent_id': rid, 'level': 2,
                'ordered_source_ids': [p['source']['chapter_id'] for p in child],
                'start_position': child[0]['source']['order'], 'end_position': child[-1]['source']['order'],
                'state': 'sealed' if ci < len(children)-1 or root['state'] == 'sealed' or
                         len(child) == spec.child_max_chapters else 'open',
                'prepartition_minimum_chapters': spec.l2_prepartition_min_chapters,
                'minimum_exception': spec.index_layout == 'token-v2' and
                    len(child) < spec.l2_prepartition_min_chapters})
    value = {'schema': PROFILE, 'run_id': repo.run_id, 'roots': roots, 'nodes': nodes,
            'ordered_source_ids': [p['source']['chapter_id'] for p in pairs],
            'planning': {'status': 'capacity_decision_required' if diagnostics else 'ready',
                         'diagnostics': diagnostics, 'expected_l1_count': expected_l1_count,
                         'minimum_l1_chapters': min(spec.parent_min_chapters, spec.parent_max_chapters),
                         'maximum_l1_chapters': spec.parent_max_chapters,
                         'expected_l2_count': spec.expected_l2_count,
                         'l2_prepartition_minimum_chapters': spec.l2_prepartition_min_chapters},
            'fingerprint': digest({'roots': roots, 'nodes': nodes})}
    repo.store.put('sequence-hierarchy-plan', value)
    return value


def plan_hierarchy(repo, spec, view):
    layout = _layout(repo, spec, view)
    # This hierarchy is the frozen seed history. Branch commits use the
    # append-only sequence-node path and cannot shift these boundaries.
    inputs=repo.manifest['inputs']
    seed_ids={d['chapter_id'] for d in inputs.get('history_sources', [])}
    seed_ids.update(p['source']['chapter_id'] for p in inputs.get('history_pairs', []))
    seed_ids.update(cid for node in inputs.get('history_nodes', []) for cid in node['source_ids'])
    positions = {d['chapter_id']:d['order'] for d in view.visible_sources()
                 if d['order'] < view.target_order and d['chapter_id'] in seed_ids}
    visible = set(positions)
    nodes = []
    for node in layout['nodes']:
        ids = [cid for cid in node['ordered_source_ids'] if cid in visible]
        if ids:
            nodes.append({**node, 'ordered_source_ids': ids,
                'start_position':positions[ids[0]], 'end_position':positions[ids[-1]],
                'state': 'sealed' if ids == node['ordered_source_ids'] else 'open'})
    existing_ids = {n['node_id'] for n in nodes}
    for node in nodes:
        if node['level'] == 1:
            node['children'] = [cid for cid in node['children'] if cid in existing_ids]
    return {'schema': PROFILE, 'roots': [n['node_id'] for n in nodes if n['level'] == 1],
        'nodes': nodes,
        'ordered_source_ids': [cid for cid in layout['ordered_source_ids'] if cid in visible],
        'planning': layout.get('planning', {'status': 'ready', 'diagnostics': []})}


def latest_nodes(repo, *, allowed_source_ids=None):
    from .knowledge import node_is_valid
    allowed = set(allowed_source_ids) if allowed_source_ids is not None else None
    grouped = {}
    for ident, node in repo.store.objects('sequence-hierarchy-node'):
        if not usable(node) or (allowed is not None and not set(node['source_ids']) <= allowed):
            continue
        node_is_valid(repo, node)
        previous = grouped.get(node['node_id'])
        if previous is None or len(node['source_ids']) > len(previous['source_ids']):
            grouped[node['node_id']] = {'id': ident, **node}
    return sorted(grouped.values(), key=lambda n: (n['start_position'], n['level']))


def latest_incremental_nodes(repo, *, allowed_source_ids=None):
    """Return the newest usable append-only nodes for the current branch.

    Historical hierarchy nodes and branch ``sequence-node`` revisions use
    different stores on purpose.  Consumers still need one navigation view,
    so this helper applies the same visibility and revision rules to the
    incremental store without treating an absent node as a coverage gap.
    """
    from .knowledge import node_is_valid

    allowed = set(allowed_source_ids) if allowed_source_ids is not None else None
    grouped = {}
    for ident, node in repo.store.objects('sequence-node'):
        if node.get('run_id') != repo.run_id or not usable(node):
            continue
        source_ids = set(node.get('source_ids', ()))
        if allowed is not None and not source_ids <= allowed:
            continue
        node_is_valid(repo, node)
        previous = grouped.get(node.get('node_id'))
        if previous is None or (len(node['source_ids']), node.get('end_position', -1), ident) > \
                (len(previous['source_ids']), previous.get('end_position', -1), previous['id']):
            grouped[node['node_id']] = {'id': ident, **node}
    return sorted(grouped.values(), key=lambda n: (n['start_position'], n['end_position'], n['level']))


def _desired_ids(repo, node, plan, force_open=False):
    ids = node['ordered_source_ids']
    if node['state'] == 'sealed' or force_open:
        return ids
    first_order = repo.manifest['inputs']['targets'][0]['order']
    inputs = repo.manifest['inputs']
    sources = {d['chapter_id']: d for d in inputs.get('history_sources', [])}
    sources.update({p['source']['chapter_id']: p['source'] for p in inputs.get('history_pairs', [])})
    bootstrap = [cid for cid in ids if cid in sources and sources[cid]['order'] < first_order]
    if node['level'] == 2:
        return bootstrap
    sealed = [child for child in plan['nodes']
              if child.get('parent_id') == node['node_id'] and child['state'] == 'sealed']
    size = max([len(bootstrap)] +
               [ids.index(child['ordered_source_ids'][-1]) + 1 for child in sealed])
    return ids[:size]


def pending_report(repo, spec, view):
    plan = plan_hierarchy(repo, spec, view)
    ready = {n['node_id']: n for n in latest_nodes(repo, allowed_source_ids=plan['ordered_source_ids'])}
    full, near = history_window(repo, spec, view, plan)
    needed = [n for n in plan['nodes']
              if n['level'] == 1 and not set(n['ordered_source_ids']) <= full]
    needed += [n for n in plan['nodes'] if n['level'] == 2 and n['node_id'] in near]
    pending = []
    for node in needed:
        desired = _desired_ids(repo, node, plan)
        if desired and (node['node_id'] not in ready or
                        not set(desired) <= set(ready[node['node_id']]['source_ids'])):
            pending.append({'node_id': node['node_id'], 'level': node['level'],
                'source_ids': desired, 'reason': 'history_not_materialized',
                'preferred_build_method': 'direct'})
    planning = plan.get('planning', {'status': 'ready', 'diagnostics': []})
    requires_decision = (planning.get('status') == 'capacity_decision_required' and
                         spec.parent_build_mode == 'direct')
    return {'profile': PROFILE, 'pending': pending,
        'ready': not pending and not requires_decision,
        'requires_capacity_decision': requires_decision,
        'planning': planning,
        'index_revision': index_snapshot(repo),
        'nodes': plan['nodes'], 'generation_calls': 0, 'local_only': True}


def history_window(repo, spec, view, plan):
    """Locate near historical details relative to the advancing branch head."""
    allowed = set(plan['ordered_source_ids']) | {c['source_id'] for _, c in repo.all_commits()}
    ordered = [doc['chapter_id'] for doc in view.visible_sources()
               if doc['chapter_id'] in allowed and doc['order'] < view.target_order]
    full = set(ordered[-spec.n_chapter:])
    children = [node for node in plan['nodes'] if node['level'] == 2]
    children += latest_incremental_nodes(repo, allowed_source_ids=allowed)
    children = sorted((node for node in children if node['level'] == 2 and not
        set(node.get('ordered_source_ids', node.get('source_ids', []))) <= full),
        key=lambda node: (node['end_position'], node['start_position']))
    return full, {node['node_id'] for node in children[-spec.n_child:]}


def _extract(repo, spec, view, provider, node, ids, navigation=(), predecessors=()):
    from .knowledge import joint_messages, parser
    from ..narrative.extraction import PARSER_VERSION, STABLE_PARSER_VERSION
    pairs = view.read_chapters(ids)
    messages, refs = joint_messages(pairs, navigation, predecessors,
                                     reference_profile=spec.reference_profile)
    messages[-1]['content'] += ('\n本次节点层级为 L' + str(node['level']) +
        '；总结给定完整范围，不补写未提供的剧情；没有中文对照时不推测既有译名。')
    profile = calibration_profile(repo.manifest['provider'], view.counter, 'indexer')
    budget = plan_request(messages, spec.capacity, spec.roles['indexer'], view.counter,
        profile=profile, calibration=read_calibration(repo.store, profile))
    if not budget['fits']:
        if budget['reasons'] == ['context_limit']:
            return None, {'kind': 'local_context_limit', 'budget': budget}
        raise NarrativeError('budget_exceeded', 'index output reservation exceeds model capacity')
    recovery = ExtractionRecovery(repo.store, view.counter, provider,
        provider_identity=repo.manifest['provider'], limits=sequence_limits(spec),
        max_retries=spec.max_attempts - 1, capacity=spec.capacity,
        max_output_tokens=spec.capacity.max_output_tokens)
    try:
        result = recovery.run(node['node_id'] + ':' + digest(pairs), messages, parser(refs),
            parser_version=STABLE_PARSER_VERSION if spec.reference_profile == 'stable-v1' else PARSER_VERSION,
            max_tokens=spec.roles['indexer'].requested_output_tokens)
    except ProviderError as exc:
        if is_context_overflow(exc):
            return None, {'kind': 'remote_context_limit', 'error': exc.kind,
                'details': exc.details, 'budget': budget}
        raise
    if result.get('failure_reason') in {'context_budget', 'context_length_exceeded',
                                        'context_window_exceeded'}:
        return None, {'kind': result['failure_reason'], 'extraction_result_id': result['id']}
    if not usable(result):
        raise NarrativeError('incomplete_historical_index',
            'index extraction failed; retained=' + result['id'] +
            '; reason=' + str(result.get('failure_reason')))
    names = {r['id']: node['node_id'] + ':' + r['id']
             for r in result['records'] if r.get('id')}
    records = []
    for record in result['records']:
        fields = {k: list(v) for k, v in record['fields'].items()}
        if record['type'] == 'TEMPORAL':
            for key in ('from_event', 'to_event'):
                fields[key] = [names[x] for x in fields[key]]
        records.append({**record, 'id': names.get(record.get('id')), 'fields': fields,
            'node_id': node['node_id'],
            'authority': 'provisional_rendering' if record['type'] == 'ENTITY' and
                         fields.get('target_form') else 'candidate_unreviewed'})
    return {'summary': result['summary'], 'records': records,
        'quarantined': result['quarantined'], 'references': refs,
        'extraction_result_id': result['id'], 'state': result['state'],
        'failure_reason': result['failure_reason'], 'budget': budget}, None


def _save(repo, view, node, ids, payload, *, method, fallback_reason=None, child_revisions=(),
          store_kind='sequence-hierarchy-node'):
    pairs = view.read_chapters(ids)
    commits = {c['source_id']: ident for ident, c in repo.all_commits()}
    value = {'schema': PROFILE, 'run_id': repo.run_id, 'node_id': node['node_id'],
        'level': node['level'], 'parent_id': node.get('parent_id'), 'source_ids': ids,
        'start_position': pairs[0]['source']['order'], 'end_position': pairs[-1]['source']['order'],
        'dependencies': [dependency(d) for p in pairs for d in [p['source']] + p['translation']],
        'commit_ids': [commits[cid] for cid in ids if cid in commits],
        'pair_hash': digest(pairs), 'build_method': method,
        'fallback_reason': fallback_reason, 'child_revisions': list(child_revisions),
        'semantic_status': 'candidate_unreviewed', **payload}
    if store_kind == 'sequence-node':
        value.update(schema='sequence-node-v1', reference_profile=repo.manifest['spec'].get(
            'reference_profile', 'legacy'), node_revision=node['revision_id'])
    ident = repo.store.put(store_kind, value)
    return {'id': ident, **value}


def materialize_hierarchy(repo, spec, view, provider, *, force_open=False,
                          requested_child_ids=(), details_only=False):
    from .context import _parent_overview
    plan = plan_hierarchy(repo, spec, view)
    planning = plan.get('planning', {})
    if (planning.get('status') == 'capacity_decision_required' and
            spec.parent_build_mode == 'direct'):
        diagnostic = {'run_id': repo.run_id, 'planning': planning,
            'not_sent': True, 'reason': 'minimum L1 range does not fit a direct summary'}
        ident = repo.store.put('sequence-index-capacity-decision', diagnostic)
        raise NarrativeError('index_capacity_decision_required',
            diagnostic['reason'] + '; diagnostic=' + ident)
    by_id = {n['node_id']: n for n in plan['nodes']}
    if any(cid not in by_id or by_id[cid]['level'] != 2 for cid in requested_child_ids):
        raise NarrativeError('visibility_denied', 'unknown or future detail node')
    ready = {n['node_id']: n for n in latest_nodes(
        repo, allowed_source_ids=plan['ordered_source_ids'])}
    full, near = history_window(repo, spec, view, plan)
    caller = SequenceCaller(repo, spec, view.counter, provider)

    def ensure(node, ids, *, navigation=(), predecessors=()):
        if not ids:
            return None, None
        existing = ready.get(node['node_id'])
        if existing and set(ids) <= set(existing['source_ids']):
            return existing, None
        # Reuse an exact frozen independent Web summary when no confirmed
        # Chinese pairs would be lost. Cross-edition Chinese remains separate.
        pairs=view.read_chapters(ids)
        if not any(p['translation'] for p in pairs):
            frozen=next((n for n in repo.manifest['inputs'].get('history_nodes',[])
                if n.get('level',2)==node['level'] and n['source_ids']==ids),None)
            if frozen:
                value=_save(repo,view,node,ids,{'summary':frozen['summary'],'records':[],
                    'quarantined':[],'references':{},'state':'extracted','failure_reason':None,
                    'origin_revision':frozen['origin_fingerprint']},method=frozen.get('build_method','direct'))
                ready[node['node_id']]=value
                return value,None
        payload, failure = _extract(repo, spec, view, provider, node, ids,
                                    navigation, predecessors)
        if payload:
            value = _save(repo, view, node, ids, payload, method='direct')
            ready[node['node_id']] = value
            return value, None
        return None, failure

    for rid in plan['roots']:
        root = by_id[rid]
        ids = _desired_ids(repo, root, plan, force_open)
        navigation = [{'id': n['id'], 'summary': n['summary']} for n in ready.values()
                      if n['level'] == 1 and n['end_position'] < root['start_position']]
        failure = None
        if not details_only and ids and (force_open or not set(ids) <= full):
            if spec.parent_build_mode == 'child_summary_rollup':
                failure = {'kind': 'explicit_child_summary_rollup'}
            else:
                _, failure = ensure(root, ids, navigation=navigation)
        if failure:
            if spec.parent_build_mode == 'direct':
                diagnostic = {'run_id': repo.run_id, 'node_id': root['node_id'],
                    'source_ids': ids, 'failure': failure, 'planning': planning,
                    'not_sent': failure.get('kind') == 'local_context_limit',
                    'reason': 'direct L1 summary needs an explicit capacity fallback decision'}
                ident = repo.store.put('sequence-index-capacity-decision', diagnostic)
                raise NarrativeError('index_capacity_decision_required',
                    diagnostic['reason'] + '; diagnostic=' + ident)
            if spec.parent_build_mode not in {'direct_then_rollup', 'child_summary_rollup'}:
                raise NarrativeError('index_capacity_decision_required', 'unknown L1 fallback mode')
            selected = []
            for cid in root['children']:
                child = by_id[cid]
                child_ids = [s for s in child['ordered_source_ids'] if s in ids]
                if not child_ids:
                    continue
                value, child_failure = ensure(child, child_ids, navigation=navigation,
                    predecessors=[{'id': n['id'], 'summary': n['summary']} for n in selected])
                if child_failure:
                    raise NarrativeError('oversized_chapter' if len(child_ids) == 1 else
                        'index_context_exceeded', 'L2 evidence exceeds index context: ' + cid)
                selected.append(value)
            summary, summary_id = _parent_overview(repo, caller, selected, rid)
            payload = {'summary': summary, 'records': [r for n in selected for r in n['records']],
                'quarantined': [r for n in selected for r in n.get('quarantined', [])],
                'references': {k: v for n in selected for k, v in n.get('references', {}).items()},
                'state': 'partial' if any(n['state'] == 'partial' for n in selected) else 'extracted',
                'failure_reason': None, 'summary_revision': summary_id}
            ready[rid] = _save(repo, view, root, ids, payload,
                method='child_summary_rollup', fallback_reason=failure,
                child_revisions=[n['id'] for n in selected])
        previous = []
        for cid in root['children']:
            child = by_id[cid]
            ids = _desired_ids(repo, child, plan, force_open or cid in requested_child_ids)
            needed = cid in requested_child_ids or (not details_only and cid in near and
                (force_open or not set(ids) <= full))
            if needed and ids:
                value, failure = ensure(child, ids, navigation=navigation,
                                        predecessors=previous)
                if failure:
                    raise NarrativeError('oversized_chapter' if len(ids) == 1 else
                        'index_context_exceeded', 'detail context exceeds capacity: ' + cid)
                previous.append({'id': value['id'], 'summary': value['summary']})
            elif cid in ready:
                previous.append({'id': ready[cid]['id'], 'summary': ready[cid]['summary']})
    completed = {cid for n in ready.values() if usable(n) for cid in n['source_ids']}
    with repo.db:
        for eid, raw in repo.db.execute(
                "SELECT id,payload FROM sequence_outbox WHERE status='pending'").fetchall():
            if json.loads(raw)['source_id'] in completed:
                repo.db.execute("UPDATE sequence_outbox SET status='completed' WHERE id=?",
                                (eid,))
    index_snapshot(repo)
    from .entities import update_entity_catalog
    entity_nodes = latest_nodes(repo) + latest_incremental_nodes(repo)
    update_entity_catalog(repo, spec, view, caller, entity_nodes)
    if spec.thread_reconcile and caller is not None:
        from .threads import update_thread_catalog
        try:
            update_thread_catalog(repo, spec, view, caller, entity_nodes, allow_model=True)
        except NarrativeError:
            pass
    return list(ready.values())


def index_snapshot(repo):
    nodes = latest_nodes(repo) + latest_incremental_nodes(repo)
    revisions = {n['id'] for n in nodes}
    parents = {}
    for node in nodes:
        parent = node.get('parent_id')
        if parent:
            parents.setdefault(parent, []).append(node['id'])
        if node['level'] == 1:
            parents.setdefault(node['node_id'], [])
    for ident, summary in repo.store.objects('sequence-parent-summary'):
        children = set(summary.get('children', []))
        if summary.get('node_id') and children and all(
                child in {node.get('node_id') for node in nodes} | revisions
                for child in children):
            revisions.add(ident)
            parents.setdefault(summary['node_id'], []).append(ident)
    value = {'run_id': repo.run_id, 'node_revisions': sorted(revisions),
        'parent_metadata': {parent: sorted(children) for parent, children in parents.items()}}
    return repo.store.put('sequence-index-revision', value)


def select_context(repo, spec, view, *, keep_full_tail=True, caller=None):
    """Choose non-overlapping historical/incremental coverage.

    The frozen hierarchy supplies historical L1/L2 nodes.  Committed chapters
    are indexed independently in ``sequence-node``; once they are outside the
    full tail, their sibling L2 nodes are rolled up into an ephemeral L1
    navigation entry.  A missing incremental node therefore falls through to
    a full pair instead of being reported as an explicit gap.
    """
    from .context import _parent_overview
    plan = plan_hierarchy(repo, spec, view)
    historical = list(plan['ordered_source_ids'])
    committed = [c['source_id'] for _, c in repo.all_commits()]
    branch_committed = [c['source_id'] for _, c in repo.commits()]
    allowed = set(historical) | set(committed)
    positions = {c['chapter_id']: c['order'] for c in view.visible_sources()
                 if c['chapter_id'] in allowed and c['order'] < view.target_order}
    ordered = sorted((cid for cid in allowed if cid in positions), key=positions.get)
    history_ready = latest_nodes(repo, allowed_source_ids=set(historical))
    incremental_ready = latest_incremental_nodes(repo, allowed_source_ids=set(committed))
    ready = history_ready + incremental_ready
    full = set(ordered[-spec.n_chapter:]) if keep_full_tail else set()
    represented = set(full)
    selected, coverage = [], []
    children = [n for n in ready if n['level'] == 2 and
                not set(n['source_ids']) <= full]
    for node in reversed(children[-spec.n_child:]):
        ids = [cid for cid in node['source_ids'] if cid not in represented]
        if ids:
            selected.append(node)
            represented.update(ids)
            coverage.append(_coverage(node, ids))
    for node in sorted((n for n in ready if n['level'] == 1),
                       key=lambda n: n['end_position'], reverse=True):
        ids = [cid for cid in node['source_ids'] if cid not in represented]
        if ids:
            selected.append(node)
            represented.update(ids)
            coverage.append(_coverage(node, ids))

    # Incremental nodes have no frozen L1 parent.  Build only the far parent
    # summaries that are actually needed by this checkpoint.  Children that
    # overlap the recent full tail are still summarized as navigation, while
    # their coverage is restricted to the uncovered chapter IDs.
    far_groups = {}
    for node in incremental_ready:
        ids = [cid for cid in node['source_ids'] if cid not in represented]
        if not ids:
            continue
        far_groups.setdefault(node.get('parent_id') or node['node_id'], []).append((node, ids))
    for parent_id, members in sorted(
            far_groups.items(), key=lambda item: min(positions.get(cid, 10**12)
                                                     for _, ids in item[1] for cid in ids)):
        children_for_rollup = []
        uncovered = []
        for node, ids in sorted(members, key=lambda item: item[0]['start_position']):
            children_for_rollup.append(node)
            uncovered.extend(ids)
        if not uncovered:
            continue
        summary, summary_id = _parent_overview(repo, caller, children_for_rollup, parent_id)
        synthetic_id = summary_id or ('sequence-parent-overview:' + digest({
            'run': repo.run_id, 'parent_id': parent_id,
            'children': [n['id'] for n in children_for_rollup]}))
        dependencies = []
        seen_dependencies = set()
        records = []
        for child in children_for_rollup:
            records.extend(child.get('records', []))
            for dependency in child.get('dependencies', []):
                key = dependency['chapter_id']
                if key not in seen_dependencies:
                    dependencies.append(dependency)
                    seen_dependencies.add(key)
        synthetic = {
            'id': synthetic_id, 'node_id': parent_id, 'parent_id': None,
            'level': 1, 'source_ids': uncovered,
            'ordered_source_ids': uncovered,
            'start_position': min(positions[cid] for cid in uncovered),
            'end_position': max(positions[cid] for cid in uncovered),
            'summary': summary, 'records': records,
            'dependencies': dependencies, 'state': 'extracted',
            'semantic_status': 'candidate_unreviewed',
            'build_method': 'child_summary_rollup',
            'child_revisions': [n['id'] for n in children_for_rollup],
            'children': [n['node_id'] for n in children_for_rollup],
        }
        selected.append(synthetic)
        represented.update(uncovered)
        coverage.append(_coverage(synthetic, uncovered))
    full.update(cid for cid in ordered if cid not in represented)
    # Inherited fork commits are already frozen as history pairs and have no
    # local branch turn to replay.  Only current-branch commits are omitted
    # here because hierarchy_context adds their exact user/assistant turns.
    full_pairs = view.read_chapters([cid for cid in ordered if cid in full
                                     and cid not in set(branch_committed)])
    for cid in sorted(full, key=positions.get):
        pair = next((p for p in full_pairs if p['source']['chapter_id'] == cid), None)
        if pair is not None:
            representation = 'full_pair' if pair['translation'] else 'full_source'
            revision_id = digest(pair)
            quality = pair['translation_status']
        else:
            commit = next((ident for ident, item in repo.all_commits()
                           if item['source_id'] == cid), None)
            representation, revision_id, quality = 'full_pair', commit, 'committed'
        coverage.append({'source_ids': [cid],
            'representation': representation, 'level': None,
            'revision_id': revision_id, 'quality': quality})
    rank = {cid: i for i, cid in enumerate(ordered)}
    coverage.sort(key=lambda row: rank[row['source_ids'][0]])
    selected.sort(key=lambda n: (n['start_position'], n['level']))
    return {'nodes': selected, 'full_pairs': full_pairs, 'full_source_ids': sorted(full, key=positions.get),
        'committed_source_ids': set(committed), 'all_nodes': ready,
        'coverage': coverage, 'expected_source_ids': ordered, 'plan': plan,
        'index_revision': digest(sorted(n['id'] for n in ready))}


def _coverage(node, ids):
    return {'source_ids': ids, 'summary_source_ids': node['source_ids'],
        'representation': 'L' + str(node['level']), 'level': node['level'],
        'node_id': node['node_id'], 'revision_id': node['id'],
        'build_method': node.get('build_method', 'direct'), 'quality': node['state']}
