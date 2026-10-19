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


def build_sequence_inputs(pipeline, target_ids, *, associations=(), entity_matches=(),
                          packet_token_budget=24000, resolve_terminology=False,
                          reference_options=None, terminology_budgets=None, style_reference_entry_ids=()):
    """Freeze Web-only history plus per-target cross-edition reference packets."""
    target_ids = list(target_ids)
    if not target_ids or len(set(target_ids)) != len(target_ids):
        raise ValueError('continuous run requires unique Web targets')
    web_edition = pipeline.archive.entries[target_ids[0]]['edition_id']
    targets = [pipeline.archive.entries[cid] for cid in target_ids]
    orders = [e['edition_order'] for e in targets]
    if any(e['edition_id'] != web_edition or e['language'] != 'ja' for e in targets) or orders != sorted(set(orders)):
        raise ValueError('continuous targets must be unique, ordered chapters from one Japanese edition')
    all_web = pipeline.archive.documents(web_edition)
    window = [d['entry_id'] for d in all_web if orders[0] <= d['edition_order'] <= orders[-1]]
    if window != target_ids:
        raise ValueError('continuous window skips an existing Web chapter')

    first_order = orders[0]
    history_nodes = []
    source_nodes = sorted((n for n in pipeline.nodes.values()
                           if n['level'] == 2 and n['edition_id'] == web_edition
                           and n['reading_range'][1] < first_order), key=lambda n: n['reading_range'])
    for node in source_nodes:
        artifact = pipeline.read_summary(node['node_id'])
        if not artifact:
            continue
        dependencies = [_source_document(pipeline.archive, eid) for eid in node['entry_ids']]
        history_nodes.append({'id': 'edition-l2:' + node['node_id'],
            'source_ids': list(node['entry_ids']),
            'start_position': node['reading_range'][0], 'end_position': node['reading_range'][1],
            'summary': json.dumps(_history_content(artifact), ensure_ascii=False, separators=(',', ':')),
            'dependencies': dependencies, 'semantic_status': 'candidate_unreviewed',
            'origin': 'frozen-independent-web-index', 'origin_fingerprint': artifact['fingerprint']})

    options = reference_options or {}
    budgets = terminology_budgets or {}
    packets, resolutions = {}, {}
    for target_id in target_ids:
        scoped_associations = _target_specific_associations(pipeline, associations, target_id)
        scoped_entities = _target_specific_entities(pipeline, entity_matches, target_id)
        resolution = None
        if resolve_terminology:
            from .terminology import resolve_target_terminology
            resolution = resolve_target_terminology(pipeline, target_id, associations, scoped_entities,
                max_terms=options.get('terminology_max_terms', 16),
                input_tokens=budgets.get('terminology_input_tokens', 16000),
                output_tokens=budgets.get('terminology_output_tokens', 4096),
                max_documents=options.get('terminology_max_documents', 12),
                radius=options.get('terminology_radius', 2),
                discover=options.get('discover_terms', True))
            resolutions[target_id] = resolution
        packet = compile_reference_packet(pipeline, target_id, scoped_associations, scoped_entities,
            visibility='reference_assisted', token_budget=packet_token_budget, n_chapter=3,
            style_mode='full', radius=2, terminology_resolution=resolution,
            style_associations=associations, style_reference_entry_ids=style_reference_entry_ids,
            style_max_documents=options.get('style_max_documents', 3),
            style_excerpt_paragraphs=options.get('style_excerpt_paragraphs', 48),
            local_evidence_policy='event-and-terms-v2' if resolve_terminology else 'event-only-v1')
        # The sequence checkpoint already carries prior Web L2 summaries; no
        # duplicate full source-history text belongs in the cross-edition block.
        packet['navigation'] = []
        packet['source_edition_id'] = web_edition
        packet['packet_scope'] = 'target-specific-event-evidence-and-local-style'
        packet['fingerprint'] = digest({k: v for k, v in packet.items() if k != 'fingerprint'})
        validate_reference_packet(packet, target_id)
        packets[target_id] = packet

    inputs = {'targets': [_source_document(pipeline.archive, cid) for cid in target_ids],
            'history_pairs': [], 'history_nodes': history_nodes, 'edition_reference_packets': packets,
            'reference_mode': 'cross_edition', 'reference_translations_in_inputs': False,
            'reserved_source_ids': [], 'core': [], 'corpus_identity': pipeline.archive.fingerprint,
            'development_only': True, 'preserve_paragraphs': True,
            'explicit_gaps': [{'source_ids': [d['entry_id'] for d in all_web
                if d['edition_order'] < first_order and d['entry_id'] not in
                {cid for n in history_nodes for cid in n['source_ids']}],
                'reason': 'Web historical index not built in the frozen pilot'}]}
    if resolve_terminology:
        inputs['edition_terminology_resolutions'] = resolutions
    return inputs


