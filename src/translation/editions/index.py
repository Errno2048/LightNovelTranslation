"""Independent L2 packing; L1 never forces an L2 to become a single chapter."""
from dataclasses import asdict, dataclass
import math

from ..budget import RoleBudget, plan_request, require_budget
from ..storage import digest
from .archive import document_dependency


@dataclass(frozen=True)
class EditionIndexSpec:
    parent_max_chapters: int = 100
    child_max_chapters: int = 100
    layout: str = 'token-v2'
    parent_min_chapters: int | None = None
    expected_l2_count: int = 10
    l2_prepartition_min_chapters: int = 3
    parent_build_mode: str = 'direct'
    entity_catalog_threshold: int = 50
    core_entity_limit: int = 50
    child_target_tokens: int = 64000
    child_min_content_tokens: int = 4000
    predecessor_reserve_tokens: int = 4096
    protocol_reserve_tokens: int = 4096

    def __post_init__(self):
        numeric = {k: v for k, v in asdict(self).items() if k not in {'parent_build_mode', 'layout'}}
        if self.parent_min_chapters is None:
            object.__setattr__(self, 'parent_min_chapters', min(30, self.parent_max_chapters))
            numeric['parent_min_chapters'] = min(30, self.parent_max_chapters)
        if any(type(v) is not int or v <= 0 for v in numeric.values()):
            raise ValueError('index sizes must be positive integers')
        if self.child_max_chapters > self.parent_max_chapters:
            raise ValueError('child hard cap exceeds parent hard cap')
        if self.entity_catalog_threshold > self.core_entity_limit:
            raise ValueError('entity catalog threshold cannot exceed the core entity limit')
        if self.layout not in {'token-v2', 'legacy-v1'}:
            raise ValueError('unknown index layout')
        if self.layout == 'token-v2' and self.parent_min_chapters > self.parent_max_chapters:
            raise ValueError('L1 minimum chapter count exceeds the configured hard maximum')
        if self.parent_build_mode not in {'direct', 'child_summary_rollup', 'direct_then_rollup'}:
            raise ValueError('invalid L1 build mode')


def _plan_edition_index_legacy(archive, edition_id, capacity, counter, spec=None, *, output_tokens=None):
    spec = spec or EditionIndexSpec()
    output_tokens = output_tokens or min(12288, capacity.max_output_tokens)
    docs = [d for d in archive.documents(edition_id) if d['rows']]
    report = require_budget(plan_request([{'role': 'system', 'content': ''}], capacity, RoleBudget(output_tokens), counter))
    usable = int(report['input_limit'] / capacity.token_inflation_factor) - spec.predecessor_reserve_tokens - spec.protocol_reserve_tokens
    if usable <= 0:
        raise ValueError('no input capacity for independent index')
    ceiling = min(spec.child_target_tokens, usable)
    sizes = {d['entry_id']: counter.count(d['raw_title'] + '\n' + d['text']) + len(d['rows'])*32 + 64 for d in docs}
    if any(v > usable for v in sizes.values()):
        raise ValueError('oversized single document: split its paragraph ranges before indexing')
    identity = {'schema': 'edition-plan-v1', 'edition_id': edition_id, 'spec': asdict(spec),
                'capacity': asdict(capacity), 'counter': counter.identity, 'output_tokens': output_tokens}
    logical_volumes = any(d.get('metadata', {}).get('index_boundary_profile') == 'logical-volume-v1' for d in docs)
    if logical_volumes:
        identity['boundary_profile'] = 'logical-volume-v1'

    def boundary(doc):
        if doc.get('metadata', {}).get('index_boundary_profile') == 'logical-volume-v1':
            return ('volume', doc.get('volume_id'))
        return ('document', doc['document_id'])
    parents, children, group, current, current_tokens = [], [], [], [], 0

    def node(level, members):
        deps = [document_dependency(d) for d in members]
        node_id = 'B' + digest({'identity': identity, 'level': level, 'members': deps})[:20]
        return {'node_id': node_id, 'edition_id': edition_id, 'language': members[0]['language'],
                'level': level, 'entry_ids': [d['entry_id'] for d in members],
                'dependencies': deps, 'titles': [d['raw_title'] for d in members],
                'reading_range': [members[0]['edition_order'], members[-1]['edition_order']],
                'content_tokens': sum(sizes[d['entry_id']] for d in members), 'status': 'not_built'}

    def flush_child():
        nonlocal current, current_tokens
        if current:
            children.append(node(2, current))
            current, current_tokens = [], 0

    def flush_parent():
        nonlocal group
        flush_child()
        if group:
            parent = node(1, group)
            member_ids = set(parent['entry_ids'])
            own = [c for c in children if set(c['entry_ids']) <= member_ids]
            for c in own:
                c['parent_id'] = parent['node_id']
                c['undersized'] = c['content_tokens'] < spec.child_min_content_tokens
            parent.update(child_ids=[c['node_id'] for c in own], build_method='direct_then_rollup')
            parents.append(parent)
            group = []

    for doc in docs:
        # Native volume boundaries are meaningful even if the token budget fits.
        if group and (boundary(doc) != boundary(group[-1]) or len(group) >= spec.parent_max_chapters):
            flush_parent()
        size = sizes[doc['entry_id']]
        if current and (len(current) >= spec.child_max_chapters or
                        (current_tokens + size > ceiling and current_tokens >= spec.child_min_content_tokens)):
            flush_child()
        if current and current_tokens + size > usable:
            flush_child()
        current.append(doc)
        group.append(doc)
        current_tokens += size
    flush_parent()
    return {'identity': identity, 'fingerprint': digest({'identity': identity, 'nodes': parents+children}),
            'parents': parents, 'children': children, 'planned_entry_ids': [d['entry_id'] for d in docs]}


