# 诊断、预算与可追溯恢复

查阅顺序：`status()` → `reference_report()` / `draft()` → `plan_next()` → 当前章 `explain()`。这些接口不调用模型，`plan_next` 可能保存本地 checkpoint。只有这些产物不足以解释错误时再定位源码。

## 参考正文与术语

| 现象 | 检查与处理 |
|---|---|
| actual_style_document_ids 为 null | 该章没有保存请求；查看计划中的 selection |
| 没有中文正文 | 检查 no_chinese_style_prose、token_budget、remaining_context_budget；重新选择小一些的样本或调整新任务配置 |
| 只有 related_context | 可以作文风样本，不能当同事件证据；不要强改 event_overlap |
| 译名未召回 | 查看 terminology_trace 的候选来源、遗漏及 search_scope；允许搜索未建索引的实际中文正文 |
| no_match / ambiguous | 只是本次有限范围的结果；不证明译本中不存在 |
| no_reference_scope | 检查 scope_failure_reason：无中文正文、源局部上下文本身超限、或参考片段仍超限 |
| partial / 目录未覆盖 | 补建有用节点；不必为了一个人物全书重抽 |

`index.resolve_terminology(chapter, source_forms=["实际日文名称"], associations=..., entity_matches=...)` 可显式定向补查并保留出处。返回 search_scope、searched_reference_refs、partial_reference_documents、document_limit_omitted 与 trace。只有实际送入模型的段落才可被接受为引用。

搜索包含已提供的中文参考版本，不依赖严格剧情映射，也不会搜索未提供的卷。当前是文字线索检索：日文局部上下文、已知译名等用于提名段落，跨语言词形差异很大时仍会漏召回；必要时 agent 使用中文目录/摘要定位更合适范围。字面命中本身不证明人物相同。

## 五类不同预算

| 配置/接口 | 作用 | 如何调整 |
|---|---|---|
| capacity.context_window_tokens / max_input_tokens | 单次请求的实际输入容量 | 按端点真实限制填写 |
| roles.*.requested_output_tokens | 单次输出，包含服务商计入其中的推理 | 分角色配置；纯推理截断考虑减小批次 |
| references.* | 参考包、补查输入/输出、候选数量 | 默认根据模型容量计算；可显式覆盖 |
| limits.* / max_attempts | 每个 index/job 的持久请求、tokens 和协议/传输尝试 | 不因恢复重置；不能换任务名规避 |
| sequence.max_revisions | 每章语义修订次数 | 初始 0–2；阻断后可显式追加有界机会 |

不设 packet_token_budget / terminology_input_tokens 时，库先扣除输出和安全余量，再除以 token_inflation_factor。补查默认使用可用输入的四分之一，输出沿用 indexer 角色的额度；最终请求还经过完整容量及校准检查。大上下文不会自动提高每个角色的输出，也不会抹掉 limits 的总账。

只输出 `config.public()["reference_budgets"]` 等脱敏字段可检查有效预算，不能打印原始配置对象。可用 `ReferenceConfig(...)` 或 TOML `[references]` 覆盖；创建任务的 packet_token_budget 参数优先，仍受容量上限约束。

## 修订次数用尽

不需要删除数据库、改导出译文或重新翻译已完成章节。用户已授权继续翻译且错误可修复时，可在原任务上记录理由并追加机会：

```python
with project.open_job("continuation-v2") as job:
    print(job.draft())
    job.extend_revision_budget(additional_rounds=1, reason="复审新发现否定反转，追加一次修订。")
    result = job.resume(max_chapters=1)
```

这会追加不可变恢复记录，保留原冻结配置和总调用账本。扩展只适用于有草稿的 review_blocked；有效修订上限最多 8。追加机会不发模型请求，resume 才执行修订与复审。不要无界循环到模型偶然通过；同一错误反复出现时应检查证据或由 agent 做局部修复。

## Agent 局部接管

```python
draft = job.draft()
# 从 draft["paragraphs"] 读取准确 alias；从 explain 输出核对对应原文和审校证据。
changes = {actual_alias: "对照原文后确定的完整替换段落。"}
if draft["round"] >= draft["revision_limit"]:
    job.extend_revision_budget(reason="Agent 对已确认问题作局部修正。")
job.submit_revision(changes, expected_version_id=draft["version_id"], reason="原文是否定，草稿漏掉否定。")
result = job.resume(max_chapters=1)
```

`submit_revision` 要求当前阻断版本、完整既有审校与准确 alias；每项替换只能是一段非空正文。生成新的不可变版本，保留旧证据及未决问题，再完整复审；不把 agent 意见当成人工验收。它消耗一次语义修订机会，但自身不调用模型。模型身份、参考材料或源语料改变仍需新任务，不能借此修改冻结上下文。

## 其他失败

- pending_request：远端完成状态未知，先检查产物；不要直接重发。
- truncated 且正文为空、reasoning_tokens 耗尽输出：输出预算问题；不是 JSON 格式问题。
- 协议/段落覆盖失败：检查已保存原始响应，不能靠空译文、重复段落或删错误标记通过。
- 语义复审新发现错误：区分原先漏检与修订引入；查看版本差异。预算增加不保证审校正确。
- 模型/tokenizer 身份变化：旧任务拒绝直接恢复；保留旧任务，建立明确的新任务。

## 语料校验的成本

同一 Project 会话内，语料文件的路径、大小、mtime/ctime 等未变化时复用已校验快照；变化时重新完整校验。关闭重开 Project 会重新验证。导入的语料应视为不可变，`project.validate_inputs(force=True)` 可显式重新计算内容指纹；仅文件属性检查不能对抗人为保留属性的改动。不要在轮询状态时反复重建 Project。
