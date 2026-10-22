"""Use the existing tolerant flat parser; bind references without guessing facts."""
import re
import unicodedata

from ..review.protocol import parse_records

SCHEMA = {
    'OVERVIEW': {'text'},
    'EVENT': {'text', 'actor', 'action', 'cause', 'result', 'evidence_ref'},
    'ENTITY': {'form', 'kind', 'description', 'evidence_ref'},
    'TEMPORAL': {'text', 'evidence_ref'},
    'KNOWLEDGE': {'text', 'knower', 'evidence_ref'},
    'COVERAGE': {'processed_all', 'omissions', 'entities_exhaustive'},
    'ASSOC': {'source_node', 'reference_node', 'relation', 'source_event', 'reference_event', 'reason', 'differences'},
    'ENTITY_MATCH': {'source_entity', 'reference_entity', 'reason', 'status',
                     'reference_rendering', 'rendering_kind', 'rendering_evidence_ref'},
    'SUMMARY': {'reviewed_all', 'semantic_coverage_complete', 'zero_findings'},
}


def flat(text, allowed):
    return parse_records(text, set(allowed), field_schema=SCHEMA,
                         syntax_profile='tolerant-records', allow_preamble=True,
                         implicit_fields={'OVERVIEW': 'text'})


def parse_summary(text, node, refs):
    parsed = flat(text, {'OVERVIEW', 'EVENT', 'ENTITY', 'TEMPORAL', 'KNOWLEDGE', 'COVERAGE'})
    errors, warnings = list(parsed['errors']), []
    result = {'node_id': node['node_id'], 'overview': '', 'events': [], 'entities': [],
              'temporal_claims': [], 'knowledge_claims': [], 'coverage': {}}
    overviews = [r for r in parsed['records'] if r['record'] == 'OVERVIEW']
    coverages = [r for r in parsed['records'] if r['record'] == 'COVERAGE']
    if len(overviews) != 1 or not overviews[0]['fields'].get('text'):
        errors.append('one_nonempty_overview_required')
    else:
        result['overview'] = overviews[0]['fields']['text']
    if len(coverages) != 1 or coverages[0]['fields'].get('processed_all') not in {'yes', 'no'}:
        errors.append('coverage_required')
    else:
        result['coverage'] = coverages[0]['fields']
    buckets = {'EVENT': 'events', 'ENTITY': 'entities', 'TEMPORAL': 'temporal_claims', 'KNOWLEDGE': 'knowledge_claims'}
    for record in parsed['records']:
        kind = record['record']
        if kind not in buckets:
            continue
        fields = record['fields']
        required = {'ENTITY': ('form', 'kind', 'description'), 'EVENT': ('text',),
                    'TEMPORAL': ('text',), 'KNOWLEDGE': ('text', 'knower')}[kind]
        if any(not fields.get(f) for f in required):
            errors.append('missing_fields:' + kind + ':' + record['id'])
            continue
        ref = fields.get('evidence_ref', '')
        # A quoted suffix is tolerated, but its quotation must really occur.
        alias, separator, quote = ref.partition('|')
        alias = alias.strip()
        row = refs.get(alias)
        supported = row is not None and (not separator or quote.strip() in row['text'])
        if kind == 'ENTITY' and row and fields['form'] not in row['text']:
            supported = False
        if not supported:
            warnings.append('missing_evidence:' + kind + ':' + record['id'])
        result[buckets[kind]].append({**fields, 'id': node['node_id'] + '.' + record['id'],
            'node_id': node['node_id'], 'evidence_ref': alias if supported else None,
            'evidence_status': 'bound_usage' if supported else 'missing_reference'})
    if not result['events']:
        errors.append('event_details_required')
    if not result['entities']:
        warnings.append('empty_entity_extraction_not_exhaustive')
    result.update(errors=errors, warnings=warnings, syntax_notes=parsed.get('notes', []),
                  complete=not errors, semantic_status='model_extracted_unreviewed')
    return result