def plan_edition_index(archive, edition_id, capacity, counter, spec=None, *, output_tokens=None):
    """Plan token-balanced L1/L2 nodes and expose strict L1 feasibility locally.

    The old fixed/target-token layout remains available for frozen experiments
    through ``layout='legacy-v1'``. New plans use the exact prompt projection
    plus the configured conservative tokenizer inflation factor.
    """
    spec = spec or EditionIndexSpec()
    if spec.layout == 'legacy-v1':
        return _plan_edition_index_legacy(archive, edition_id, capacity, counter, spec,
                                          output_tokens=output_tokens)
    from .prompts import summary_messages

    output_tokens = output_tokens or min(12288, capacity.max_output_tokens)
    docs = [d for d in archive.documents(edition_id) if d['rows']]
    docs = sorted(docs, key=lambda d: d['edition_order'])
    effective_min = min(spec.parent_min_chapters, spec.parent_max_chapters)
    effective_max = spec.parent_max_chapters
    identity = {'schema': 'edition-plan-v2', 'planner': 'token-v2', 'edition_id': edition_id,
                'spec': asdict(spec), 'capacity': asdict(capacity), 'counter': counter.identity,
                'output_tokens': output_tokens, 'inflation': capacity.token_inflation_factor}
    logical_volumes = any(d.get('metadata', {}).get('index_boundary_profile') == 'logical-volume-v1'
                          for d in docs)
    if logical_volumes:
        identity['boundary_profile'] = 'logical-volume-v1'
    sizes = {d['entry_id']: counter.count(d['raw_title'] + '\n' + d['text']) + len(d['rows'])*32 + 64
             for d in docs}

    def node(level, members, parent_id=None, status='not_built', *, short_range=False):
        deps = [document_dependency(d) for d in members]
        node_id = 'B' + digest({'identity': identity, 'level': level, 'parent': parent_id,
                                'members': deps})[:20]
        return {'node_id': node_id, 'edition_id': edition_id, 'language': members[0]['language'],
                'level': level, 'parent_id': parent_id,
                'entry_ids': [d['entry_id'] for d in members], 'dependencies': deps,
                'titles': [d['raw_title'] for d in members],
                'reading_range': [members[0]['edition_order'], members[-1]['edition_order']],
                'content_tokens': sum(sizes[d['entry_id']] for d in members),
                'status': status, 'short_range': short_range}

    def direct_budget(members):
        probe = node(1, members)
        messages = summary_messages(probe, members)
        return plan_request(messages, capacity, RoleBudget(output_tokens), counter)

    def child_budget(members):
        probe = node(2, members)
        return plan_request(summary_messages(probe, members), capacity,
                            RoleBudget(output_tokens), counter)

    def boundary(doc):
        if doc.get('metadata', {}).get('index_boundary_profile') == 'logical-volume-v1':
            return ('volume', doc.get('volume_id'))
        return ('document', doc['document_id'])

    sections, section = [], []
    for doc in docs:
        if section and (boundary(doc) != boundary(section[-1]) or
                        doc['edition_order'] != section[-1]['edition_order'] + 1):
            sections.append(section)
            section = []
        section.append(doc)
    if section:
        sections.append(section)

    planning_status = 'ready'
    diagnostics = []
    parent_groups = []
    expected_parent_count = 0
    for segment in sections:
        expected_parent_count += max(1, round(len(segment) / max(1, effective_min)))
        cursor = 0
        while cursor < len(segment):
            remaining = segment[cursor:]
            if len(remaining) >= effective_min:
                minimum = remaining[:effective_min]
                budget = direct_budget(minimum)
                if not budget['fits']:
                    planning_status = 'index_capacity_decision_required'
                    diagnostics.append({'kind': 'minimum_l1_does_not_fit',
                        'entry_ids': [d['entry_id'] for d in minimum], 'minimum_chapters': effective_min,
                        'budget': budget, 'suggested_actions': ['explicit_child_summary_rollup',
                                                               'revise_index_capacity_or_l1_minimum']})
            chosen = []
            for doc in remaining[:effective_max]:
                trial = chosen + [doc]
                if chosen and not direct_budget(trial)['fits']:
                    break
                if not chosen and not direct_budget(trial)['fits']:
                    # Keep a visible range in the plan so an agent can inspect
                    # it and choose an explicit alternate method.
                    chosen.append(doc)
                    diagnostics.append({'kind': 'oversized_single_chapter',
                                        'entry_id': doc['entry_id'], 'budget': direct_budget(trial)})
                    planning_status = 'index_capacity_decision_required'
                    break
                chosen.append(doc)
            if not chosen:
                raise ValueError('token planner failed to advance')
            short = len(chosen) < effective_min
            if short and len(remaining) >= effective_min:
                diagnostics.append({'kind': 'short_l1_range',
                    'entry_ids': [d['entry_id'] for d in chosen],
                    'minimum_chapters': effective_min})
                planning_status = 'index_capacity_decision_required'
            parent_groups.append((chosen, short))
            cursor += len(chosen)

    parents, children = [], []
    for members, short in parent_groups:
        # The expected child count is derived from the L1's token volume and
        # its prepartition minimum. Final chunks remain chapter-atomic and may
        # be shorter where one large chapter skews the token distribution.
        expected_children = min(spec.expected_l2_count,
            max(1, round(len(members) / spec.l2_prepartition_min_chapters)))
        target = max(1, math.ceil(sum(sizes[d['entry_id']] for d in members) / expected_children))
        parent = node(1, members, short_range=short)
        l2_groups, current, current_tokens = [], [], 0
        for i, doc in enumerate(members):
            trial = current + [doc]
            should_balance = (current and len(l2_groups) < expected_children - 1 and
                              current_tokens >= target)
            if should_balance or (current and (len(current) >= spec.child_max_chapters or
                                                  not child_budget(trial)['fits'])):
                l2_groups.append(current)
                current, current_tokens = [], 0
                trial = [doc]
            if not child_budget(trial)['fits']:
                diagnostics.append({'kind': 'oversized_l2_chapter', 'entry_id': doc['entry_id'],
                                    'budget': child_budget(trial)})
                planning_status = 'oversized_chapter'
            current.append(doc)
            current_tokens += sizes[doc['entry_id']]
        if current:
            l2_groups.append(current)
        parent['child_ids'] = []
        for group in l2_groups:
            child = node(2, group, parent['node_id'], short_range=len(group) < spec.l2_prepartition_min_chapters)
            parent['child_ids'].append(child['node_id'])
            children.append(child)
        parent['expected_l2_count'] = expected_children
        parent['prepartition_min_l2_chapters'] = spec.l2_prepartition_min_chapters
        parent['build_method'] = spec.parent_build_mode
        parents.append(parent)

    result = {'identity': identity, 'parents': parents, 'children': children,
        'planned_entry_ids': [d['entry_id'] for d in docs],
        'planning': {'status': planning_status, 'diagnostics': diagnostics,
                     'expected_l1_count': expected_parent_count,
                     'minimum_l1_chapters': effective_min, 'maximum_l1_chapters': effective_max,
                     'token_balanced': True, 'inflation_factor': capacity.token_inflation_factor,
                     'default_parent_build_mode': spec.parent_build_mode}}
    result['fingerprint'] = digest(result)
    return result
