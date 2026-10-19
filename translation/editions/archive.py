"""Read native documents without inventing a canonical bilingual binding."""
from collections import defaultdict
from copy import deepcopy
from pathlib import Path

from ..storage import digest, read_json, read_jsonl


def _validate_import_export(folder, manifest, entries, paragraphs):
    """Validate only the new generic export contract; legacy views are unchanged."""
    if manifest.get('status') != 'complete':
        raise ValueError('generic edition export is incomplete')
    if (len(entries) != manifest.get('entry_count') or
            len(paragraphs) != manifest.get('paragraph_count')):
        raise ValueError('generic edition export count mismatch')
    if digest({'entries': entries, 'paragraphs': paragraphs}) != manifest.get('view_fingerprint'):
        raise ValueError('generic edition export fingerprint mismatch')
    by_entry = defaultdict(list)
    for row in paragraphs:
        by_entry[row['entry_id']].append(row)
    for entry in entries:
        rows = sorted(by_entry[entry['entry_id']], key=lambda row: row['ordinal'])
        if entry.get('paragraph_ids') != [row['paragraph_id'] for row in rows]:
            raise ValueError('generic edition export paragraph list mismatch')
        if [row['ordinal'] for row in rows] != list(range(len(rows))):
            raise ValueError('generic edition export paragraph order mismatch')
        if entry['edition_id'] != manifest.get('edition_id'):
            raise ValueError('generic edition export edition mismatch')
    if [entry['edition_order'] for entry in entries] != list(range(len(entries))):
        raise ValueError('generic edition export reading order mismatch')
    for name, key in (('provenance', 'provenance_fingerprint'), ('issues', 'issues_fingerprint')):
        target = folder / (name + '.jsonl')
        if not target.is_file() or digest(read_jsonl(target)) != manifest.get(key):
            raise ValueError('generic edition export ' + name + ' mismatch')


def classify_reference_mode(manifest, existing_mapping=(), probes=(), requested='auto'):
    if requested not in {'auto', 'parallel', 'cross_edition', 'source_only'}:
        raise ValueError('unknown reference mode')
    declared = manifest.get('version_relation')
    references = manifest.get('reference_editions', [])
    if requested != 'auto':
        mode, reason = requested, 'explicit_configuration'
    elif declared == 'different_edition':
        mode, reason = 'cross_edition', 'declared_different_edition'
    elif not references:
        mode, reason = 'source_only', 'no_reference_edition'
    elif declared == 'same_source' and existing_mapping:
        mode, reason = 'parallel', 'declared_same_source_with_mapping'
    else:
        mode, reason = 'cross_edition', 'unverified_reference_relationship'
    return {'reference_mode': mode, 'reason': reason, 'declaration': declared,
            'probe_count': len(probes), 'semantic_equivalence_confirmed': False}


