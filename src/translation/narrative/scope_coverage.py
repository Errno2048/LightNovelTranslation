"""Narrow coverage-note adjudication; never deletes original model notes.

Only observed, unambiguous whole-sentence missing-chapter notices and explicit
coarsening/no-gap declarations can be classified. Unknown or mixed claims block.
No excluded body is read to establish that it is outside the requested scope.
"""
import re

from ..storage import digest
from .schemas import NarrativeError

VERSION = 'summary-policy-exclusion-review-v1'
COMPRESSION_VERSION = 'summary-declared-compression-review-v1'
_COMPRESSION = [
    re.compile(r'^本节点内部分战斗细节（(?P<detail>[^（）\r\n]{1,160})）属侧面描写，已按原文范围概括，无未处理的重大缺口。$'),
    re.compile(r'^本节点摘要聚焦主线事件，(?P<detail>[^。！？\r\n]{1,160})的详细过程未逐一展开，属正常粗化。$'),
    re.compile(r'^(?P<detail>\d+章为Side章节[^。！？\r\n]{1,450})已并入摘要中未单独展开。$'),
    re.compile(r'^无重要剧情缺口；本块覆盖(?P<detail>[^。！？\r\n]{1,300})的完整经过。$'),
]
_STRUCTURAL_COMPRESSION = '本节点为level 1整段粗化摘要，各章细部分节（level 2及以下）未生成，属正常结构层级安排而非剧情覆盖缺口。'
_MATERIAL = re.compile(r'未(?:读取|处理|覆盖|提供)|缺(?:章|失|少)|遗漏|不完整|尚未')
_NO_GAP = re.compile(r'^(?:无|无覆盖缺口|无重要剧情缺口|无未处理的重大缺口)[。.]?$')
_NOTICE = re.compile(
    r'^(?:章节\s*)?(main:\d+)(?:\s*缺少配对资料，该章内容未纳入本节点摘要与检索范围。|'
    r'缺失于本节点章节列表（jump gap），其内容未被本摘要覆盖[。]?|'
    r'\s*未包含在本次提供的配对资料中，其内容未覆盖。)$')


def exclusion_notice_key(note):
    match = _NOTICE.fullmatch(note.strip()) if isinstance(note, str) else None
    return match[1] if match else None


def declared_compression_note(note):
    if not isinstance(note,str):
        return False
    if _NO_GAP.fullmatch(note.strip()):
        return True
    if note.strip()==_STRUCTURAL_COMPRESSION:
        return True
    for pattern in _COMPRESSION:
        match=pattern.fullmatch(note.strip())
        if match and not _MATERIAL.search(match['detail']):
            return True
    return False


def _artifact_node(view,index_id,summary_id):
    from .multilevel import read_multilevel_index, list_children, read_node_summary
    index = read_multilevel_index(view, index_id)
    artifact = view.store.get(summary_id, 'multilevel-summary')
    node = next((n for n in index['roots'] if n['node_id'] == artifact['node_id']), None)
    if node is None:
        for root in index['roots']:
            node = next((n for n in list_children(view,index_id,root['node_id']) if n['node_id'] == artifact['node_id']), None)
            if node is not None:
                break
    if node is None:
        raise NarrativeError('visibility_denied','coverage review node is outside this index')
    artifact = read_node_summary(view,summary_id,node)
    return index,node,artifact


def review_policy_exclusions(view, index_id, summary_id, *, persist=True):
    index,node,artifact = _artifact_node(view,index_id,summary_id)
    positions = {c['chapter_id']:i for i,c in enumerate(view.sources,1)}
    excluded = {view.archive.chapters[cid]['match_key']:cid for cid in index['excluded_source_ids']
        if node['start_position'] <= positions[cid] <= node['end_position']}
    notes = artifact['content']['coverage_notes']
    notices = []
    for i,note in enumerate(notes):
        key = exclusion_notice_key(note)
        if key in excluded:
            notices.append({'note_index':i,'note':note,'match_key':key,
                'excluded_source_id':excluded[key],'decision':'outside_requested_scope'})
    if not notices:
        return None
    value = {'version':VERSION,'index_id':index_id,'summary_id':summary_id,
        'node_id':node['node_id'],'policy_fingerprint':view.policy.fingerprint,
        'notes_digest':digest(notes),'notices':notices,
        'method':'exact_exclusion_notice_and_reserved_physical_position',
        'semantic_status':'not_reviewed; this decision concerns requested scope only'}
    ident = view.store.put('summary-coverage-scope-review',value) if persist else 'summary-coverage-scope-review:'+digest(value)
    return {'review_id':ident,**value}


def read_scope_review(view,index_id,review_id):
    value = view.store.get(review_id,'summary-coverage-scope-review')
    expected = review_policy_exclusions(view,index_id,value['summary_id'],persist=False)
    if expected is None or expected != {'review_id':review_id,**value}:
        raise NarrativeError('stale_summary','coverage scope review differs from exact policy/note/version')
    return expected


def review_declared_compression(view,index_id,summary_id,*,persist=True):
    """Admit only reviewed protocol-misuse grammars, not arbitrary
    'normal compression' claims. This never asserts their narrative truth.
    """
    _,node,artifact = _artifact_node(view,index_id,summary_id)
    notes=artifact['content']['coverage_notes']
    notices=[]
    for i,note in enumerate(notes):
        if not declared_compression_note(note):
            continue
        side=re.match(r'^(\d+)章为Side章节',note)
        if side and 'main:'+side[1] not in {h['match_key'] for h in node['headers']}:
            continue
        notices.append({'note_index':i,'note':note,'decision':
            'declared_no_gap' if _NO_GAP.fullmatch(note.strip()) else 'declared_normal_coarsening'})
    if not notices:
        return None
    value={'version':COMPRESSION_VERSION,'index_id':index_id,'summary_id':summary_id,
        'node_id':node['node_id'],'policy_fingerprint':view.policy.fingerprint,
        'notes_digest':digest(notes),'notices':notices,
        'method':'strict_declared_compression_grammar_without_material_gap_clause',
        'reason':'note explicitly declares no material gap, or only normal overview coarsening; not semantic accuracy approval',
        'semantic_status':'unreviewed_navigation; not evidence that the summary is factually accurate'}
    ident=view.store.put('summary-coverage-compression-review',value) if persist else 'summary-coverage-compression-review:'+digest(value)
    return {'review_id':ident,**value}


def read_compression_review(view,index_id,review_id):
    value=view.store.get(review_id,'summary-coverage-compression-review')
    expected=review_declared_compression(view,index_id,value['summary_id'],persist=False)
    if expected is None or expected!={'review_id':review_id,**value}:
        raise NarrativeError('stale_summary','compression review differs from exact policy/note/version')
    return expected
