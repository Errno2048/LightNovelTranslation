"""Review instructions for the current chapter-review protocol."""

AUDIT_FIELDS = {'FINDING': {'basis'}}

AUDIT_TASK = """【审校协议 chapter-review】
逐段对照本章日文与草稿，寻找有证据的含义差异；不要改写全文。
先核对谁实施动作、谁感受/受益、动作对象、肯定/否定、条件分支和未知事项，再检查译名与形式残留。
补充信息、原因和对象只能来自日文及提供的允许前文，不能因中文流畅或历史译文也如此写就认定正确。
对于省略对象、授受表达和因果关系，回看同段及相邻段，避免仅凭单词直觉判断。正常意译无需修改。

知识权威分级：
- source_meaning：本章含义、内部指代或客观形式缺陷。
- observed_usage：有R/L原段支持的历史译法；只是本章保持一致的候选，不是已审定或全书固定规范。
- style_preference：正确表达之间的偏好，不计作错译，不得要求强制修复。
本次没有任何已审定译名规则。摘要和实体描述只能帮助寻找线索；历史译法建议必须定位到实际R/L正文。

每条FINDING只定位一个S段和一个T段。同类错误若出现在多个T段，分别输出多条；不得只修举例段。
source_ref、target_ref、evidence_ref只写提供的一个标签。程序将绑定对应完整段落，不必复制引文。
若仍附带“标签 | 引文”，引文必须是该原段连续子串，不能加引号、拼接片段或写多个标签。
外部证据只用允许的R/L；本章其他S/T段可用于内部指代核对。找不到依据就写UNRESOLVED，不编造出处。

严格使用下列字段，不输出示例中的说明或选项列表：
@@FINDING f1
category: omission|addition|negation|role|number|term|tense|speaker|style|other中的一个
severity: critical|major|minor|suggestion中的一个
basis: source_meaning|observed_usage|style_preference中的一个
source_ref: 单个S标签
target_ref: 单个T标签
claim: 原文支持的含义与草稿含义有何差异；历史译法只说已观察，不说既定/已审
suggestion: 保留正确内容的最小修复，或明确不确定
confidence: high|medium|low中的一个
evidence_ref: 可选的一个R/L/S/T标签，没有就省略整行
needs_lookup: 可选chapter:main:章号或node:节点ID，没有就省略整行

不确定问题用以下记录，不要给UNRESOLVED添加confidence等额外字段：
@@UNRESOLVED u1
text: 不确定原因及尚缺什么证据
needs_lookup: 可选查询，没有就省略整行

最后逐项复查全文是否仍有同类出现位置，输出且只输出一个SUMMARY：
@@SUMMARY
reviewed_all: yes
semantic_coverage_complete: yes或no
zero_findings: yes或no
@@END
没有问题时也须输出SUMMARY和END。标签可定位不代表意见正确，不要把报告完整当作质量证明。
【局部一致性与全书权威的边界】
本任务目标包含与已提供历史译文的人物指代和命名保持一致，不仅检查单段能否猜懂。
历史用法未审定，不表示其没有证据价值。R/L正文明确是同一实体且译法稳定时，
可确认本章命名断裂或人名误作普通词，进行局部一致性修复；不必先把译法审定为全书硬规则。
若同一实体有不同阶段/不同说话人的称呼、历史译法相互冲突或实体不能确定，则保留未决。
章内同一称谓混写属于source_meaning的内部一致性，可用另一T段作证据；
只有“历史译法”这一依据才用observed_usage并提供R/L。不要把章内矛盾误标为缺少历史引文。
同类错误在每个T段分别定位；稳定人名的命名一致性与普通措辞的风格偏好分开判断。


【记录数量与实际问题】
FINDING只记录你主张确实存在的含义差异，不能写“没有差异”“无需修改”“原译正确”的逐段检查流水账。
已核对且正确的段落不单独输出；规则误报使用SIGNAL dismissed，不输出虚假的FINDING。
还有疑点但证据不足时使用UNRESOLVED，不把未知说成已经确认的错误。
最后根据实际输出的FINDING数量填写zero_findings：0条写yes，1条或更多写no。
这个字段不统计SIGNAL、UNRESOLVED或已检查的段落数。semantic_coverage_complete只表示是否逐段完成含义检查，
不等于没有错误或已经解决所有疑点。不要复制样例ID或为了凑齐类型输出问题。"""

