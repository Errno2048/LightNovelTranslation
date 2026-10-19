# 分块摘要：稳定输出、宽容解析与有界格式修正

2026-10-16。承接 [失败原因验证](translation-summary-failure-validation.md) 和 [多级索引设计](translation-multilevel-lazy-index-design.md)。本轮已实现于多级索引生成路径。

后续完整链路已通过 [第1200章最低工程验收](translation-p4.2-1200-acceptance.md)。本次14个选用摘要均可读取概括，保留249处缺引用和34个隔离元信息条目，未触发LLM格式修复。另补严格范围外缺章／明确概括粒度审查：保存原警告及不可变审查对象，编译和发送前重建时再次校验；未知或真实资料缺口仍阻断。最新回归190项通过。本文下方164项及局部试验用量保留为此前协议验证记录，不与完整链路混算。

## 准入规则

**可读的一级概括可以作为待审查的导航资料。引文缺失不再导致整块失败。** 逐字匹配只说明文字能定位，不证明它支持模型的整个判断。

| 问题 | 当前处理 |
| --- | --- |
| 摘要可读，但个别 ref、引文或双语证据不匹配 | 保留摘要；有效证据中移除问题项，另存 `missing_reference`、原始值和原因 |
| 单条实体损坏、日文形式不存在、译名没有出现在允许的中文里 | 隔离该条元信息；保留概括和其他条目 |
| 格式导致概括无法读取 | 尝试一次受预算约束的 LLM 转录，再用同一解析器读取 |
| 多个冲突概括、冲突核心 JSON 键或多份 JSON 文档 | 不擅自选择，也不让格式修正模型裁决 |
| 真正的资料覆盖缺口、依赖失效、隐藏资料或预算超限 | 仍按原有边界阻断，与缺失引用分开 |

新版本为 `lazy-two-level-bilingual-v3-resilient-flat`，协议为 `summary-flat-v3`。旧 `lazy-two-level-bilingual-v2-tail-task` 摘要仍按旧 prompt 和严格规则读取；新建计划使用新版。旧计划不能直接替换 prompt，应重新规划。旧实验 codec 和早期单层摘要路径不修改。

## Prompt 与记录格式

`summary_protocol.protocol_messages()` 保持完整、允许范围内的日文和中文对照，继续使用 100／10 章策略及 1,048,576 tokens 上下文配置。系统消息及正文末尾重申任务、数值上限和全部字段格式。

默认整体概括最多 2500 字，无下限；最多 20 个实体、5 个固定表达，每条引文最多 160 字。先写概括，后写可选元信息。实体类型完整列出为 `person / organization / place / item / ability / species / role / other`。保留时间主体、方向、回忆与当下的区别，不猜身份、性别、说话人或知情状态。中文用于识别既有译法，剧情和描述依据日文。

格式示意，下面是占位说明，非真实作品知识：

```text
@@OVERVIEW
text: 事件、人物、前因后果、转折、结果和未决线索。
  多行正文的续行缩进两个空格。
reference_note: 无法可靠提供逐字引文时说明原因。
@@ENTITY e1
kind: person
source_form: 日文原样形式
description: 作品中的角色及本块状态；无依据则留空。
@@RENDERING e1
source_form: 上述日文形式
target_form: 已有中文实际用法
usage_scope: 本节点中具体适用的称谓或场景
pairing_id: pair:main:601
ja: ja:main:601@0 | 对应日文片段
zh: zh:main:601:0@0 | 对应中文片段
@@UNRESOLVED
text: 尚不能判断的事项。
@@END
```

记录只有开始标记，没有成对闭合符号；`@@END` 可选。自然语言引号和括号无需 JSON 转义。多个日文形式重复 `source_form`；多个译法重复整条 `@@RENDERING` 并沿用所属实体 ID。其他记录包括 `@@FIXED / @@CONFLICT / @@COVERAGE`。拿不准引用时省略证据并写 `reference_note`，不能编造引用或把它记为剧情覆盖缺口。

## 本地解析和质量侧录

`parse_output(raw)` 仅处理结构；`decode_summary(raw, pairs, config)` 再检查作品形式、引用和候选元信息。

