"""Local, provenance-preserving dictionary reads. Never downloads on lookup."""
import json
from pathlib import Path
import sqlite3

from ..storage import file_digest


class Dictionary:
    def __init__(self, *, wikdict=None, numerals=None, small=None):
        self.paths = {k: Path(v) for k, v in
                      {'wikdict': wikdict, 'numerals': numerals, 'small': small}.items() if v}
        self.identity = {k: {'file': p.name, 'sha256': file_digest(p)} for k, p in self.paths.items()}
        self.entries = {}
        if 'numerals' in self.paths:
            for line in self.paths['numerals'].read_text(encoding='utf-8').splitlines():
                row = json.loads(line)
                if row.get('pos') == 'num':
                    self.entries.setdefault(row['word'], []).append({
                        'source': 'kaikki-en-ja-num', 'entry_id': row.get('word'),
                        'headword': row['word'], 'pos': 'num', 'language': 'en',
                        'readings': row.get('forms', []),
                        'senses': [s.get('glosses', []) for s in row.get('senses', [])],
                        'numeric_value': None, 'status': 'lexical_candidate',
                        'license': 'Wiktionary source licenses; preserve upstream attribution'})
        if 'small' in self.paths:
            self.small = json.loads(self.paths['small'].read_text(encoding='utf-8'))
        else:
            self.small = {}

    def lookup(self, term, *, reading=None, limit=5):
        if not isinstance(term, str) or not term.strip() or len(term) > 128 or type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('nonempty dictionary term up to 128 characters and limit 1..10 required')
        entries = list(self.entries.get(term, []))
        if 'wikdict' in self.paths:
            with sqlite3.connect(self.paths['wikdict'].resolve().as_uri()+'?mode=ro', uri=True) as db:
                rows = db.execute('SELECT lexentry,sense,written_rep,trans_list,score,is_good '
                                  'FROM translation WHERE written_rep=? ORDER BY score DESC LIMIT ?',
                                  (term, limit)).fetchall()
            entries += [{'source': 'wikdict-ja-zh', 'entry_id': r[0], 'headword': r[2],
                         'senses': r[1], 'target_forms': r[3], 'score': r[4], 'is_good': r[5],
                         'license': 'CC BY-SA (WikDict data)', 'status': 'lexical_candidate'} for r in rows]
        if term in self.small:
            entries.append({'source': 'Japanese-Chinese-thesaurus', 'entry_id': term,
                            'headword': term, 'raw_definition': self.small[term],
                            'license': 'Unlicense (repository declaration)', 'status': 'lexical_candidate'})
        # A reading is a hint, never an unsupported sense disambiguation.
        return {'status': 'found' if entries else 'not_found', 'term': term, 'reading_hint': reading,
                'entries': entries[:limit], 'source_versions': self.identity, 'generation_calls': 0,
                'usage': 'Dictionary senses are alternatives, not approved work-specific translations.'}
