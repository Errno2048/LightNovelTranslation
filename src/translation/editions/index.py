"""Independent L2 packing; L1 never forces an L2 to become a single chapter."""
from dataclasses import asdict, dataclass

from ..budget import RoleBudget, plan_request, require_budget
from ..storage import digest
from .archive import document_dependency


@dataclass(frozen=True)
class EditionIndexSpec:
    parent_max_chapters: int = 100
    child_max_chapters: int = 10
    child_target_tokens: int = 64000
    child_min_content_tokens: int = 4000
    predecessor_reserve_tokens: int = 4096
    protocol_reserve_tokens: int = 4096

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError('index sizes must be positive integers')
        if self.parent_max_chapters < self.child_max_chapters:
            raise ValueError('parent bound smaller than child bound')


def plan_edition_index(archive, edition_id, capacity, counter, spec=None, *, output_tokens=None):
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
            parent.update(child_ids=[c['node_id'] for c in own], build_method='child_summary_rollup')
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
