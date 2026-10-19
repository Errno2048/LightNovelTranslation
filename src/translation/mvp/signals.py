"""Rule signals require grounded model dispositions; no automatic approval."""
from ..review.protocol import locate

SIGNAL_TASK = '''
【P6规则信号闭环 v1】
对每个已列signal另输出一条记录（审校和裁决均须独立检查）：
@@SIGNAL s1
signal: 原样复制signal的id
decision: confirmed|dismissed|unresolved
reason: 明确解释原文与译文中的数值、单位、对象和限定词，或说明规则为何误报
source_ref: 实际S标签
target_ref: 实际T标签
finding: 如confirmed须关联本次已输出或已给定的finding ID；其他情况省略
confirmed表示具体错译，须有可定位FINDING；等价写法/规则误报用dismissed；证据不足unresolved。
不要仅因为数字集合相同而忽略对象互换。一个、十分、一点等低置信项不能机械确认。
全部记录结束后统一@@END。不能漏掉任何signal。
'''


def resolutions(version,parsed,signals,findings):
    rows,errors = [],[]
    by_id = {s['id']:s for s in signals}
    grounded = {f['id'] for f in findings if f['status']=='grounded'}
    for record in parsed['records']:
        if record['record']!='SIGNAL': continue
        f = record['fields']
        source = locate(f.get('source_ref'),version['source_segments'],require_quote=False)
        target = locate(f.get('target_ref'),version['segments'],require_quote=False)
        signal=by_id.get(f.get('signal'),{})
        in_group=(not signal.get('source_refs') or source and source['alias'] in signal['source_refs']) and (
            not signal.get('target_refs') or target and target['alias'] in signal['target_refs'])
        if isinstance(signal.get('target'),str):
            in_group=in_group and target and target['alias']==signal['target']
        valid = (f.get('signal') in by_id and f.get('decision') in {'confirmed','dismissed','unresolved'} and
                 f.get('reason') and source and target and in_group and (f['decision']!='confirmed' or f.get('finding') in grounded))
        if not valid: errors.append('ungrounded_signal_resolution:'+str(f.get('signal')))
        rows.append({'id':f.get('signal'),**f,'grounded':bool(valid)})
    if len(rows)!=len(by_id) or {r['id'] for r in rows}!=set(by_id) or len({r['id'] for r in rows})!=len(rows):
        errors.append('signal_resolution_incomplete_or_duplicate')
    return rows,errors
