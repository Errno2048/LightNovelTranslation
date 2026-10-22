# 11 审校、裁决与修订

[返回目录](index.md) · 上一步：[输出覆盖](10-translation-output.md) · 下一步：[增量知识](12-incremental-knowledge.md)

## 审校链路及上下文

当前默认 `chapter-review`。链路是“版本 → 覆盖 → 确定性检查 → LLM 审校 → LLM 裁决 → 必要时修订与复审”。各角色共享冻结的来源范围与引用规则，但收到的任务、结构化候选和输出协议不同，不能将它们视为彼此独立的事实验证器。

审校/裁决可见当前日文和译文、实际入选的近章完整日中、分层导航/知识候选、定向局部证据，以及跨版本入选的中文正文和术语。review context 注册每段引用及允许用途；新译文与 candidate 知识不自动成为事实。详细上下文边界见 [09](09-context-evidence.md)。

只给同一模型更丰富材料，不能保证找出所有错译。检出率、证据正确率、修复是否引入新错应分别评估；工程 complete 仅说明该协议闭环完成。

## 规则检查和数字信号

空译文、未完整生成、覆盖映射不完整等是 hard failures。长度、残留假名、括号、标记及数字异常主要作为 signals，交给审校/裁决解释，而不是看到数字差异就自动替换。

数字检查以对应段组为单位，识别量值、单位和部分限定词；执行 NFKC 并保留定位，保护“精一杯”“一生懸命”“万が一”等词形。通常不把缺少量词依据的所有汉字数字都算作数量。即便两边数值集合相同，也可能发生对象或范围错配，需要语义检查。

音译技能名暗含“四”等语义、奇幻世界自定义计量、非字面隐喻不可能只靠规则穷尽。当前本地词典工具支持 WikDict SQLite、Kaikki 数字 JSONL 或小型 JSON 等后端，带来源信息；不会自动下载词典。公共任务默认没有注入词典后端，不能把数字保护词表等同于完整日中词典。

## 模型输出怎样变成可用报告

模型协议需声明 `reviewed_all: yes`、`semantic_coverage_complete: yes/no` 和 `zero_findings: yes/no`，解析后得到 `review_complete`、`semantic_coverage_reviewed` 等报告状态，并区分 findings、unresolved 与覆盖审查。无问题也按记录格式输出，不能仅返回一句“没发现问题”。共享平面解析器容忍有界语法变体；few-shot 规定了零问题与有问题的形状。

当前计数容错策略可在记录结构完整时，从实际 FINDING 数量推导缺失或矛盾的 zero_findings，并保留 declared_summary、protocol_warnings；不能借此补全缺失的语义覆盖声明。有一条 FINDING 却写 zero_findings: yes 时，候选仍须进入裁决，不会因为计数标记错误而被丢弃。

有 findings 或规则 signals 时，裁决模型判断 confirmed/dismissed/未决并给证据及理由；无候选时可保存本地空裁决，不代表又独立完成了一次语义证明。格式修复只能恢复协议内容，不能为了通过解析而删除意见或补造证据。

## 修订和接纳规则

confirmed 问题进入有限修订：修订模型提出针对当前版本的改动，生成新版本后重新覆盖、检查、审校和裁决。初始 `max_revisions` 默认 1，配置可设 0–2。修订次数与请求格式重试次数不是同一预算。

修订没有实际改动时，允许一次有记录的重新裁决；恢复复用同一次响应，不能不断循环直到模型同意。严重 confirmed 问题在修订后仍存在，会阻断提交。

| sequence mode | 接纳含义 |
|---|---|
| `working` | hard failures 清除、审校完成且无剩余严重 confirmed 问题后，可带未决/轻微问题提交；标签为 experimental_working、semantics_unreviewed |
| `accepted_only` | 进一步按 acceptance 规则要求 findings、signals、未决等均有合格处理，并保存接纳对象；仍不等于人工审定 |

一次裁决引用合法不代表它正确理解了语义。working 不应被描述为“所有问题修好”，accepted_only 也不应被描述为“模型质量保证”。

## Agent 怎样接管阻断草稿

先用 `job.status()`、`job.draft()`、`job.explain(chapter)` 看当前版本、证据和失败原因，按 [诊断与恢复](../diagnostics.md)选择操作。

- `extend_revision_budget(additional_rounds=1, reason=...)`：仅用于当前 review_blocked 草稿，追加有记录的修订机会，总上限 8；不改初始 spec，不清空旧响应，不重置累计 tokens/requests。
- `submit_revision({draft_alias: "单段新译文"}, expected_version_id=..., reason=...)`：仅改当前阻断且尚未 prepared 的版本。别名必须属于当前草稿；每项为非空单段，无换行、`@@` 或围栏。使用版本校验避免覆盖并发修改；需有剩余修订额度。
- `resume()`：新版本重新接受整套审校，不直接通过人工提交跳到 committed。

先验证问题再申请或使用有界额度，不要删库、更换任务名或手改 committed 对象绕过阻断。用户已授权范围内可由 agent 执行明确修复；信息不足时保留未决，不能伪造证据。

## 代码位置

- `src/translation/review/context.py`、`prompts.py`、`protocol.py`：上下文、few-shot 与报告解析。
- `src/translation/review/checks.py`、`signals.py`：hard failures 与规则信号。
- `src/translation/mvp/numbers.py`、`dictionary.py`：数量检查与可选词典后端。
- `src/translation/review/service.py`：模型审校与裁决；`revision.py`：修订方案及应用。
- `src/translation/review/acceptance.py`：`plan_acceptance`、`accept_translation`，正式接纳门槛。
- `src/translation/sequence/service.py`：`SequenceEngine._prepare`、`advance_sequence`，working/accepted_only 与有界复审。
- `src/lightnovel_translation/job.py`：`extend_revision_budget`、`submit_revision`，agent 恢复入口。
- 回归入口：`tests/translation/test_library_api.py`、`test_reference_workflows.py`。
