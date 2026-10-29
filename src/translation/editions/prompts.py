"""Versioned prompts shared by command-line runs and offline replay."""
import json

PROTOCOL = 'independent-editions-v1'


def j(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


SUMMARY_SYSTEM = """你为一本小说的一个版本建立可追溯索引。仅依据本次正文，不使用作品记忆或另一个版本。
用中文概括，原始人物、技能等名称保留本版本原始表记；日文正文不要猜中文译名。
读完整块，概括行动、参与者、起因和结果，避免只罗列主题词。保留不同叙事线、回忆/插叙以及叙述者未知的状态。
人物描述写在作品中的角色/行为，普通词不抽作专名。技能名称与持有人此刻等级分开；同名未必同一人，匿名角色不猜身份。
前驱摘要仅帮助理解，不能当成本块证据。evidence_ref只能填本次正文给出的一个段落标签，不需要复制引文。
概要以2500字为软目标，不设下限；完整性优先，超长仍可存储。提取5至20个关键事件（不足则按实际），最多64个重要实体；不要为凑数量编造。
用平面记录，无需匹配括号；可换行续写字段值。字段固定如下，没有的可选记录省略。最后独占一行@@END。
@@OVERVIEW
text: 整块连贯概括
@@EVENT e1
text: 具体事件概括
actor: 正文明示的主体或未知
action: 行动
cause: 原因或未说明
result: 结果或未说明
evidence_ref: 本次一个正文标签
@@ENTITY n1
form: 本版本实际名称（必须逐字出现在证据段）
kind: person或skill或place或organization或item或other
description: 角色或用途描述，不附会其他版本
evidence_ref: 包含该名称的正文标签
@@TEMPORAL t1
text: 明确的时序/倒叙关系，区分阅读位置与事件时间
evidence_ref: 正文标签
@@KNOWLEDGE k1
knower: 文本中的知情主体
text: 谁知道/误以为什么，在哪里披露
evidence_ref: 正文标签
@@COVERAGE
processed_all: yes
entities_exhaustive: no
omissions: 无，或明确尚未处理的内容
@@END
以上是格式说明，所有示例值必须替换为实际内容。不要输出说明、JSON数组或另一个版本的译名。"""


def summary_messages(node, documents, predecessor=()):
    payload = {'edition_id': node['edition_id'], 'node_id': node['node_id'],
               'predecessor_navigation_only': list(predecessor),
               'documents': [{'entry_id': d['entry_id'], 'title': d['raw_title'],
                              'reading_order': d['edition_order'],
                              'paragraphs': [{'ref': r['ref'], 'text': r['text']} for r in d['rows']]}
                             for d in documents]}
    return [{'role': 'system', 'content': SUMMARY_SYSTEM},
            {'role': 'user', 'content': j(payload) + '\n正文结束。只对本块提取，并以@@COVERAGE和@@END结束。'}]


MATCH_SYSTEM = """比较同一作品的Web日文与中文文库两个版本的L2摘要。它们可能重排、增删、改写，且结局不同。
只判断共同事件，不要求一一对应或单调顺序。允许多个源块关联同一参考块，也允许完全没有对应。
相同迷宫、技能、战斗主题或人物不能证明同一事件。比较主体、具体行动、起因、结果和叙述视角。
章节标题及序列只作辅助，S编号跨卷会重置。不要依据作品记忆解开叙事诡计。
对输入给出的每一组笛卡尔积(source_node,reference_node)，恰好输出一条ASSOC，包括无关的组合。
relation只能是event_overlap（有共同事件）、related_context（仅背景/阶段相关）、unrelated、uncertain。
event_overlap必须给双方一个实际匹配的EVENT ID；即使共同事件存在，也明确块中其他内容差异，不能称整块译文等价。
@@ASSOC a1
source_node: 输入源块ID
reference_node: 输入参考块ID
relation: event_overlap
source_event: 源块一个事件ID
reference_event: 参考块一个事件ID
reason: 共同的具体行动及结果，或者为什么不对应
differences: 增删、视角和事实差异；不能确认就写未知
@@END
负例格式示意：同为大迷宫，一个描述王子一行穿越、另一个描述早年冒险者探索，应判related_context或unrelated，不能仅因场景相同判event_overlap。
正文增删不妨碍局部共同事件，但共同背景不构成共同事件。"""


def compact_summary(summary):
    return {key: summary[key] for key in ('node_id', 'overview', 'events', 'temporal_claims', 'knowledge_claims', 'coverage')}


def association_messages(sources, references, nodes):
    payload = {'source_blocks': [dict(compact_summary(s), titles=nodes[s['node_id']]['titles'],
                    reading_range=nodes[s['node_id']]['reading_range']) for s in sources],
               'reference_blocks': [dict(compact_summary(s), titles=nodes[s['node_id']]['titles'],
                    reading_range=nodes[s['node_id']]['reading_range']) for s in references]}
    return [{'role': 'system', 'content': MATCH_SYSTEM}, {'role': 'user', 'content': j(payload)}]


ENTITY_SYSTEM = """根据两个版本在候选L1范围内的实体目录匹配实际译名，目录包含未配对L2的实体。
实体不必来自已匹配事件。核对类型、定义性描述和实际用例；版本中的状态变化不等于不同实体。
不得单凭同名合并不同人，不猜匿名角色真实身份，不把技能与等级、个体与种族混成同一实体。
实体身份与当前称呼形式分开：全名、爱称、绰号、称号不能无条件互换。
reference_rendering应选该日文出现形式对应的中文实际用法；可从中文用例中选取实体主名之外的爱称，但必须逐字存在于rendering_evidence_ref的正文。
只有对象关联而无合适称呼证据时保留歧义，不把爱称扩写成尚未披露的实名。无法确认不输出supported。
允许不匹配、一对多和歧义。仅输出有依据的候选，未匹配实体不需逐项占位。
@@ENTITY_MATCH m1
source_entity: 提供的日文实体ID
reference_entity: 提供的中文实体ID
status: supported
reason: 对象相同的依据与用法范围，描述差异不强行抹平
reference_rendering: 对应当前出现形式的中文实际表记
rendering_kind: name或nickname或title或other
rendering_evidence_ref: 包含该表记的实际中文用例标签
@@SUMMARY
reviewed_all: yes
@@END
status只允许supported、ambiguous、rejected。supported仍是模型候选，不是人工核验的全书规范。"""

TRANSLATION_SYSTEM = """将提供的日文Web正文忠实、自然地译为简体中文，完整翻译所有正文段落，只输出译文正文。
Web原文及允许的Web历史决定行动、指代、时间、数量、因果、技能效果和知情状态。
绑定该Web原文的新译文可提供邻章连续性。实体匹配是实际用法的候选，核对对象相同后参考。
文库材料位于独立的style_documents区域，只供译名和语言风格参考；没有逐章或逐句对应保证。
不要因为文库多出/删去情节而补写/删改Web，不据文库提前消除Web的匿名、含混或误解。
相关摘要帮助定位但不是已核验事实。若存在冲突，忠于Web；不要输出解释或将来源标签带入译文。"""


def translation_messages(packet, target):
    body = {'reference_packet': {k: v for k, v in packet.items() if k not in {'dependencies', 'fingerprint'}},
            'target': {'entry_id': target['entry_id'], 'title': target['raw_title'],
                       'paragraphs': [{'ref': r['ref'], 'text': r['text']} for r in target['rows']]}}
    return [{'role': 'system', 'content': TRANSLATION_SYSTEM}, {'role': 'user', 'content': j(body)}]