class CrossEditionSequenceBridge:
    """Idempotently append the frozen packet to each SequenceEngine context."""
    def __init__(self, packets):
        self.packets = dict(packets)

    def __call__(self, view, context):
        target_id = context['source']['chapter_id']
        packet = self.packets.get(target_id)
        if packet is None:
            raise ValueError('missing frozen cross-edition packet for target')
        validate_reference_packet(packet, target_id)
        actual = view.body(target_id, task=True)
        target_dep = next((d for d in packet['dependencies'] if d['entry_id'] == target_id), None)
        if not target_dep or target_dep['revision_id'] != actual['revision_id']:
            raise ValueError('cross-edition packet source revision differs from sequence source')
        reference = {'target_id': target_id, 'packet_fingerprint': packet['fingerprint'],
                     'external_dependencies': deepcopy(packet.get('dependencies', [])),
                     'association_fingerprints': list(packet.get('association_fingerprints', [])),
                     'entity_match_fingerprints': list(packet.get('entity_match_fingerprints', [])),
                     'visibility_policy': packet['visibility_policy']}
        previous = context.get('edition_reference')
        if previous:
            if previous != reference:
                raise ValueError('saved sequence context has a different cross-edition packet')
            selection = context.get('edition_reference_selection')
            if selection is not None:
                message = reference_context_message(packet, selection)
                blocks = [b for b in context['prompt_blocks'] if b['kind'] == 'cross_edition_reference']
                # Object storage sorts JSON keys, while the saved prompt keeps
                # its original bytes. Compare payloads without rewriting that
                # prompt, so reopening cannot invalidate a legal selection.
                def unpack(text):
                    header, body = text.split('\n', 1)
                    return header, json.loads(body)
                if (len(blocks) != 1 or unpack(blocks[0]['text']) != unpack(message['content']) or
                        {'role': 'user', 'content': blocks[0]['text']} not in context['messages']):
                    raise ValueError('saved sequence context differs from its frozen reference selection')
            return context

        messages = list(context['messages'])
        if not messages or messages[0].get('role') != 'system':
            raise ValueError('sequence context has no leading system message')
        rules = CROSS_EDITION_RULES
        if context.get('output_profile') == 'source-records-v1':
            rules = rules.replace('不要输出章节标题或段落ID。', '源段ID仅用于输出协议，程序将其从最终正文去除。')
        messages[0] = {**messages[0], 'content': messages[0]['content'] + '\n\n' + rules}
        represented = {cid for row in context['coverage'] if row['representation'] == 'full_pair'
                       for cid in row['source_ids']}
        selection = {'schema': 'edition-reference-selection-v1', 'packet_fingerprint': packet['fingerprint'],
            'source_history_ids': [d['entry_id'] for d in packet.get('source_history', []) if d['entry_id'] not in represented],
            'style_document_ids': [d['entry_id'] for d in packet.get('style_documents', [])],
            'terminology_indices': list(range(len(packet.get('terminology', [])))),
            'omissions': [{'kind': 'source_history', 'entry_id': d['entry_id'], 'reason': 'already_in_full_web_pair'}
                         for d in packet.get('source_history', []) if d['entry_id'] in represented]}
        from ..sequence.schemas import SequenceSpec
        from ..sequence.context import budget_for, input_soft_limit
        spec = SequenceSpec.from_dict(view.repo.manifest['spec'])
        # Optional cross-edition references share the final calibrated budget
        # with history and the target; they do not get an independent allowance.
        while True:
            selection['fingerprint'] = digest({k: v for k, v in selection.items() if k != 'fingerprint'})
            message = reference_context_message(packet, selection)
            report = budget_for(view.repo, spec, view.counter, messages[:-1] + [message, messages[-1]])
            if report['fits'] and report['safe_input_tokens'] <= input_soft_limit(spec, report):
                break
            if selection['style_document_ids']:
                removed = selection['style_document_ids'].pop()
                selection['omissions'].append({'kind': 'style_document', 'entry_id': removed, 'reason': 'remaining_context_budget'})
            elif selection['terminology_indices']:
                removed = selection['terminology_indices'].pop()
                selection['omissions'].append({'kind': 'terminology', 'source_form': packet['terminology'][removed]['source_form'],
                                              'reason': 'remaining_context_budget'})
            else:
                # Mandatory source history stays present. Existing sequence
                # checkpoint/budget handling decides whether it can fit.
                break
        # The target's source turn stays last, preserving SequenceEngine's
        # translation-turn capture and stable target parsing.
        messages.insert(len(messages) - 1, message)
        enriched = dict(context)
        enriched['messages'] = messages
        enriched['edition_reference'] = reference
        enriched['edition_reference_selection'] = selection
        enriched['prompt_blocks'] = list(context.get('prompt_blocks', [])) + [{
            'kind': 'cross_edition_reference', 'id': packet['fingerprint'],
            'text': message['content'], 'authority': 'style_and_terminology_candidate_only'}]
        return enriched

