"""Bridge independent edition packets into the durable sequence engine."""
from copy import deepcopy
import json

from ..storage import digest
from .context import compile_reference_packet


CROSS_EDITION_RULES = (
    '跨版本连续翻译：当前目标是 Web 日文原文。当前 Web 正文、可见的 Web 历史和本分支已生成译文是事实依据。'
    '文库中文来自独立版本，只能参考语言风格和有证据的译名；它不与任何 Web 章节或段落构成平行译文。'
    '不得用文库材料补充、删除或改写 Web 事件、因果、人物关系、技能效果、时间顺序或知情状态。'
    '若文库与 Web 冲突，以 Web 为准；压缩摘要只作导航，不补造当前正文未出现的事实。'
    '逐个翻译所有非空原文段落，每个原文段落对应一个中文非空行，不合并或删除段落；不要输出章节标题或段落ID。'
)


def validate_reference_packet(packet, target_id):
    if not isinstance(packet, dict) or packet.get('schema') != 'edition-reference-packet-v1':
        raise ValueError('invalid cross-edition reference packet')
    if packet.get('reference_mode') != 'cross_edition' or packet.get('target_id') != target_id:
        raise ValueError('cross-edition packet is bound to another target or mode')
    if packet.get('fingerprint') != digest({k: v for k, v in packet.items() if k != 'fingerprint'}):
        raise ValueError('cross-edition packet fingerprint mismatch')
    for doc in packet.get('style_documents', []):
        if doc.get('semantic_status') != 'cross_edition_not_parallel' or 'style_reference' not in doc.get('allowed_uses', []):
            raise ValueError('cross-edition body lacks a style-only use boundary')
    return True


def validate_reference_selection(packet, selection):
    if (selection.get('schema') != 'edition-reference-selection-v1' or
            selection.get('packet_fingerprint') != packet['fingerprint'] or
            selection.get('fingerprint') != digest({k: v for k, v in selection.items() if k != 'fingerprint'})):
        raise ValueError('cross-edition reference selection fingerprint mismatch')
    for key, group in (('source_history_ids', 'source_history'), ('style_document_ids', 'style_documents')):
        ids = selection.get(key, [])
        available = {d['entry_id'] for d in packet.get(group, [])}
        if len(ids) != len(set(ids)) or not set(ids) <= available:
            raise ValueError('cross-edition selection contains an unknown or duplicate document')
    indices = selection.get('terminology_indices', [])
    if (any(type(i) is not int or i < 0 or i >= len(packet.get('terminology', [])) for i in indices) or
            len(indices) != len(set(indices))):
        raise ValueError('cross-edition selection contains invalid terminology indices')


def selected_reference_materials(packet, selection=None):
    if selection is None:
        return {key: packet.get(key, []) for key in ('source_history', 'style_documents', 'terminology')}
    validate_reference_selection(packet, selection)
    return {
        'source_history': [d for d in packet.get('source_history', []) if d['entry_id'] in selection['source_history_ids']],
        'style_documents': [d for d in packet.get('style_documents', []) if d['entry_id'] in selection['style_document_ids']],
        'terminology': [packet['terminology'][i] for i in selection['terminology_indices']],
    }


def reference_context_message(packet, selection=None):
    """Purpose-limit what the translator sees from a frozen ReferencePacket."""
    validate_reference_packet(packet, packet['target_id'])
    associations = packet.get('block_associations', {})
    # Whole-L2 association explanations can describe events later than the
    # current chapter. Keep only navigation IDs and evidence status.
    association_projection = {
        'authority': associations.get('authority', 'unreviewed_cross_edition_navigation'),
        'items': [{k: row.get(k) for k in ('source_node', 'reference_node', 'evidence_status')}
                  for row in associations.get('items', [])],
    }
    materials = selected_reference_materials(packet, selection)
    if selection is not None:
        # Revisions and native paragraph IDs remain in the frozen packet. The
        # prompt only needs citable aliases, prose and each document's purpose.
        def usage(row):
            return {k: row[k] for k in ('ref', 'text')}
        def document(doc):
            return {**{k: doc[k] for k in ('entry_id', 'edition_id', 'raw_title',
                'allowed_uses', 'semantic_status', 'body_selection', 'omitted_paragraphs', 'selection_reason') if k in doc},
                'rows': [usage(r) for r in doc['rows']]}
        materials = {
            'source_history': [document(d) for d in materials['source_history']],
            'style_documents': [document(d) for d in materials['style_documents']],
            'terminology': [{**{k: t[k] for k in ('source_form', 'target_form', 'source_description', 'authority',
                                                'rendering_kind') if k in t},
                'source_usage': usage(t['source_usage']), 'reference_usage': usage(t['reference_usage'])}
                for t in materials['terminology']],
        }
    projection = {
        'reference_mode': packet['reference_mode'],
        'visibility_policy': packet['visibility_policy'],
        'reference_exposure': packet.get('reference_exposure'),
        'source_history': deepcopy(materials['source_history']),
        'block_associations': association_projection,
        'terminology': deepcopy(materials['terminology']),
        'style_documents': deepcopy(materials['style_documents']),
        'omissions': deepcopy(packet.get('omissions', [])) + (deepcopy(selection['omissions']) if selection else []),
    }
    if packet.get('terminology_resolution_fingerprint'):
        projection['terminology_trace'] = deepcopy(packet.get('terminology_trace', []))
        projection['terminology_instruction'] = ('实体身份与当前称呼形式分开，爱称不自动扩写为实名。'
            '候选只证明外版实际用法；原文的命名解释、含混和匿名身份仍按Web保留。'
            '未匹配或省略只表示本次查找范围没有充分证据，不代表全书不存在。')
    if packet.get('terminology_check_profile') == 'name-forms-v1':
        projection['terminology_check_profile'] = 'name-forms-v1'
        projection['terminology_checks'] = (
            '翻译、审校和裁决均逐项核对本章实际使用的称呼与候选用例。区分同一对象和相同称呼形式，'
            '检查爱称是否被扩写为实名、昵称是否无依据地另造、命名解释是否仍忠实Web。'
            '有同一对象及同类称呼的直接用例时，偏离已有用法是可报告的一致性问题，不能仅以中文能读懂略过；'
            '若版本改写使沿用不适用，说明源文理由，保留未决或提出兼顾源文解释的方案。'
            '候选不是硬规则，不无条件替换，不从文库推断匿名身份或未披露事实。')
    return {'role': 'user', 'content': (
        '【独立版本参考；不是平行译文】以下中文只用于表达风格和有用的候选译名。'
        '关联、摘要和术语均未人工核验。它们可能覆盖当前 Web 位置之后或存在改写的内容；'
        '不要据此判断当前剧情、因果、人物状态、时间顺序或技能效果。当前 Web 原文和本分支历史优先。\n'
        + json.dumps(projection, ensure_ascii=False, separators=(',', ':'))
    )}


