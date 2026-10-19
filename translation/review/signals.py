"""Generic draft residue candidates, with every occurrence retained."""
import re

_PLACEHOLDER = re.compile(r'(?<![A-Za-z0-9_])_(?:[a-zA-Z][a-zA-Z0-9]*_)+[a-zA-Z0-9]+')


def residue_signals(version):
    return [{'id': f"residue:{row['alias']}:{match.start()}", 'kind': 'placeholder_candidate',
             'target': row['alias'], 'token': match.group(), 'start': match.start(),
             'end': match.end(), 'text': row['text'], 'status': 'requires_source_check'}
            for row in version['segments'] for match in _PLACEHOLDER.finditer(row['text'])]