| 输入偏差 | 本地行为 |
| --- | --- |
| BOM、CRLF、完整围栏、外层说明、单元素 JSON 数组 | 规范化并记录；只接受无歧义的单份 JSON 文档 |
| 平面标记大小写、适度缩进、全角冒号／引文分隔符 | 规范化并记录 |
| 无 `@@END` | 警告，不阻断 |
| `SUMMARY`、`source_form_alt`、`source_forms` 等已知别名 | 按有限别名表映射，不做模糊猜测 |
| 多行正文、未缩进续行 | 保留；未缩进续行加注记 |
| 同一未决／冲突／覆盖记录重复 `text` | 按列表逐条保留，不误当成冲突标量 |
| 旧 JSON、字符串形式 overview | 兼容解析并记录转换 |
| 字符串外的 JSON 尾逗号 | 删除并记录位置，不改引号内的 `,}` 或 `,]` |
| 历史已确认的 metadata 少一个闭合花括号 | 结构扫描后，仅在完整 JSON 重解析通过时插入并记录位置 |
| 未知记录／字段、孤立译法、重复实体 ID、可选字段损坏 | 原始输出留存，单条隔离，不随意合并或归属 |
| 未知实体类型 | 中性映射为 `other`，另存 `original_kind` 和告警 |
| 超出请求字数／条数 | 标记，不静默截断；上下文编译仍有硬预算 |

解析前限制输出 500,000 字符、结构深度 128、平面记录 2000 条；超限直接保留失败，不继续送模型。空输出、无法辨认的结构和缺少可读概括不能视为成功。

引用仅保留语言正确、ref 存在、引文在对应段落逐字出现且长度合规的项。双语用法还要求同一配对，且分别包含声明形式。只可提示同章精确匹配的其他位置，不自动迁移引用或跨章寻找证据。

规范化内容仍使用原五字段结构，完整审计独立保存于 `artifact.quality`：

```json
{
  "status": "usable_with_annotations",
  "semantic_status": "unreviewed",
  "reference_status": "missing_reference",
  "missing_reference_count": 1,
  "references": [{
    "path": "/overview/evidence/0",
    "ref": "ja:main:601@8",
    "quote": "模型原输出片段",
    "status": "missing_reference",
    "reason": "quote_mismatch",
    "suggested_refs": []
  }]
}
```

完全没有提供引用也有明确缺失记录。`reference_note`、字段转换、隔离项与覆盖缺口分别保存。`literal_matches_only` 仅表示通过字面定位，语义仍为 `unreviewed`。

父元信息汇总把状态传给对应实体、具体译法和固定表达；翻译 prompt 只注入简短支持状态，不复制整个审计日志。摘要标签增加 `node_id / chapters / reference_status / missing_reference_count / lookup`，预留后续获取二级摘要或原文的定位信息；本轮没有实现自动检索 agent。

真正的非空 `coverage_notes` 仍阻断完整前情编译。粗化摘要、未构建全部子节点、缺少引用均不应写成剧情覆盖缺口。

第1200章实测中，模型仍将政策隐藏章节、正常概括说明和“无”写入此字段。`scope_coverage.py`新增 `review_policy_exclusions()`／`read_scope_review()` 与 `review_declared_compression()`／`read_compression_review()`，只识别明确整句语法，核对节点、隐藏范围或可见番外归属，生成带策略和原警告摘要hash的不可变审查；原 `coverage_notes` 不删除。编译需显式携带 `coverage_scope_reviews`／`coverage_compression_reviews`，发送前重建再次验证。原文“无深入描写”等实质判断不属于这种范围／粒度豁免，也不由格式修正补写。该接口不确认摘要事实准确，完整规则和开发证据见验收报告。

## 一次性 LLM 格式修正

流程为：原始响应落盘 → 本地解析与准入 → 必要时一次格式转录 → 同一解析器二次读取 → 保真检查 → 使用或保留失败。

引用不匹配、未知类型、可选元信息损坏、长度告警本身不触发付费修正。多个冲突概括、核心键歧义、超限输入也不送模型裁决。HTTP 超时、网络失败、拒答或输出截断不进入摘要格式修正。

修正模型只收到原模型输出、解析阻断原因、字段格式和数值上限，不重新读取小说全文或隐藏章节。要求逐字转录完整值，不润色、不补剧情、不改分类、不补造引用；无法确定时返回不可修正。

二次解析后必须满足：

1. 概括可读，没有新的结构歧义或未知记录。
2. 摘要、描述、姓名、译名、范围、引文等完整值来自原输出，不能新增或删掉完整值。
3. 已恢复字段及从损坏 JSON 中独立读出的完整顶层字段保持原位置，不能交换概括与人物描述。
4. 失败或未通过保真检查则终止，不递归修正。

