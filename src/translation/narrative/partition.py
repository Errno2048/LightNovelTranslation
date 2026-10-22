"""Explicit two-level partitions; no chapter-number division or hidden Chinese."""
from dataclasses import asdict, dataclass
import math

from ..budget import ModelCapacity, RoleBudget, plan_request
from ..storage import digest
from .schemas import NarrativeError


@dataclass(frozen=True)
class AdaptiveIndexSpec:
    parent_max_chapters: int = 100
    child_max_chapters: int = 10
    planning_utilization: float = .80
    predecessor_reserve_tokens: int = 8192
    protocol_reserve_tokens: int = 4096
    translation_expansion: float = 1.2
    child_output_per_chapter: int = 640
    parent_output_per_chapter: int = 96
    minimum_output_tokens: int = 1024
    child_min_content_tokens: int = 0
    parent_summary_input_tokens_per_child: int = 8192
    layout: str = "adaptive"

    def __post_init__(self):
        for name in ('parent_max_chapters', 'child_max_chapters', 'child_output_per_chapter',
                     'parent_output_per_chapter', 'minimum_output_tokens'):
            if type(getattr(self,name)) is not int or getattr(self,name) <= 0:
                raise ValueError("positive index bounds required")
        if type(self.child_min_content_tokens) is not int or self.child_min_content_tokens < 0:
            raise ValueError("child minimum content tokens must be nonnegative")
        if type(self.parent_summary_input_tokens_per_child) is not int or self.parent_summary_input_tokens_per_child <= 0:
            raise ValueError("parent summary input allowance must be positive")
        if self.child_max_chapters > self.parent_max_chapters:
            raise ValueError("child bound exceeds parent bound")
        if not 0 < self.planning_utilization <= 1 or self.translation_expansion < 0:
            raise ValueError("invalid planning allowance")
        if min(self.predecessor_reserve_tokens,self.protocol_reserve_tokens) < 0:
            raise ValueError("negative planning reserve")


def _measurement(pair, counter, spec):
    src = pair['source']
    zh = pair.get('translation', [])
    if isinstance(zh, dict): zh = [zh]
    source = counter.count(src['title']+'\n'+src['text'])
    # Unavailable references are estimated from Japanese only. Never load them.
    target = sum(counter.count(d['text']) for d in zh) if zh else math.ceil(source*spec.translation_expansion)
    return source+target+128