def _source_document(archive, entry_id):
    entry = archive.entries[entry_id]
    doc = archive.read(entry_id)
    paragraphs = [row['text'] for row in doc['rows']]
    text = '\n'.join(paragraphs)
    return {'chapter_id': entry_id, 'revision_id': entry['revision_id'],
            'language': entry['language'], 'order': entry['edition_order'],
            'match_key': f"main:{entry['edition_order']}", 'title': entry['raw_title'],
            'paragraph_ids': [r['paragraph_id'] for r in doc['rows']],
            'paragraph_texts': paragraphs, 'text': text, 'text_hash': digest(text)}


def _history_content(artifact):
    """Drop source-internal paragraph references before storing a navigation node."""
    content = deepcopy(artifact['content'])
    content.pop('errors', None)
    content.pop('warnings', None)
    content.pop('syntax_notes', None)
    for group in ('events', 'entities', 'temporal_claims', 'knowledge_claims'):
        for row in content.get(group, []):
            row.pop('evidence_ref', None)
            row.pop('node_id', None)
    return content


def _target_specific_associations(pipeline, associations, target_id):
    """Retain only event anchors whose Japanese evidence lies in this target."""
    kept = []
    for artifact in associations:
        if artifact.get('status') != 'complete':
            continue
        result = deepcopy(artifact)
        rows = []
        for pair in result.get('result', {}).get('associations', []):
            if pair.get('relation') != 'event_overlap' or target_id not in pipeline.nodes[pair['source_node']]['entry_ids']:
                continue
            source = pipeline.read_summary(pair['source_node'])
            events = {e['id']: e for e in source['content']['events']}
            registry = pipeline.archive.refs(pipeline.nodes[pair['source_node']]['entry_ids'])
            candidates = [pair] + pair.get('additional_event_matches', [])
            for candidate in candidates:
                event = events.get(candidate.get('source_event'))
                evidence = registry.get(event.get('evidence_ref')) if event else None
                if candidate.get('relation') != 'event_overlap' or not evidence or evidence['entry_id'] != target_id:
                    continue
                rows.append({**{k: v for k, v in pair.items() if k != 'additional_event_matches'},
                             'source_event': candidate['source_event'],
                             'reference_event': candidate.get('reference_event'),
                             'additional_event_matches': []})
        if rows:
            result['result']['associations'] = rows
            result['fingerprint'] = digest({k: v for k, v in result.items() if k != 'fingerprint'})
            kept.append(result)
    return kept


def _target_specific_entities(pipeline, entity_matches, target_id):
    """Use only observed prior/current source forms; hide L1-wide descriptions."""
    target_order = pipeline.archive.entries[target_id]['edition_order']
    registry = {r['ref']: r for eid in pipeline.archive.entries
                for r in pipeline.archive.read(eid)['rows']}
    result = []
    for original in entity_matches:
        match = deepcopy(original)
        mappings = []
        for row in match.get('mappings', []):
            if row.get('status') != 'supported' or not row.get('source_form'):
                continue
            source_usage = registry.get(row.get('source_usage_ref'))
            reference_usage = registry.get(row.get('reference_usage_ref'))
            if not source_usage or not reference_usage:
                continue
            if pipeline.archive.entries[source_usage['entry_id']]['edition_order'] > target_order:
                continue
            mappings.append({**row,
                'source_description': '由当前 Web 阅读位置以前或当前原文用例支持的译名候选；未人工确认。',
                'reason': '两侧存在可定位用例；只作为当前语境中的译名候选。'})
        match['mappings'] = mappings
        match['conflicts'] = [c for c in match.get('conflicts', [])]
        if mappings:
            match['fingerprint'] = digest({k: v for k, v in match.items() if k != 'fingerprint'})
            result.append(match)
    return result