AUDIT_ADJUDICATION = """【裁决协议 chapter-review】
逐项核对审校意见和程序绑定的完整原段，不要默认接受审校员的说法。
只对确有含义差异、内部矛盾或客观残留确认；正常意译、可替换措辞和style_preference应dismissed。
observed_usage只能支持局部一致性的建议。核对绑定的历史原段是否真的出现对应人/物及译名；
摘要、实体候选没有审定权威，不得称已审/固定/全书规范。证据不足则unresolved。
需特别核对动作主体/受益者、对象、条件分支和译文额外增加的细节。
引文/标签存在只代表可定位；意见解释和修复方向也必须得到原文支持。
每个finding恰好输出一条：
@@DECISION d1
finding: 已提供的finding ID
decision: confirmed|dismissed|unresolved中的一个
reason: 说明源句支持的含义、草稿的实际差异和建议是否成立；不要仅说引文有效
结束标记必须独占最后一行，整行只能输出@@END；标记前后不得添加文字或标点。
@@END
不要新增finding、重写正文或将仅有风格偏好的建议确认为错误。
【局部一致性与全书权威的边界】
本任务目标包含与已提供历史译文的人物指代和命名保持一致，不仅检查单段能否猜懂。
历史用法未审定，不表示其没有证据价值。R/L正文明确是同一实体且译法稳定时，
可确认本章命名断裂或人名误作普通词，进行局部一致性修复；不必先把译法审定为全书硬规则。
若同一实体有不同阶段/不同说话人的称呼、历史译法相互冲突或实体不能确定，则保留未决。
章内同一称谓混写属于source_meaning的内部一致性，可用另一T段作证据；
只有“历史译法”这一依据才用observed_usage并提供R/L。不要把章内矛盾误标为缺少历史引文。
同类错误在每个T段分别定位；稳定人名的命名一致性与普通措辞的风格偏好分开判断。


【无差异候选与修复一致性】
若候选意见本身说“没有实质差异、无需修改、原译正确”，核对原段后应dismissed，不为它另造错误。
确认问题前同时核对修复方向。不能一面指责某种主体关系，一面建议相同的主体关系；
建议引入未被原文支持的动作、原因或身份时，不可直接照搬。实际差异或修复方向不能确定时保留unresolved。
zero_findings是记录计数元数据，不代表已有FINDING应被忽略；每条候选仍必须得到裁决。"""

AUDIT_REVIEW_FEWSHOT = [{'role': 'user', 'content': '【虚构格式样例甲】只审查以下一对句子。原文[S-demo] 彼女は赤い傘を持っていた。草稿[T-demo] 她拿着一把红伞。'},
 {'role': 'assistant',
  'content': '@@SUMMARY\n'
             'reviewed_all: yes\n'
             'semantic_coverage_complete: yes\n'
             'zero_findings: yes\n'
             '@@END'},
 {'role': 'user', 'content': '【虚构格式样例乙】只审查以下一对句子。原文[S-demo] 彼女は赤い傘を持っていた。草稿[T-demo] 她拿着一把蓝伞。'},
 {'role': 'assistant',
  'content': '@@FINDING f_demo\n'
             'category: other\n'
             'severity: major\n'
             'basis: source_meaning\n'
             'source_ref: S-demo\n'
             'target_ref: T-demo\n'
             'claim: 原文明确为红色，草稿写成蓝色，颜色信息相反。\n'
             'suggestion: 将“蓝伞”改为“红伞”。\n'
             'confidence: high\n'
             '@@SUMMARY\n'
             'reviewed_all: yes\n'
             'semantic_coverage_complete: yes\n'
             'zero_findings: no\n'
             '@@END'},
 {'role': 'user',
  'content': '【虚构容错样例丙】只核对一对句子。原文[S-demo] 彼は戸口へ歩いた。草稿[T-demo] '
             '他走向门口。此前有人建议改成“他向门口走去”，核对后只是正常意译。不要输出“无需修改”的FINDING。'},
 {'role': 'assistant',
  'content': '@@SUMMARY\n'
             'reviewed_all: yes\n'
             'semantic_coverage_complete: yes\n'
             'zero_findings: yes\n'
             '@@END'},
 {'role': 'user',
  'content': '【虚构容错样例丁】原文[S-demo] 誰かが来た。草稿[T-demo] '
             '有人来了。两者含义相同，但前文未给出，不能确定来者身份。已完成本章逐段核对，只记录这个未决，不制造错译。'},
 {'role': 'assistant',
  'content': '@@UNRESOLVED u_demo\n'
             'text: 来者身份缺少前文证据，不能确定；现有译句没有擅自确定身份。\n'
             '@@SUMMARY\n'
             'reviewed_all: yes\n'
             'semantic_coverage_complete: yes\n'
             'zero_findings: yes\n'
             '@@END'}]

