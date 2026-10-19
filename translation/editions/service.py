"""Explicit independent-index orchestration; all model output remains unreviewed."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..storage import digest, read_json, write_json
from .archive import document_dependency
from .calls import EditionCaller
from .prompts import (ENTITY_SYSTEM, association_messages, j, summary_messages)
from .protocol import parse_associations, parse_entity_matches, parse_summary


class EditionPipeline:
    def __init__(self, archive, plans, provider, counter, capacity, folder, *, index_source=None, **limits):
        self.archive, self.counter, self.capacity = archive, counter, capacity
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        # Index artifacts may be frozen in a previous experiment while a new
        # translation branch writes its own durable calls and results.  Keeping
        # the read root separate prevents a continuation run from silently
        # rebuilding or mutating the index experiment.
        self.index_source = Path(index_source) if index_source else self.folder
        self.plans = plans
        self.nodes = {n['node_id']: n for p in plans for n in p['parents'] + p['children']}
        self.caller = EditionCaller(provider, counter, capacity, self.folder / 'calls', **limits)
        for plan in plans:
            path = self.folder / 'plans' / (plan['identity']['edition_id'] + '.json')
            if path.exists() and read_json(path) != plan:
                raise ValueError('index plan changed; choose a new output folder/layout')
            write_json(path, plan)

    def read_summary(self, node_id):
        node = self.nodes[node_id]
        self.archive.validate_dependencies(node['dependencies'])
        path = self.index_source / 'summaries' / (node_id + '.json')
        if not path.exists():
            return None
        artifact = read_json(path)
        if artifact['node'] != node or artifact['fingerprint'] != digest(artifact['content']):
            raise ValueError('stale/corrupted independent index summary')
        return artifact

    def _build_child(self, node_id, output_tokens):
        node = self.nodes[node_id]
        docs = [self.archive.read(eid) for eid in node['entry_ids']]
        refs = self.archive.refs(node['entry_ids'])
        predecessors = sorted((n for n in self.nodes.values() if n['level'] == 2 and
            n.get('parent_id') == node['parent_id'] and n['reading_range'][1] < node['reading_range'][0]),
            key=lambda n: n['reading_range'])
        navigation = []
        for previous in predecessors[-2:]:
            artifact = self.read_summary(previous['node_id'])
            if artifact:
                navigation.append({'node_id': previous['node_id'], 'overview': artifact['content']['overview']})
        outcome = self.caller.structured('edition-index:' + node_id,
            summary_messages(node, docs, navigation), output_tokens,
            lambda text: parse_summary(text, node, refs))
        if outcome['status'] != 'complete':
            write_json(self.folder / 'failures' / (node_id + '.json'), outcome)
            return {'node_id': node_id, **outcome}
        artifact = {'node': node, 'content': outcome['result'], 'generation_id': outcome['generation_id'],
                    'attempts': outcome['attempts'], 'dependencies': node['dependencies'],
                    'fingerprint': digest(outcome['result'])}
        write_json(self.folder / 'summaries' / (node_id + '.json'), artifact)
        return {'node_id': node_id, 'status': 'complete', 'fingerprint': artifact['fingerprint'],
                'warnings': artifact['content']['warnings']}

    def ensure_index_nodes(self, node_ids, *, workers=3, output_tokens=None):
        output_tokens = output_tokens or min(12288, self.capacity.max_output_tokens)
        if len(node_ids) != len(set(node_ids)):
            raise ValueError('duplicate build node')
        groups = {}
        for nid in node_ids:
            node = self.nodes[nid]
            if node['level'] != 2:
                raise ValueError('use build_parent explicitly for L1')
            groups.setdefault(node['parent_id'], []).append(nid)

        def build_group(ids):
            results = []
            for nid in sorted(ids, key=lambda i: self.nodes[i]['reading_range']):
                if self.read_summary(nid):
                    results.append({'node_id': nid, 'status': 'complete', 'cache_hit': True})
                else:
                    results.append(self._build_child(nid, output_tokens))
            return results

        results = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(build_group, ids) for ids in groups.values()]
            for future in as_completed(futures):
                results.extend(future.result())
        return {'results': results, 'complete': all(r['status'] == 'complete' for r in results)}

    def entity_catalog(self, parent_id):
        parent = self.nodes[parent_id]
        entities, covered, missing = [], [], []
        for nid in parent['child_ids']:
            summary = self.read_summary(nid)
            if summary is None:
                missing.append(nid)
                continue
            covered.append(nid)
            for e in summary['content']['entities']:
                row = self.archive.refs(self.nodes[nid]['entry_ids']).get(e['evidence_ref'])
                entities.append({**e, 'usage': row['text'] if row else None,
                                 'parent_id': parent_id, 'edition_id': parent['edition_id']})
        return {'parent_id': parent_id, 'entities': entities, 'covered_child_ids': covered,
                'missing_child_ids': missing, 'coverage_complete': not missing,
                'entities_exhaustive': False}

    def build_parent(self, parent_id, *, allow_partial=False, output_tokens=8192):
        parent = self.nodes[parent_id]
        catalog = self.entity_catalog(parent_id)
        if catalog['missing_child_ids'] and not allow_partial:
            return {'status': 'not_built', 'missing_child_ids': catalog['missing_child_ids']}
        children = [self.read_summary(nid) for nid in catalog['covered_child_ids']]
        if not children:
            return {'status': 'not_built', 'missing_child_ids': parent['child_ids']}
        messages = [{'role': 'system', 'content': '根据同一版本子块摘要生成连贯的父块剧情概括，软目标2500字。保留线索、因果和叙述顺序，不合并未知身份，不用作品记忆。只输出概括正文。'},
                    {'role': 'user', 'content': j({'edition_id': parent['edition_id'],
                        'coverage': {k: v for k, v in catalog.items() if k != 'entities'},
                        'children': [{'node_id': c['node']['node_id'], 'overview': c['content']['overview'],
                                      'events': c['content']['events'], 'temporal_claims': c['content']['temporal_claims'],
                                      'knowledge_claims': c['content']['knowledge_claims']} for c in children]})}]
        raw = self.caller.call('edition-parent:' + parent_id, messages, output_tokens)
        artifact = {'parent_id': parent_id, 'overview': raw['generation']['text'],
                    'build_method': 'child_summary_rollup', 'entity_catalog': catalog,
                    'child_fingerprints': [c['fingerprint'] for c in children],
                    'generation_id': raw['id'], 'semantic_status': 'model_extracted_unreviewed'}
        write_json(self.folder / 'parents' / (parent_id + '.json'), artifact)
        return artifact

    def match_blocks(self, source_node_ids, reference_node_ids, *, output_tokens=None):
        output_tokens = output_tokens or min(16384, self.capacity.max_output_tokens)
        sources = [self.read_summary(nid) for nid in source_node_ids]
        references = [self.read_summary(nid) for nid in reference_node_ids]
        if not sources or not references or any(s is None for s in sources+references):
            return {'status': 'not_built', 'associations': [], 'search_complete': False}
        s = [a['content'] for a in sources]
        r = [a['content'] for a in references]
        outcome = self.caller.structured('edition-block-association', association_messages(s, r, self.nodes),
                                         output_tokens, lambda text: parse_associations(text, s, r))
        artifact = {'status': outcome['status'], 'source_node_ids': source_node_ids,
                    'reference_node_ids': reference_node_ids, 'search_scope': 'explicit_candidate_nodes',
                    'search_complete': False, 'source_fingerprints': [a['fingerprint'] for a in sources],
                    'reference_fingerprints': [a['fingerprint'] for a in references], **outcome}
        artifact['fingerprint'] = digest(artifact)
        write_json(self.folder / 'associations' / (artifact['fingerprint'] + '.json'), artifact)
        return artifact

    def match_entities(self, source_parent_id, reference_parent_ids, *, output_tokens=None, batch_size=40):
        output_tokens = output_tokens or min(12288, self.capacity.max_output_tokens)
        source = self.entity_catalog(source_parent_id)
        references = [self.entity_catalog(nid) for nid in reference_parent_ids]
        reference_entities = [e for c in references for e in c['entities']]
        reference_rows = self.archive.refs([eid for nid in reference_parent_ids
            for child in self.nodes[nid]['child_ids'] for eid in self.nodes[child]['entry_ids']])
        outcomes, mappings = [], []
        # Source batches always see the same full candidate reference directory.
        # The final conflict check covers the union rather than silently replacing.
        for start in range(0, len(source['entities']), batch_size):
            batch = source['entities'][start:start+batch_size]
            messages = [{'role': 'system', 'content': ENTITY_SYSTEM}, {'role': 'user', 'content': j({
                'source_coverage': {k: v for k, v in source.items() if k != 'entities'},
                'reference_coverage': [{k: v for k, v in c.items() if k != 'entities'} for c in references],
                'source_entities': batch, 'reference_entities': reference_entities})}]
            outcome = self.caller.structured('edition-entity-matching:' + str(start), messages, output_tokens,
                lambda text: parse_entity_matches(text, batch, reference_entities, reference_rows))
            outcomes.append(outcome)
            if outcome['status'] == 'complete':
                mappings.extend(outcome['result']['mappings'])
        # Several block-local IDs with the same form are not assumed identical;
        # conflicting form-level renderings are surfaced to the consumer.
        forms = {}
        for row in mappings:
            if row['status'] == 'supported':
                forms.setdefault(row['source_form'], set()).add(row['target_form'])
        conflicts = [{'source_form': f, 'target_forms': sorted(ts)} for f, ts in forms.items() if len(ts) > 1]
        artifact = {'status': 'complete' if all(o['status'] == 'complete' for o in outcomes) else 'partial',
                    'source_parent_id': source_parent_id, 'reference_parent_ids': reference_parent_ids,
                    'source_catalog': source, 'reference_catalogs': references, 'mappings': mappings,
                    'conflicts': conflicts, 'batches': outcomes}
        artifact['fingerprint'] = digest(artifact)
        write_json(self.folder / 'entity-mappings' / (artifact['fingerprint'] + '.json'), artifact)
        return artifact

    def lookup_reference(self, node_id):
        """Read only; never creates a summary or sends a model request."""
        summary = self.read_summary(node_id)
        return {'status': 'found' if summary else 'not_built', 'summary': summary,
                'suggested_build_nodes': [] if summary else [node_id]}