class EditionArchive:
    def __init__(self, entries, paragraphs):
        self.entries = {}
        self.paragraphs = {}
        self.by_entry = defaultdict(list)
        for entry in entries:
            key = entry['entry_id']
            if key in self.entries:
                raise ValueError('duplicate edition entry ID')
            self.entries[key] = deepcopy(entry)
        for row in paragraphs:
            eid, pid = row['entry_id'], row['paragraph_id']
            if eid not in self.entries or pid in self.paragraphs:
                raise ValueError('unknown entry or duplicate paragraph ID')
            entry = self.entries[eid]
            if row.get('edition_id') != entry['edition_id'] or row.get('revision_id') != entry['revision_id']:
                raise ValueError('paragraph edition/revision mismatch')
            self.paragraphs[pid] = deepcopy(row)
            self.by_entry[eid].append(self.paragraphs[pid])
        for rows in self.by_entry.values():
            rows.sort(key=lambda r: r['ordinal'])
            if len({r['ordinal'] for r in rows}) != len(rows):
                raise ValueError('duplicate paragraph ordinal')
        self.fingerprint = digest({'entries': self.entries, 'paragraphs': self.paragraphs})

    @classmethod
    def from_folder(cls, folder):
        folder = Path(folder)
        entries = read_jsonl(folder / 'entries.jsonl')
        paragraphs = read_jsonl(folder / 'paragraphs.jsonl')
        manifest_path = folder / 'manifest.json'
        manifest = read_json(manifest_path) if manifest_path.is_file() else {}
        if manifest.get('schema') == 'editions-export-v1':
            _validate_import_export(folder, manifest, entries, paragraphs)
        elif any(entry.get('metadata', {}).get('import_revision') for entry in entries):
            raise ValueError('generic edition export has no valid completion manifest')
        return cls(entries, paragraphs)

    def documents(self, edition_id, *, narrative_only=True):
        entries = [e for e in self.entries.values() if e['edition_id'] == edition_id]
        if narrative_only:
            entries = [e for e in entries if e['kind'].startswith('narrative') and not e.get('metadata', {}).get('extra')]
        return [self.read(e['entry_id']) for e in sorted(entries, key=lambda e: (e['edition_order'], e['entry_id']))
                if self.by_entry[e['entry_id']]]

    def read(self, entry_id):
        entry = self.entries[entry_id]
        prefix = ('J' if entry['language'] == 'ja' else 'Z') + digest(entry_id)[:12]
        rows = [{'ref': prefix + 'P' + str(r['ordinal']).zfill(4), 'paragraph_id': r['paragraph_id'],
                 'entry_id': entry_id, 'edition_id': entry['edition_id'], 'revision_id': entry['revision_id'],
                 'ordinal': r['ordinal'], 'text': r['raw_text']}
                for r in self.by_entry[entry_id] if r['kind'] == 'body' and r['raw_text'].strip()]
        return {**entry, 'rows': rows, 'text': '\n\n'.join(r['text'] for r in rows),
                'text_hash': digest(rows)}

    def refs(self, entry_ids):
        return {r['ref']: r for eid in entry_ids for r in self.read(eid)['rows']}

    def validate_dependencies(self, dependencies):
        for dep in dependencies:
            doc = self.read(dep['entry_id'])
            if any(doc[key] != dep[key] for key in ('edition_id', 'revision_id', 'text_hash')):
                raise ValueError('stale edition document dependency')


def document_dependency(doc):
    return {k: doc[k] for k in ('entry_id', 'edition_id', 'revision_id', 'text_hash')}


def parallel_documents(pairs):
    """An explicit adapter; external editions never pass through this binding."""
    entries, paragraphs, bindings = [], [], []
    for pair in pairs:
        source = pair['source']
        translations = pair.get('translation', [])
        if isinstance(translations, dict):
            translations = [translations]
        for doc in [source] + translations:
            eid = doc['chapter_id']
            edition = 'parallel-' + doc['language']
            entry = {'entry_id': eid, 'edition_id': edition, 'revision_id': doc['revision_id'],
                     'document_id': edition, 'edition_order': doc['order'], 'native_order': doc['order'],
                     'raw_title': doc['title'], 'language': doc['language'], 'kind': 'narrative'}
            if eid not in {e['entry_id'] for e in entries}:
                entries.append(entry)
                for i, row in enumerate(doc['paragraphs']):
                    if row.get('kind', 'body') != 'body':
                        continue
                    paragraphs.append({'entry_id': eid, 'edition_id': edition,
                        'revision_id': doc['revision_id'], 'paragraph_id': row.get('paragraph_id', eid+':p'+str(i)),
                        'ordinal': row.get('ordinal', i), 'kind': 'body', 'raw_text': row['text']})
        bindings.append({'source_id': source['chapter_id'], 'translation_ids': [d['chapter_id'] for d in translations],
                         'semantic_status': pair.get('semantic_status', 'unreviewed')})
    return EditionArchive(entries, paragraphs), bindings