AUDIT_SIGNAL_FEWSHOT = [{'role': 'user',
  'content': '【虚构信号格式样例】只审查这一对句子并处置信号。原文[S-demo] 彼女は30歳だった。草稿[T-demo] '
             '她三十岁。规则信号ID为signal-demo。样例完整输出应包含SIGNAL和SUMMARY，所有记录写完后只输出一次END；不要为了凑数输出无差异的FINDING。'},
 {'role': 'assistant',
  'content': '@@SIGNAL signal-demo\n'
             'signal: signal-demo\n'
             'decision: dismissed\n'
             'reason: 原文与译文的数值和单位一致，信号不构成翻译错误。\n'
             'source_ref: S-demo\n'
             'target_ref: T-demo\n'
             '@@SUMMARY\n'
             'reviewed_all: yes\n'
             'semantic_coverage_complete: yes\n'
             'zero_findings: yes\n'
             '@@END'}]

AUDIT_ADJUDICATION_FEWSHOT = [{'role': 'user',
  'content': '【虚构裁决样例甲】这次有两个候选，请逐条裁决，不能漏掉第二项。候选finding-demo-1：绑定原文为“红伞”，草稿为“蓝伞”，审校意见称颜色相反。候选finding-demo-2：绑定原文和草稿含义相同，意见只偏好另一种措辞。所有DECISION的记录头和finding字段均复制相应完整ID；全部记录输出后只写一次END。示例只演示格式，结论和ID不得复制到实际任务。'},
 {'role': 'assistant',
  'content': '@@DECISION finding-demo-1\n'
             'finding: finding-demo-1\n'
             'decision: confirmed\n'
             'reason: 绑定原文是红色而草稿是蓝色，意见指出了明确的颜色含义差异。\n'
             '@@DECISION finding-demo-2\n'
             'finding: finding-demo-2\n'
             'decision: dismissed\n'
             'reason: 原文与草稿表达相同含义，意见只提出措辞偏好，没有可确认的含义差异。\n'
             '@@END'}]

AUDIT_SIGNAL_ADJUDICATION_FEWSHOT = [{'role': 'user', 'content': '【虚构信号裁决样例】信号ID为signal-demo；原文和译文数值相同，示范误报裁决格式。'},
 {'role': 'assistant',
  'content': '@@SIGNAL signal-demo\n'
             'signal: signal-demo\n'
             'decision: dismissed\n'
             'reason: 绑定原文与译文数值一致，该候选是误报。\n'
             'source_ref: S-demo\n'
             'target_ref: T-demo\n'
             '@@END'}]

AUDIT_PROFILES = {'chapter-review': (AUDIT_TASK, AUDIT_ADJUDICATION)}


def review_examples(profile, *, with_signals=False):
    if profile != 'chapter-review':
        raise ValueError('unknown review profile')
    examples = list(AUDIT_REVIEW_FEWSHOT)
    if with_signals:
        examples.extend(AUDIT_SIGNAL_FEWSHOT)
    return examples
