"""Local, evidence-bound ordering of a caller-selected reference file set."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import posixpath
import re
import unicodedata
from pathlib import Path
import zipfile
from xml.etree import ElementTree as ET

from ..storage import digest, file_digest
from .schemas import InputSpec


def _number(text):
    text = unicodedata.normalize('NFKC', text).strip()
    if text.isdecimal():
        return int(text)
    digits = dict(zip('零〇一二三四五六七八九两', [0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 2]))
    if text and all(c in digits for c in text):
        return int(''.join(str(digits[c]) for c in text))
    value = current = 0
    for char in text:
        if char in digits:
            current = digits[char]
        elif char in '十百千':
            value += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            current = 0
        else:
            return None
    return value + current if text else None


def _label(text):
    text = unicodedata.normalize('NFKC', text).strip()
    extra = bool(re.search(r'(?:\bEx\b|外传|外傳|短篇集|特别篇|特別篇)', text, re.I))
    match = re.search(r'第?\s*([0-9零〇一二三四五六七八九十百千两]+)\s*[卷巻册冊]', text)
    if not match:
        match = re.search(r'\s([0-9]{1,3})\s*(?:[上下中]|前篇|后篇|後篇)?$', text)
    volume = _number(match[1]) if match else None
    part = next((i for suffix, i in [('上', 1), ('中', 2), ('下', 3), ('前篇', 1), ('后篇', 2), ('後篇', 2)]
                 if text.endswith(suffix)), None)
    return volume, part, 'extra' if extra else 'mainline' if volume is not None else 'unknown'


def _package_labels(path):
    if path.suffix.lower() != '.epub':
        return []
    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read('META-INF/container.xml'))
        package_path = next(n.attrib['full-path'] for n in container.iter()
                            if n.tag.split('}')[-1] == 'rootfile')
        package = ET.fromstring(archive.read(package_path))
        labels = [('package_title', n.text or '') for n in package.iter() if n.tag.split('}')[-1] == 'title']
        items = [n.attrib for n in package.iter() if n.tag.split('}')[-1] == 'item']
        ncx = next((n for n in items if n.get('media-type') == 'application/x-dtbncx+xml'), None)
        if ncx:
            target = posixpath.normpath(posixpath.join(posixpath.dirname(package_path), ncx['href']))
            root = ET.fromstring(archive.read(target))
            # Only the opening navigation labels can identify the book/volume.
            labels += [('opening_navigation', ''.join(n.itertext()).strip()) for n in root.iter()
                       if n.tag.split('}')[-1] == 'navLabel'][:3]
        return labels


def _series_key(text):
    text = unicodedata.normalize('NFKC', text).strip()
    text = re.sub(r'(?:第?\s*[0-9零〇一二三四五六七八九十百千两]+\s*[卷巻册冊]|\s[0-9]{1,3})\s*(?:[上下中]|前篇|后篇|後篇)?$', '', text)
    return ''.join(c for c in text.casefold() if c.isalnum())


@dataclass(frozen=True)
class ReferenceSetPlan:
    """Serializable ordering decision. Unknown/conflicting order cannot import."""
    data: dict

    def to_dict(self):
        return deepcopy(self.data)

    @property
    def fingerprint(self):
        return self.data['fingerprint']

    def to_input_spec(self):
        if self.data['ordering_status'] != 'resolved':
            raise ValueError('reference order needs resolution; provide explicit overrides')
        return InputSpec.from_dict(self.data['input_spec'])


def plan_reference_set(paths, *, edition_id, language='zh', order='auto', overrides=None, series_label=None):
    """Inspect only supplied EPUB/TXT files; never add other directory members.

    overrides keys are resolved paths or unique filenames. Values may contain
    volume_ordinal, part_ordinal, volume_kind and reading_order. A completely
    explicit reading_order supports standalone chapter files with opaque names.
    """
    if order not in {'auto', 'provided'}:
        raise ValueError('order must be auto or provided')
    if series_label is not None and (not isinstance(series_label, str) or not series_label.strip()):
        raise ValueError('series_label must be nonempty when supplied')
    if isinstance(paths, (str, Path)):
        paths = [paths]
    paths = [Path(p).resolve() for p in paths]
    if not paths:
        raise ValueError('reference files must be nonempty')
    if order == 'auto':
        # Duplicate selection and evidence must not depend on enumeration order.
        paths.sort(key=str)
    overrides = overrides or {}
    names = [p.name for p in paths]
    if any(names.count(key) > 1 for key in overrides if key in names):
        raise ValueError('override filename is ambiguous; use resolved paths')
    issues, rows, seen = [], [], {}
    for given_order, path in enumerate(paths):
        content_hash = file_digest(path)
        if content_hash in seen:
            seen[content_hash]['duplicate_paths'].append(str(path))
            continue
        candidates = [(kind, text, *_label(text)) for kind, text in
                      [('filename', path.stem)] + _package_labels(path)]
        votes = {v for _, _, v, _, kind in candidates if v is not None and kind != 'extra'}
        override = overrides.get(str(path), overrides.get(path.name, {}))
        unknown = set(override) - {'volume_ordinal', 'part_ordinal', 'volume_kind', 'reading_order'}
        if unknown:
            raise ValueError('unknown reference override fields: ' + ','.join(sorted(unknown)))
        for key in ('volume_ordinal', 'part_ordinal', 'reading_order'):
            if key in override and (type(override[key]) is not int or override[key] < 0):
                raise ValueError(key + ' must be a nonnegative integer')
        volume = override.get('volume_ordinal', next(iter(votes)) if len(votes) == 1 else None)
        kinds = {c[4] for c in candidates}
        kind = override.get('volume_kind', 'extra' if 'extra' in kinds else 'mainline' if volume is not None else 'unknown')
        if kind not in {'mainline', 'extra', 'unknown'}:
            raise ValueError('invalid volume_kind')
        parts = {c[3] for c in candidates if c[3] is not None}
        part = override.get('part_ordinal', next(iter(parts)) if len(parts) == 1 else None)
        conflicts = []
        if len(votes) > 1 and 'volume_ordinal' not in override:
            conflicts.append('conflicting_volume_numbers')
        if len(parts) > 1 and 'part_ordinal' not in override:
            conflicts.append('conflicting_parts')
        row = {'source_id': 'ref-' + content_hash[:24], 'path': str(path),
               'series_key': _series_key(series_label or next((c[1] for c in candidates if c[0] == 'package_title' and c[1].strip()), path.stem)),
               'content_fingerprint': content_hash, 'volume_ordinal': volume, 'part_ordinal': part,
               'volume_kind': kind, 'evidence': [{'kind': c[0], 'text': c[1]} for c in candidates],
               'overrides': deepcopy(override), 'conflicts': conflicts, 'duplicate_paths': [],
               'explicit_order': override.get('reading_order', given_order if order == 'provided' else None)}
        seen[content_hash] = row
        rows.append(row)
    # Deterministically choose the representative for exact duplicate bytes.
    for row in rows:
        all_paths = sorted(set([row['path']] + row['duplicate_paths']))
        row['path'], row['duplicate_paths'] = all_paths[0], all_paths[1:]
    usable = [r for r in rows if r['volume_kind'] != 'extra']
    if len({r['series_key'] for r in usable}) > 1:
        issues.append('mixed_or_unresolved_series')
    extras = sorted((r for r in rows if r['volume_kind'] == 'extra'), key=lambda r: r['path'])
    explicit = bool(usable) and all(r['explicit_order'] is not None for r in usable)
    if explicit:
        usable.sort(key=lambda r: (r['explicit_order'], r['source_id']))
        if len({r['explicit_order'] for r in usable}) != len(usable):
            issues.append('duplicate_explicit_order')
    else:
        usable.sort(key=lambda r: (r['volume_ordinal'] is None, r['volume_ordinal'] or 0,
                                   r['part_ordinal'] or 0, r['source_id']))
        if any(r['volume_ordinal'] is None for r in usable):
            issues.append('unknown_volume_order')
        keys = [(r['volume_ordinal'], r['part_ordinal']) for r in usable]
        if len(keys) != len(set(keys)):
            issues.append('duplicate_volume_or_part')
        for volume in {r['volume_ordinal'] for r in usable}:
            group = [r for r in usable if r['volume_ordinal'] == volume]
            if len(group) > 1 and any(r['part_ordinal'] is None for r in group):
                issues.append('unmarked_part_or_duplicate_volume')
    if not usable:
        issues.append('no_mainline_reference')
    if any(r['conflicts'] for r in usable):
        issues.append('conflicting_order_evidence')
    volumes = sorted({r['volume_ordinal'] for r in usable if r['volume_ordinal'] is not None})
    gaps = sorted(set(range(volumes[0], volumes[-1]+1)) - set(volumes)) if volumes else []
    availability = {'scope': 'unknown' if not volumes else 'gapped' if gaps else 'prefix' if volumes[0] == 1 else 'window',
                    'available_volumes': volumes, 'unavailable_prefix': list(range(1, volumes[0])) if volumes else [],
                    'internal_gaps': gaps, 'end_integrity': 'unknown'}
    for i, row in enumerate(usable):
        row['input_order'] = i
    spec = {'edition_id': edition_id, 'edition_label': edition_id, 'language': language,
            'version_relation': 'different_edition', 'sources': [
                {'source_id': r['source_id'], 'path': r['path'], 'input_order': r['input_order'],
                 'volume_hint': f"第{r['volume_ordinal']}卷" if r['volume_ordinal'] is not None else r['source_id']}
                for r in usable]}
    InputSpec.from_dict(spec) if usable else None
    data = {'schema': 'reference-set-v1', 'policy_version': 'reference-order-v1',
            'edition_id': edition_id, 'language': language, 'ordering_status': 'needs_resolution' if issues else 'resolved',
            'series_label': series_label, 'version_consistency': 'unverified',
            'ordered_sources': usable, 'excluded_or_unplaced': extras, 'availability': availability,
            'endpoint_candidate': usable[-1]['source_id'] if usable and not issues else None,
            'input_spec': spec, 'issues': issues}
    data['fingerprint'] = digest(data)
    return ReferenceSetPlan(data)
