"""Provisional L2/L1 previews built from staged, not-yet-reviewed translations.

These nodes never enter the accepted index. They exist so the next window can
use compact summaries instead of injecting an entire unreviewed window as text.
"""
import json

from ..narrative.extraction import PARSER_VERSION, STABLE_PARSER_VERSION
from ..narrative.length import summary_length_info
from ..references import STABLE
from ..review.versions import read_version
from ..storage import digest
from .knowledge import ExtractionRecovery, joint_messages, parser
from .requests import SequenceCaller, sequence_limits

NEAREST_RAW_CHAPTERS = 3


def _generation(window, versions):
    return digest({'window': [doc['chapter_id'] for doc in window],
                   'versions': {doc['chapter_id']: versions[doc['chapter_id']]
                                for doc in window}})


def _translation_doc(source, version, version_id):
    paragraphs = version['text'].splitlines()
    return {'chapter_id': 'generated:provisional:' + digest(version_id)[:24],
            'revision_id': version_id, 'language': 'zh',
            'match_key': source.get('match_key'), 'order': source.get('order'),
            'title': source.get('title'), 'text': version['text'],
            'text_hash': digest(version['text']),
            'paragraph_ids': ['generated:provisional:' + digest([version_id, index])[:24]
                              for index in range(len(paragraphs))],
            'paragraph_texts': paragraphs, 'source': 'provisional',
            'canonical_source_id': source['chapter_id']}


def _pairs(job, window, versions):
    pairs = []
    for doc in window:
        source = {**doc, 'language': 'ja'}
        view = job.engine.view(doc['chapter_id'])
        version = read_version(view, versions[doc['chapter_id']])
        pairs.append({'source': source,
                      'translation': [_translation_doc(source, version,
                                                       versions[doc['chapter_id']])]})
    return pairs


def _records(result, node_id):
    names = {record['id']: node_id + ':' + record['id']
             for record in result.get('records', []) if record.get('id')}
    records = []
    for record in result.get('records', []):
        fields = {key: list(value) for key, value in record['fields'].items()}
        if record['type'] == 'TEMPORAL':
            for key in ('from_event', 'to_event'):
                fields[key] = [names[item] for item in fields.get(key, [])]
        records.append({**record, 'id': names.get(record.get('id')), 'fields': fields,
                        'node_id': node_id,
                        'authority': ('provisional_rendering'
                                      if record['type'] == 'ENTITY' and fields.get('target_form')
                                      else 'candidate_unreviewed')})
    return records


