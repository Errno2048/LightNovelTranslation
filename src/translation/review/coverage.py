"""Many-to-many paragraph coverage, independent of semantic accuracy."""
from ..narrative.schemas import NarrativeError
from .protocol import parse_records, ids
from .requests import text_messages
from .versions import read_version
from .repair import decode_records, read_records
from ..references import STABLE
from ..storage import digest

PROFILES = {'plain', 'coarse'}
RECOVERY_POLICY = {'version': 'local-monotone-gap', 'max_gap_paragraphs': 2,
                   'max_missing_per_side': 4, 'max_group_paragraphs_per_side': 8,
                   'max_group_characters': 6000}


def recover_local_gaps(version, original):
    """Coarsen only small monotone gaps, without asserting semantic alignment."""
    result = {**original, 'original_groups': original['groups'],
              'original_unmapped_source': original['unmapped_source'],
              'original_unmapped_target': original['unmapped_target'],
              'original_errors': original['errors'], 'recoveries': [],
              'recovery_policy': dict(RECOVERY_POLICY), 'mapping_quality': 'exact'}
    if not original['errors']:
        return result
    result['mapping_quality'] = 'incomplete'
    if original['errors'] != ['unmapped_content'] or not original['groups']:
        result['recovery_not_applied'] = 'not_a_pure_monotone_gap'
        return result
    groups = original['groups']
    parent = list(range(len(groups)))

    def root(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for rows_key, group_key, missing_key in [
        ('source_segments', 'source_paragraph_ids', 'unmapped_source'),
        ('segments', 'target_paragraph_ids', 'unmapped_target')]:
        expected = [r['id'] for r in version[rows_key]]
        missing = set(original[missing_key])
        if len(missing) > RECOVERY_POLICY['max_missing_per_side']:
            result['recovery_not_applied'] = 'too_many_missing_paragraphs'
            return result
        owners = {pid: i for i, g in enumerate(groups) for pid in g[group_key]}
        start = 0
        while start < len(expected):
            if expected[start] not in missing:
                start += 1
                continue
            end = start + 1
            while end < len(expected) and expected[end] in missing:
                end += 1
            if end - start > RECOVERY_POLICY['max_gap_paragraphs']:
                result['recovery_not_applied'] = 'gap_too_long'
                return result
            left = owners.get(expected[start-1]) if start else None
            right = owners.get(expected[end]) if end < len(expected) else None
            anchors = [i for i in (left, right) if i is not None]
            if not anchors:
                result['recovery_not_applied'] = 'no_mapped_anchor'
                return result
            # Use both adjacent anchors; do not guess which side owns the gap.
            lo, hi = min(anchors), max(anchors)
            for i in range(lo, hi+1):
                parent[root(i)] = root(lo)
            start = end
    components = {}
    for i in range(len(groups)):
        components.setdefault(root(i), []).append(i)
    recovered = []
    for indices in components.values():
        group = {'status': 'mapped'}
        added = {}
        for rows_key, key in [('source_segments', 'source_paragraph_ids'),
                              ('segments', 'target_paragraph_ids')]:
            rows = version[rows_key]
            old = {p for i in indices for p in groups[i][key]}
            positions = [j for j, r in enumerate(rows) if r['id'] in old]
            lo, hi = min(positions), max(positions)
            # Chapter-edge gaps have only one anchor.
            if indices[0] == 0:
                lo = 0
            if indices[-1] == len(groups)-1:
                hi = len(rows)-1
            group[key] = [r['id'] for r in rows[lo:hi+1]]
            added[key] = [p for p in group[key] if p not in old]
        changed = any(added.values()) or len(indices) > 1
        if changed:
            text_length = sum(len(r['text']) for key, field in [
                ('source_segments', 'source_paragraph_ids'), ('segments', 'target_paragraph_ids')]
                for r in version[key] if r['id'] in group[field])
            if (any(len(group[k]) > RECOVERY_POLICY['max_group_paragraphs_per_side'] for k in added)
                    or text_length > RECOVERY_POLICY['max_group_characters']):
                result['recovery_not_applied'] = 'coarse_group_too_large'
                result['recoveries'] = []
                return result
            recovery = {'method': 'merge_adjacent_anchors', 'original_group_indices': indices,
                        'source_paragraph_ids': group['source_paragraph_ids'],
                        'target_paragraph_ids': group['target_paragraph_ids'],
                        'missing_source_paragraph_ids': added['source_paragraph_ids'],
                        'missing_target_paragraph_ids': added['target_paragraph_ids'],
                        'semantic_status': 'requires_review'}
            recovery['id'] = 'mapping-gap:' + digest(recovery)[:20]
            group.update(status='coarse', recovery_id=recovery['id'])
            result['recoveries'].append(recovery)
        recovered.append(group)
    # Recovered groups must still be a partition, not just contain every ID.
    for rows_key, key in [('source_segments', 'source_paragraph_ids'), ('segments', 'target_paragraph_ids')]:
        if [p for g in recovered for p in g[key]] != [r['id'] for r in version[rows_key]]:
            result.update(recoveries=[], recovery_not_applied='recovery_is_not_a_partition')
            return result
    result.update(groups=recovered, unmapped_source=[], unmapped_target=[], errors=[],
                  mapping_complete=True, mapping_quality='coarse')
    return result


def validate_mapping(version, parsed, *, profile="legacy"):
    if profile not in PROFILES:
        raise ValueError("unknown mapping profile")
    source = {s["alias"]: s["id"] for s in version["source_segments"]}
    target = {s["alias"]: s["id"] for s in version["segments"]}
    seen_s, seen_t, groups, errors = [], [], [], list(parsed["errors"])
    normalizations = []
    for r in parsed["records"]:
        f = r["fields"]
        ss, tt = ids(f.get("source", "")), ids(f.get("target", ""))
        status = f.get("status")
        # An observed response translated this enum. The opt-in codec only
        # recognizes this exact synonym; paragraph IDs and content stay intact.
        if profile in {"coarse"} and status == "已映射":
            normalizations.append({"record_id": r["id"], "field": "status",
                                   "original": status, "canonical": "mapped"})
            status = "mapped"
        if not ss or not tt or set(ss)-source.keys() or set(tt)-target.keys() or status != "mapped":
            errors.append("invalid_mapping:"+r["id"])
            continue
        seen_s.extend(ss); seen_t.extend(tt)
        groups.append({"source_paragraph_ids": [source[x] for x in ss],
                       "target_paragraph_ids": [target[x] for x in tt], "status": "mapped"})
    for seen, expected, label in [(seen_s, list(source), "source"), (seen_t, list(target), "target")]:
        if len(seen) != len(set(seen)):
            errors.append("duplicate_"+label)
        if seen != [x for x in expected if x in seen]:
            errors.append("reordered_"+label)
    unmapped_s, unmapped_t = [source[x] for x in source if x not in seen_s], [target[x] for x in target if x not in seen_t]
    if unmapped_s or unmapped_t:
        errors.append("unmapped_content")
    result = {"groups": groups, "unmapped_source": unmapped_s, "unmapped_target": unmapped_t,
            "layout_exempt": version["layout_exempt"], "errors": errors,
            "mapping_complete": bool(groups) and not errors, "semantic_coverage_reviewed": False}
    if profile in {"coarse"}:
        result["normalizations"] = normalizations
    if profile == 'coarse':
        result = recover_local_gaps(version, result)
    return result


def mapping_diagnostics(version, parsed):
    """Explain invalid references without guessing semantic correspondence."""
    result = {'legal_ranges': {}, 'invalid_records': []}
    rows = {'source': version['source_segments'], 'target': version['segments']}
    for side, segments in rows.items():
        aliases = [r['alias'] for r in segments]
        result['legal_ranges'][side] = {'first': aliases[0] if aliases else None,
            'last': aliases[-1] if aliases else None, 'count': len(aliases)}
    for record in parsed['records']:
        detail = {'record_id': record['id'], 'fields': record['fields'], 'invalid_ids': {}, 'nearby': {}}
        for side, segments in rows.items():
            known = {r['alias']: i for i, r in enumerate(segments)}
            values = ids(record['fields'].get(side, ''))
            invalid = [value for value in values if value not in known]
            if not invalid:
                continue
            detail['invalid_ids'][side] = invalid
            # Include the actual end of the text for fabricated tail labels,
            # and the neighbors of any legal anchor in the same group.
            positions = {i for value in values if value in known
                         for i in range(max(0, known[value]-1), min(len(segments), known[value]+2))}
            positions.update(range(max(0, len(segments)-3), len(segments)))
            detail['nearby'][side] = [{'id':segments[i]['alias'], 'text':segments[i]['text']}
                                     for i in sorted(positions)][:8]
        if detail['invalid_ids']:
            result['invalid_records'].append(detail)
        if len(result['invalid_records']) >= 5:
            break
    return result


def _mapping_feedback_policy(session, version_id, profile):
    store = session.view.store
    saved = [v for _, v in store.objects('mapping-feedback-policy')
             if v['version_id'] == version_id and v['profile'] == profile]
    if saved:
        return saved[0]['diagnostics']
    # Historical failed conversations must reconstruct their exact old cache
    # identity when resumed. Only entirely new tasks adopt detailed feedback.
    prior = any(v['version_id'] == version_id and v['kind'] == 'mapping'
                for _, v in store.objects('review-request'))
    policy = 'legacy' if prior else 'detailed'
    store.put('mapping-feedback-policy', {'version_id':version_id, 'profile':profile, 'diagnostics':policy})
    return policy


def map_coverage(session, version_id, *, profile="legacy"):
    bound_version = read_version(session.view, version_id)
    if bound_version.get('output_binding'):
        return session.view.store.put('translation-coverage', declared_coverage(bound_version, version_id))
    if profile not in PROFILES:
        raise ValueError("unknown mapping profile")
    v = read_version(session.view, version_id)
    source_example = ','.join(r['alias'] for r in v['source_segments'][:2])
    target_example = ','.join(r['alias'] for r in v['segments'][:2])
    task = ("请将全部有意义的日文段落与译文段落按含义建立对应。允许相邻多对一、一对多；不能按数量猜配。"
        "漏译或无法对应时保留未映射，不造标签。每组只输出：\n@@MAP m1\nsource: S001,S002\ntarget: T001\nstatus: mapped"
        "\n逐组继续，最后单独一行@@END。仅使用实际提供的S/T标签，不输出译文或解释。")
    if v.get('reference_profile') == STABLE:
        task = ("按含义建立本章日文和中文段落的对应。允许相邻多对多，逐个复制提供的完整段落ID，"
            "不按数量猜配，不改变ID。漏译或无法对应时保留未映射。以下只演示格式，不表示这些段落一定对应：\n"
            f"@@MAP m1\nsource: {source_example}\ntarget: {target_example}\nstatus: mapped\n"
            "逐组继续，最后单独一行@@END。不输出译文或解释。")
    messages = text_messages(v, task)
    syntax = 'tolerant-records' if profile == 'coarse' else 'plain'
    feedback_policy = _mapping_feedback_policy(session, version_id, profile) if profile == 'coarse' else 'plain'
    attempts = []
    max_attempts = min(3, getattr(session,'max_task_attempts',None) or 1) if profile=='coarse' else 1
    for attempt in range(max_attempts):
        raw = session.call(version_id, "mapping", messages,
            **({'task_family':'mapping:'+profile} if profile=='coarse' else {}))
        attempts.append(raw['id'])
        parsed, repair_id = decode_records(session, version_id, raw, {"MAP"}, syntax_profile=syntax)
        validated = validate_mapping(v, parsed, profile=profile)
        if validated['mapping_complete'] or attempt==max_attempts-1:
            break
        # A new diagnostic prompt shares the original logical task/physical cap.
        # Ask for a full replacement mapping; never fabricate correspondence.
        source_alias = {r['id']:r['alias'] for r in v['source_segments']}
        target_alias = {r['id']:r['alias'] for r in v['segments']}
        extra = ('\n非法引用诊断（只能引用实际存在的ID；附近正文仅供重新按含义判断）：'+str(mapping_diagnostics(v, parsed))
                 if feedback_policy == 'detailed' else '')
        messages += [{'role':'assistant','content':raw['generation']['text']},
            {'role':'user','content':'上次映射未通过结构校验。请重新输出本章完整映射，不能只补缺口；'
             '仍须按含义判断，真实漏译或增译保留未对应，不得为凑全覆盖强配。'
             '\n校验错误：'+str(validated['errors'])+
             '\n未对应日文ID：'+','.join(source_alias[p] for p in validated['unmapped_source'])+
             '\n未对应中文ID：'+','.join(target_alias[p] for p in validated['unmapped_target'])+extra}]
    data = {"schema": "translation-coverage-v1", "version_id": version_id, "generation_id": raw["id"],
            "source_revision": v["source_revision"], "translation_hash": v["text_hash"], "repair_id": repair_id,
            **validated}
    if profile=='coarse':
        data['mapping_attempt_generation_ids'] = attempts
    if profile != "legacy":
        data["mapping_profile"] = profile
    return session.view.store.put("translation-coverage", data)


def read_coverage(view, version_id, coverage_id):
    v = read_version(view, version_id)
    c = view.store.get(coverage_id, "translation-coverage")
    if c["version_id"] != version_id or c["translation_hash"] != v["text_hash"] or c["source_revision"] != v["source_revision"]:
        raise NarrativeError("conflict", "coverage belongs to another text version")
    if c.get('mapping_profile') == 'source-records':
        if c != declared_coverage(v, version_id):
            raise NarrativeError('invalid_record', 'declared coverage differs from bound output')
        return c
    if c.get('mapping_profile')=='coarse':
        # Early coarse artifacts predate the optional retry lineage; their
        # sole immutable generation still has an unambiguous binding.
        attempts=c.get('mapping_attempt_generation_ids',[c['generation_id']])
        if not attempts or attempts[-1]!=c['generation_id'] or len(attempts)>3:
            raise NarrativeError('invalid_record','mapping attempt lineage differs')
        for ident in attempts:
            raw=view.store.get(ident,'review-generation')
            if raw['version_id']!=version_id or raw['kind']!='mapping':
                raise NarrativeError('invalid_record','mapping attempt belongs to another task')
    syntax = 'tolerant-records' if c.get('mapping_profile') == 'coarse' else 'plain'
    expected = validate_mapping(v, read_records(view, version_id, c["generation_id"], c.get("repair_id"), {"MAP"}, syntax_profile=syntax),
                                profile=c.get("mapping_profile", "legacy"))
    if any(c[k] != val for k, val in expected.items()):
        raise NarrativeError("invalid_record", "coverage differs from raw model output")
    return c


def declared_coverage(version, version_id):
    if not version.get('output_binding') or len(version['source_segments']) != len(version['segments']):
        raise NarrativeError('invalid_record', 'declared coverage requires a complete output binding')
    return {'schema': 'translation-coverage-v1', 'version_id': version_id,
        'generation_id': version['output_binding']['generation_id'], 'repair_id': None,
        'mapping_profile': 'source-records', 'mapping_quality': 'declared_by_translation_output',
        'source_revision': version['source_revision'], 'translation_hash': version['text_hash'],
        'groups': [{'source_paragraph_ids': [s['id']], 'target_paragraph_ids': [t['id']], 'status': 'mapped'}
                   for s, t in zip(version['source_segments'], version['segments'])],
        'unmapped_source': [], 'unmapped_target': [], 'layout_exempt': version['layout_exempt'],
        'mapping_complete': True, 'semantic_coverage_reviewed': False, 'errors': []}