这些是格式转录及局部结构检查，不证明原摘要事实正确，也不是通用修复器。字符串边界严重损坏或归属不明时仍可能拒绝恢复。

默认同一原输出／双语范围／模型／tokenizer 最多一次修正；同一账本最多两次。单次修正输入估算最多 64,000 tokens，输出预留 8192 tokens。修正计入全局请求及 token 额度，计划展示条件预留。默认总输出预算从 256,000 调整为 300,000，容纳 16 次摘要及两次修正的最坏预留。设置 `max_repair_requests=0` 可关闭兜底。

摘要与修正使用独立账本项；响应在解析前保存为不可变对象。请求、原响应、修正响应、用量、两次解析、保真判断和失败都留存。恢复时读取已有成功或失败结果；解析进程异常也重用已落盘响应。HTTP 自动重试必须为零，未知远端结果保留预算。

准入结果另外携带 `admission_version`。解析规则升级后从原响应生成新的不可变解析产物，旧产物保留，不必重新支付生成费用；已有格式转录同样重新解析后使用。

## API 与验证

| API | 返回或新增参数 |
| --- | --- |
| `summary_protocol.protocol_messages(node, pairs, config)` | 完整双语平面协议 prompt |
| `summary_protocol.parse_output(raw)` | 本地结构结果、问题和隔离项 |
| `summary_admission.decode_summary(raw, pairs, config)` | `{usable, content, quality}` |
| `summary_admission.component_quality(quality, path)` | 对应实体／译法的质量 |
| `summary_repair.repair_summary(...)` | 有界修正，含 `status / repair_id / decoded / fidelity` |
| `plan_multilevel_summaries(...)` | 新增 `max_repair_requests / repair_max_input / repair_max_output` |
| `run_multilevel_summary_plan(...)` | 本地解析及必要时一次兜底，成功产物带质量侧录 |
| `read_node_summary(...)` | 重查依赖、原响应、修正来源、质量和内容，批量加载相关正文以免恢复时逐章扫描语料 |

自动回归 `164 passed`，其中新增 35 项专项测试覆盖兼容、隔离、冲突拒绝、引用、改写／删值／移动字段拒绝、预算、网络异常、缓存、解析中断恢复、解析版本升级、重复列表字段及旧版本读取。现有 EPUB 依赖弃用告警仍存在。

历史回放 16 份真实输出，含 11 份平面记录和 5 份 JSON：15 份在本地保留概括，新增 API 调用为 0；原先未写出有效概括且声称没有资料的一份仍失败。四份 JSON 通过已知 metadata 缺括号修复恢复；缺少 `@@END` 和 `source_form_alt` 案例均恢复。它们是旧案例回放，不能据此推算新样本成功率；引用问题仍保留，未宣称语义正确。

真实小规模试验仅新增两次 API 请求：

| 项目 | 结果 | 实测输入／输出 tokens |
| --- | --- | --- |
| 新 prompt，main:601–610 完整双语 | 直接可解析，未调用兜底；概括 1393 字，20 个实体、18 个译法，9 条引用缺失标记 | 57,792／5286 |
| 缺逗号合成样例，以历史结果及前两个实体构造 | 一次转录后解析通过；无新增／丢失完整值，无可恢复字段移位 | 3635／2583 |

合计输入 61,427、输出 7869，共 **69,296 tokens**，其中修正输出包含 32 个 reasoning tokens。引用问题没有阻断概括。新响应省略结束标记、使用未缩进段落、在同一未决记录重复 `text`，均已本地容错；五条未决线索逐条保留。

559 个受保护语料和历史试验文件哈希不变。已验证完成后的缓存恢复新增请求为 0。没有重跑 100 章摘要，没有生成目标章译文，全部前情覆盖仍未完成。完整结果和用量在 `p4.2-resilient-summary/live/result.json`；完整新 prompt 在 `live/io/summary.prompt.md`，原始响应在 `live/io/attempt-01.output.txt`，修正请求及原始／二次解析在 `live/io/repair.result.json`。当前仍是语义未审查候选。

```powershell
# 默认只读回放，零 API 调用
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_2_resilient.py
# 使用生产接口的离线验证
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_2_resilient.py --mock
# 独立有界真实试验，完成后重复运行使用缓存
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_2_resilient.py --live
py -3.11 -m pytest tests/translation -q
```

回放、完整 prompt、输出、质量注记、修正前后文本和预算账本均位于 `artifacts/translation/tenken-cache-clean/verification/p4.2-resilient-summary/`。旧试验和语料另做哈希保护。