def plan_adaptive_index(pairs, index_spec, capacity, role_budget, counter, *,
                        source_manifest_id, visibility_policy_id, excluded_ranges=(), existing_plan=None):
    """Pure planning over already permission-checked pairs, or a NarrativeView.

    IDs bind a layout lineage and a start, while immutable revisions bind exact
    membership. Sealed nodes survive append; only an open tail is replanned.
    """
    if hasattr(pairs, 'read_chapters'):
        view = pairs
        pairs = view.read_chapters([c['chapter_id'] for c in view.visible_sources()])
    pairs = list(pairs)
    spec = index_spec
    ids = [p['source']['chapter_id'] for p in pairs]
    orders = [p['source']['order'] for p in pairs]
    if len(ids) != len(set(ids)) or orders != sorted(set(orders)):
        raise NarrativeError('invalid_record','partition inputs must be unique in reading order')
    if role_budget.requested_output_tokens > capacity.max_output_tokens:
        raise NarrativeError('budget_exceeded','index output reserve exceeds model capacity')
    soft_input = math.floor(min(capacity.context_window_tokens-role_budget.requested_output_tokens-capacity.safety_tokens,
                               capacity.max_input_tokens or capacity.context_window_tokens)*spec.planning_utilization)
    overhead = spec.predecessor_reserve_tokens+spec.protocol_reserve_tokens
    measurements={p['source']['chapter_id']:_measurement(p,counter,spec) for p in pairs}
    def report(members, level):
        content = sum(measurements[p['source']['chapter_id']]-128 for p in members)
        estimate = overhead+content+len(members)*128
        output = spec.minimum_output_tokens+len(members)*(spec.child_output_per_chapter if level==2 else spec.parent_output_per_chapter)
        safe = math.ceil(estimate*capacity.token_inflation_factor)
        return {'estimated_input_tokens':estimate,'safe_input_tokens':safe,'soft_input_limit':soft_input,
                'content_input_tokens':content,'estimated_output_tokens':output,'requested_output_tokens':role_budget.requested_output_tokens,
                'fits':safe<=soft_input and output+role_budget.reasoning_reserve_tokens<=role_budget.requested_output_tokens}
    def rollup_report(members, child_count):
        estimate=overhead+child_count*spec.parent_summary_input_tokens_per_child
        output=spec.minimum_output_tokens+len(members)*spec.parent_output_per_chapter
        safe=math.ceil(estimate*capacity.token_inflation_factor)
        return {'estimated_input_tokens':estimate,'safe_input_tokens':safe,'soft_input_limit':soft_input,
                'content_input_tokens':child_count*spec.parent_summary_input_tokens_per_child,
                'estimated_output_tokens':output,'requested_output_tokens':role_budget.requested_output_tokens,
                'basis':'child_summary_rollup',
                'fits':safe<=soft_input and output+role_budget.reasoning_reserve_tokens<=role_budget.requested_output_tokens}
    identity_spec=asdict(spec)
    if not spec.child_min_content_tokens:
        identity_spec.pop('child_min_content_tokens',None)
        identity_spec.pop('parent_summary_input_tokens_per_child',None)
    identity = {'planner':'adaptive-token' if spec.child_min_content_tokens else 'adaptive',
                'spec':identity_spec,'capacity':asdict(capacity),
                'role':asdict(role_budget),'counter':counter.identity,'source_manifest':source_manifest_id,
                'visibility':visibility_policy_id}
    lineage = digest(identity)
    frozen_roots, frozen_nodes, consumed = [], [], 0
    if existing_plan:
        if existing_plan['identity'] != identity:
            raise NarrativeError('conflict','changed planner configuration requires a new layout lineage')
        lineage = existing_plan['lineage']
        by_id = {n['node_id']:n for n in existing_plan['nodes']}
        for root_id in existing_plan['roots']:
            root = by_id[root_id]
            if root['state'] != 'sealed': break
            members = root['ordered_source_ids']
            if ids[consumed:consumed+len(members)] != members:
                raise NarrativeError('stale_summary','sealed index membership changed')
            for p in pairs[consumed:consumed+len(members)]:
                if existing_plan['pair_versions'].get(p['source']['chapter_id'])!=digest(p):
                    raise NarrativeError('stale_summary','sealed index bilingual revision changed')
            frozen_roots.append(root_id)
            frozen_nodes.extend([root]+[by_id[x] for x in root['children']])
            consumed += len(members)
    remaining = pairs[consumed:]
    children, current = [], []
    def flush_child(reason):
        if current:
            children.append((list(current),reason))
            current.clear()
    for pair in remaining:
        if not report([pair],2)['fits'] or (not spec.child_min_content_tokens and not report([pair],1)['fits']):
            raise NarrativeError('oversized_chapter',pair['source']['chapter_id']+': single chapter exceeds index input/output allowance')
        gap = current and pair['source']['order'] != current[-1]['source']['order']+1
        if current and (gap or len(current)>=spec.child_max_chapters or not report(current+[pair],2)['fits']):
            flush_child('visibility_gap' if gap else 'chapter_limit' if len(current)>=spec.child_max_chapters else 'token_budget')
        current.append(pair)
    flush_child('open_tail')
    groups, current_group = [], []
    for child,why in children:
        flattened = [p for c,_ in current_group for p in c]
        gap = flattened and child[0]['source']['order'] != flattened[-1]['source']['order']+1
        if current_group and (gap or len(flattened)+len(child)>spec.parent_max_chapters or not report(flattened+child,1)['fits']):
            groups.append((current_group,'visibility_gap' if gap else 'chapter_limit' if len(flattened)+len(child)>spec.parent_max_chapters else 'token_budget'))
            current_group=[]
        if not report(child,1)['fits']:
            # L1 coarse request can have a stricter output allowance. Split the
            # as-yet unsealed L2 candidate rather than crossing child boundaries.
            for p in child:
                if current_group:
                    flat=[q for c,_ in current_group for q in c]
                    if len(flat)+1>spec.parent_max_chapters or not report(flat+[p],1)['fits']:
                        groups.append((current_group,'token_budget')); current_group=[]
                current_group.append(([p],'parent_budget'))
        else:
            current_group.append((child,why))
    if current_group: groups.append((current_group,'open_tail'))
    # If a full-source parent would force child blocks below the configured
    # useful size, rebuild the remaining L2 layout independently, then budget
    # L1 by conservative child-summary allowances. L2 still carries full
    # bilingual evidence; only the parent rollup consumes its summaries.
    layout='full_source'
    fallback_reason=None
    if spec.child_min_content_tokens:
        planned_children=[child for group,_ in groups for child,_ in group]
        undersized=any(report(child,2)['content_input_tokens']<spec.child_min_content_tokens
                       for child in planned_children)
        # A parent split specifically by full-source input budget is the signal
        # that an L1 rollup from its L2 summaries may retain a larger useful
        # navigation range. Chapter-count/visibility boundaries do not trigger
        # the fallback on their own.
        parent_would_split=any(reason=='token_budget' for _,reason in groups)
        if undersized or parent_would_split:
            layout='summary_rollup'
            fallback_reason=('child_below_minimum' if undersized else '')
            if parent_would_split:
                fallback_reason += ('+' if fallback_reason else '')+'full_source_parent_token_budget'
            packed=[]; current=[]
            def flush_packed(reason):
                if current:
                    packed.append((list(current),reason)); current.clear()
            for pair in remaining:
                gap=current and pair['source']['order']!=current[-1]['source']['order']+1
                exceeds=(current and (gap or len(current)>=spec.child_max_chapters or
                    not report(current+[pair],2)['fits']))
                if exceeds:
                    flush_packed('visibility_gap' if gap else 'chapter_limit' if len(current)>=spec.child_max_chapters else 'token_budget')
                current.append(pair)
            flush_packed('open_tail')
            # A short terminal block is often just an artefact of the window
            # ending. Merge adjacent blocks where the L2 hard budget permits.
            changed=True
            while changed:
                changed=False
                for i,(members,reason) in enumerate(packed):
                    if report(members,2)['content_input_tokens']>=spec.child_min_content_tokens:
                        continue
                    for j in (i-1,i+1):
                        if not 0<=j<len(packed): continue
                        left=min(i,j); right=max(i,j)
                        a,ra=packed[left]; b,rb=packed[right]
                        if a[-1]['source']['order']+1!=b[0]['source']['order']: continue
                        merged=a+b
                        if len(merged)<=spec.child_max_chapters and report(merged,2)['fits']:
                            packed[left]=(merged,rb if right==len(packed)-1 and rb=='open_tail' else 'minimum_tokens_merged')
                            del packed[right]; changed=True; break
                    if changed: break
            groups=[]; current_group=[]
            for child,why in packed:
                flattened=[p for c,_ in current_group for p in c]
                gap=flattened and child[0]['source']['order']!=flattened[-1]['source']['order']+1
                too_many=len(flattened)+len(child)>spec.parent_max_chapters
                exceeds=bool(current_group) and (gap or too_many or
                    not rollup_report(flattened+child,len(current_group)+1)['fits'])
                if exceeds:
                    groups.append((current_group,'visibility_gap' if gap else 'chapter_limit' if too_many else 'rollup_budget'))
                    current_group=[]
                if not rollup_report(child,len(current_group)+1)['fits']:
                    raise NarrativeError('oversized_chapter','one L2 summary exceeds the configured L1 rollup allowance')
                current_group.append((child,why))
            if current_group: groups.append((current_group,'open_tail'))
    elif any(not report(child,1)['fits'] for group,_ in groups for child,_ in group):
        raise NarrativeError('oversized_chapter','one full-source L2 block cannot fit its L1 request allowance')
    nodes=list(frozen_nodes); roots=list(frozen_roots)
    def make_node(members,level,parent,state,reason,child_count=None):
        source_ids=[p['source']['chapter_id'] for p in members]
        node_id='adaptive-node:'+digest({'lineage':lineage,'level':level,'parent':parent,'start':source_ids[0]})[:24]
        budget=(rollup_report(members,child_count or 0) if layout=='summary_rollup' and level==1 else report(members,level))
        node={'node_id':node_id,'parent_id':parent,'level':level,'ordered_source_ids':source_ids,
              'start_position':members[0]['source']['order'],'end_position':members[-1]['source']['order'],
              'state':state,'boundary_reason':reason,'boundary_source':'token_budget' if 'budget' in reason else 'fixed',
              'volume_id':None,'layout':layout,'budget':budget,
              'minimum_child_content_tokens':spec.child_min_content_tokens if level==2 else None,
              'minimum_exception':bool(level==2 and spec.child_min_content_tokens and
                  report(members,2)['content_input_tokens']<spec.child_min_content_tokens)}
        node['revision_id']='adaptive-node-revision:'+digest(node)
        return node
    for gi,(group,reason) in enumerate(groups):
        members=[p for c,_ in group for p in c]
        state='open' if gi==len(groups)-1 and reason=='open_tail' and len(members)<spec.parent_max_chapters else 'sealed'
        root=make_node(members,1,None,state,reason,child_count=len(group))
        root['children']=[]
        child_nodes=[]
        for ci,(members2,why) in enumerate(group):
            child_state='open' if state=='open' and ci==len(group)-1 and len(members2)<spec.child_max_chapters else 'sealed'
            node=make_node(members2,2,root['node_id'],child_state,why)
            root['children'].append(node['node_id']); child_nodes.append(node)
        root['revision_id']='adaptive-node-revision:'+digest({k:v for k,v in root.items() if k!='revision_id'})
        roots.append(root['node_id']); nodes.extend([root]+child_nodes)
    flattened=[cid for rid in roots for n in nodes if n['node_id']==rid for cid in n['ordered_source_ids']]
    if flattened != ids:
        raise NarrativeError('invalid_record','adaptive partition lost or duplicated chapters')
    layouts={n.get('layout','full_source') for n in nodes if n['level']==1}
    selected_layout='mixed' if len(layouts)>1 else next(iter(layouts),layout)
    plan={'schema':'adaptive-index-plan-v1','identity':identity,'lineage':lineage,'roots':roots,'nodes':nodes,
          'selected_layout':selected_layout,
          'fallback_reason':fallback_reason,
          'minimum_child_content_tokens':spec.child_min_content_tokens,
          'excluded_ranges':list(excluded_ranges),'ordered_source_ids':ids,
          'pair_versions':{p['source']['chapter_id']:digest(p) for p in pairs}}
    plan['plan_id']='adaptive-index-plan:'+digest(plan)
    return plan


def import_legacy_index(index, *, source_manifest_id):
    """Preserve existing fixed-node identities and summary references verbatim."""
    return {'schema':'legacy-fixed-v1','source_manifest_id':source_manifest_id,
            'legacy_index':index,'plan_id':'legacy-index-plan:'+digest(index)}