def build_provisional_index(job, window, versions):
    """Build/reuse a provisional index for one translated window."""
    spec = job.engine.spec
    repo = job.repo
    generation = _generation(window, versions)
    for ident, value in repo.store.objects('sequence-provisional-index'):
        if value.get('run_id') == repo.run_id and value.get('generation') == generation:
            return value
    pairs = _pairs(job, window, versions)
    by_id = {pair['source']['chapter_id']: pair for pair in pairs}
    history = []
    from .hierarchy import latest_nodes, latest_incremental_nodes
    for node in latest_nodes(repo) + latest_incremental_nodes(repo):
        if node.get('level') == 1:
            history.append({'id': node.get('id'), 'summary': node.get('summary')})
    recovery = ExtractionRecovery(
        repo.store, job.engine.counter, job.engine.provider,
        provider_identity=repo.manifest['provider'], limits=sequence_limits(repo,spec),
        max_retries=spec.max_attempts - 1, capacity=spec.capacity,
        max_output_tokens=spec.capacity.max_output_tokens)
    parser_version = (STABLE_PARSER_VERSION if spec.reference_profile == STABLE
                      else PARSER_VERSION)
    l2_nodes = []
    previous = []
    size = max(1, int(spec.child_max_chapters))
    for index in range(0, len(window), size):
        chunk = window[index:index + size]
        chunk_ids = [doc['chapter_id'] for doc in chunk]
        chunk_pairs = [by_id[chapter_id] for chapter_id in chunk_ids]
        node_id = 'provisional-l2:' + digest({'generation': generation,
                                              'chunk': chunk_ids})[:24]
        messages, refs = joint_messages(chunk_pairs, history, previous,
                                        reference_profile=spec.reference_profile)
        pair_hash = digest(chunk_pairs)
        result = recovery.run(node_id + ':extract:' + pair_hash, messages, parser(refs),
                              parser_version=parser_version,
                              max_tokens=spec.roles['indexer'].requested_output_tokens)
        node = {'schema': 'sequence-provisional-node-v1', 'run_id': repo.run_id,
                'node_id': node_id, 'level': 2,
                'source_ids': chunk_ids,
                'version_ids': [versions[chapter_id] for chapter_id in chunk_ids],
                'commit_ids': [versions[chapter_id] for chapter_id in chunk_ids],
                'start_position': chunk[0].get('order'),
                'end_position': chunk[-1].get('order'),
                'pair_hash': pair_hash, 'summary': result.get('summary'),
                'summary_length': summary_length_info(result.get('summary') or ''),
                'records': _records(result, node_id),
                'state': result.get('state'),
                'failure_reason': result.get('failure_reason'),
                'semantic_status': 'provisional', 'generation': generation,
                'build_method': 'provisional_direct'}
        node['id'] = repo.store.put('sequence-provisional-node', node)
        l2_nodes.append(node)
        previous.append({'node_id': node_id, 'revision_id': node['id'],
                         'summary': node.get('summary') or '【provisional 摘要缺失】'})
    l1_id = 'provisional-l1:' + digest({'generation': generation})[:24]
    if any(node.get('summary') for node in l2_nodes):
        from .context import _parent_overview
        caller = SequenceCaller(repo, spec, job.engine.counter, job.engine.provider)
        summary, summary_id = _parent_overview(repo, caller, l2_nodes, l1_id)
    else:
        summary, summary_id = '【provisional L1 摘要缺失】', None
    l1 = {'schema': 'sequence-provisional-node-v1', 'run_id': repo.run_id,
          'node_id': l1_id, 'level': 1, 'source_ids': [d['chapter_id'] for d in window],
          'version_ids': [versions[d['chapter_id']] for d in window],
          'commit_ids': [versions[d['chapter_id']] for d in window],
          'start_position': window[0].get('order'),
          'end_position': window[-1].get('order'),
          'summary': summary, 'summary_id': summary_id,
          'summary_length': summary_length_info(summary or ''),
          'records': [record for node in l2_nodes for record in node.get('records', [])],
          'state': ('partial' if any(node.get('state') in {'partial', 'summary_only'}
                                     for node in l2_nodes) else 'extracted'),
          'semantic_status': 'provisional', 'generation': generation,
          'build_method': 'provisional_rollup'}
    l1['id'] = repo.store.put('sequence-provisional-node', l1)
    nearest = []
    for doc in window[-NEAREST_RAW_CHAPTERS:]:
        version = read_version(job.engine.view(doc['chapter_id']),
                               versions[doc['chapter_id']])
        nearest.append({'source_id': doc['chapter_id'], 'title': doc.get('title', ''),
                        'text': version['text'], 'version_id': versions[doc['chapter_id']]})
    payload = {'schema': 'sequence-provisional-index-v1', 'run_id': repo.run_id,
               'window': [doc['chapter_id'] for doc in window],
               'generation': generation,
               'version_ids': {doc['chapter_id']: versions[doc['chapter_id']]
                               for doc in window},
               'l1': l1, 'l2': l2_nodes, 'nearest': nearest,
               'status': 'provisional'}
    payload['id'] = repo.store.put('sequence-provisional-index', payload)
    return payload


def _fit_tail(text, limit, counter):
    if counter is None or limit is None or counter.count(text) <= limit:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high) // 2
        if counter.count(text[middle:]) <= limit:
            high = middle
        else:
            low = middle + 1
    return '【前部已截断】\n' + text[low:]


def provisional_message(index, *, counter=None, max_tokens=None):
    if not index:
        return None
    parts = ['【上一窗口 provisional L1/L2（未审校，仅供导航；'
             '专名以冻结 glossary 和已确认历史为准，不得当作已确认事实）】']
    l1 = index.get('l1') or {}
    if l1.get('summary'):
        parts.append('【L1 概览】\n' + l1['summary'])
    parts.append('【L2 分块】')
    for node in index.get('l2', []):
        parts.append('[%s–%s] %s' % (node.get('start_position'), node.get('end_position'),
                                     node.get('summary') or '【摘要缺失】'))
    base = '\n\n'.join(parts)
    if max_tokens is not None and counter is not None:
        if max_tokens < 4096:
            return None
        base = _fit_tail(base, max_tokens, counter)
        remaining = max_tokens - counter.count(base)
        if remaining > 512 and index.get('nearest'):
            raw = '\n\n'.join('### ' + doc.get('title', '') + '\n' + doc.get('text', '')
                              for doc in index['nearest'])
            raw = _fit_tail(raw, remaining, counter)
            base = base + '\n\n【最近章节 provisional 原文；仅供衔接】\n' + raw
    return {'role': 'user', 'content': base}