def parse_associations(text, sources, references):
    known = {s['node_id'] for s in sources + references}
    lines, header_errors, header_notes = text.splitlines(), [], []
    normalized = []
    for index, line in enumerate(lines):
        match = re.fullmatch(r'@@ASSOC\s+(\S+)\s+(\S+)\s*', unicodedata.normalize('NFKC', line).strip(), re.I)
        if match and set(match.groups()) <= known:
            source_id, reference_id = match.groups()
            normalized.append('@@ASSOC binding' + str(index))
            following = []
            for next_line in lines[index+1:]:
                if unicodedata.normalize('NFKC', next_line).strip().startswith('@@'):
                    break
                following.append(next_line)
            for key, value in (('source_node', source_id), ('reference_node', reference_id)):
                explicit = next((m[1].strip() for raw in following if
                    (m := re.match(r'^' + key + r'\s*[:：]\s*(.*)', unicodedata.normalize('NFKC', raw).strip()))), None)
                if explicit is None:
                    normalized.append(key + ': ' + value)
                elif explicit != value:
                    header_errors.append('header_binding_conflict:' + key)
            header_notes.append('explicit_pair_ids_from_header')
        else:
            normalized.append(line)
    text = '\n'.join(normalized)
    parsed = flat(text, {'ASSOC'})
    # Header IDs label output rows, not input objects. Actual bindings are the
    # explicit source/reference pair; repeated a1 labels do not erase evidence.
    errors = [e for e in parsed['errors'] if e != 'duplicate_record_id'] + header_errors
    warnings = ['repeated_output_header_ids'] if 'duplicate_record_id' in parsed['errors'] else []
    rows, quarantine = [], []
    source_map = {x['node_id']: x for x in sources}
    reference_map = {x['node_id']: x for x in references}
    expected = {(s, r) for s in source_map for r in reference_map}
    seen = set()
    for record in parsed['records']:
        f = record['fields']
        key = f.get('source_node'), f.get('reference_node')
        if key not in expected:
            quarantine.append({'fields': f, 'reason': 'pair_not_requested_or_wrong_edition_axis'})
            continue
        seen.add(key)
        relation = f.get('relation')
        if relation not in {'event_overlap', 'related_context', 'unrelated', 'uncertain'} or not f.get('reason'):
            errors.append('invalid_relation_or_missing_reason:' + str(key))
            continue
        source_event = f.get('source_event')
        reference_event = f.get('reference_event')
        source_ids = {e['id'] for e in source_map[key[0]]['events'] if e['evidence_status'] == 'bound_usage'}
        reference_ids = {e['id'] for e in reference_map[key[1]]['events'] if e['evidence_status'] == 'bound_usage'}
        evidence = source_event in source_ids and reference_event in reference_ids
        effective = relation if relation != 'event_overlap' or evidence else 'uncertain'
        candidate = {**f, 'id': 'A' + str(len(rows)+1), 'relation': effective,
                     'model_relation': relation, 'evidence_status': 'bound_event_refs' if evidence else 'summary_only',
                     'status': 'model_reviewed', 'allowed_uses': ['style_reference'] if effective in {'event_overlap', 'related_context'} else [],
                     'parallel_example': False}
        previous = next((r for r in rows if (r['source_node'], r['reference_node']) == key), None)
        if previous is None:
            candidate['additional_event_matches'] = []
            rows.append(candidate)
        else:
            warnings.append('multiple_event_records_for_one_pair:' + str(key))
            previous['additional_event_matches'].append(candidate)
            if previous['relation'] != effective:
                previous.update(relation='uncertain', allowed_uses=[])
                warnings.append('conflicting_pair_decisions:' + str(key))
            for field in ('reason', 'differences'):
                if f.get(field) and f[field] not in previous.get(field, ''):
                    previous[field] = previous.get(field, '') + '\n' + f[field]
    if seen != expected:
        errors.append('missing_block_pairs:' + str(len(expected-seen)))
    return {'associations': rows, 'errors': errors, 'warnings': warnings,
            'quarantined_records': quarantine, 'complete': not errors,
            'syntax_notes': parsed.get('notes', []) + header_notes}


def parse_entity_matches(text, source_entities, reference_entities, reference_rows=None):
    parsed = flat(text, {'ENTITY_MATCH', 'SUMMARY'})
    errors = [e for e in parsed['errors'] if e != 'duplicate_record_id']
    rows, unmatched, warnings = [], [], []
    sources = {e['id']: e for e in source_entities}
    references = {e['id']: e for e in reference_entities}
    seen = set()
    for record in parsed['records']:
        if record['record'] != 'ENTITY_MATCH':
            continue
        f = record['fields']
        s, r = sources.get(f.get('source_entity')), references.get(f.get('reference_entity'))
        if s and r is None and f.get('reference_entity', '').strip().lower() in {'未匹配', '无', 'none', 'null', 'unmatched', '未找到'} and f.get('status') in {'rejected', 'ambiguous'} and f.get('reason'):
            unmatched.append({**f, 'source_form': s['form']})
            continue
        if not s or not r or (s['id'], r['id']) in seen:
            errors.append('unknown_or_duplicate_entity_pair')
            continue
        seen.add((s['id'], r['id']))
        if f.get('status') not in {'supported', 'ambiguous', 'rejected'} or not f.get('reason'):
            errors.append('invalid_entity_match')
            continue
        supported = s['evidence_status'] == r['evidence_status'] == 'bound_usage'
        rendering = f.get('reference_rendering', r['form'])
        rendering_ref = f.get('rendering_evidence_ref', r['evidence_ref'])
        if f.get('reference_rendering'):
            actual = (reference_rows or {}).get(rendering_ref)
            usage = actual['text'] if actual else r.get('usage', '') if rendering_ref == r['evidence_ref'] else ''
            if not rendering or rendering not in usage:
                errors.append('rendering_not_in_reference_usage:' + record['id'])
                continue
        rows.append({**f, 'source_form': s['form'], 'target_form': rendering,
                     'source_description': s['description'], 'reference_description': r['description'],
                     'source_usage_ref': s['evidence_ref'], 'reference_usage_ref': rendering_ref,
                     'status': f['status'] if supported else 'ambiguous',
                     'evidence_status': 'bound_usage' if supported else 'missing_reference'})
    summaries = [r for r in parsed['records'] if r['record'] == 'SUMMARY']
    if len(summaries) != 1 or summaries[0]['fields'].get('reviewed_all') != 'yes':
        errors.append('entity_review_summary_required')
    # Conflicting proposed names remain visible, rather than picking the last row.
    conflicts = {row['source_entity'] for row in rows if row['status'] == 'supported' and
                 any(other['source_entity'] == row['source_entity'] and other['target_form'] != row['target_form']
                     and other['status'] == 'supported' for other in rows)}
    for row in rows:
        if row['status'] == 'supported' and row['source_entity'] in conflicts:
            row['status'] = 'ambiguous'
    return {'mappings': rows, 'unmatched_entities': unmatched, 'warnings': warnings,
            'errors': errors, 'complete': not errors}
