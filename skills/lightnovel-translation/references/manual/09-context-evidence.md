# 09 翻译上下文与证据工具

[返回目录](index.md) · 上一步：[任务规划](08-job-planning.md) · 下一步：[译文协议](10-translation-output.md)

## 完整 prompt 由哪些部分组成

连续上下文采用检查点加追加轮次的方式。具体任务的 `messages` 是唯一准据，不能只读 prompt 模板就断言模型看到了全部材料。

| 部分 | 内容及用途 |
|---|---|
| system / 检查点前缀 | 翻译要求、协议、知识工作集、层次导航及必要历史双语 |
| 检查点后的历史轮次 | 已提交章的 user/assistant 翻译轮次，保留连续语境 |
| 增量与未决信息 | 新候选、缺口和前章未决；显式标记为非权威 |
| 邻近原译及层次材料 | 近章完整日中、近块细节、远块概括；压缩规则见 [13](13-compaction.md) |
| 跨版本参考 | 实际选入的中文正文、译名候选、用途限制；见 [06](06-reference-retrieval.md) |
| 定向证据 | 与本章名字/片假名及局部段落有关的历史原文、中文证据 |
| 当前目标 user 消息 | 当前完整日文逐段标记、输出要求与合法源段 ID，位于末端 |

公共主线默认没有自动冻结的全书权威核心人物卡；`core` 为空时不能声称模型必然获得主角的完整背景。候选来自索引/增量抽取，其数量及出处需看实际对象。

## 怎样取得实际输入输出

运行前看 `plan = job.plan_next()` 中的 `context.messages`、budget 和参考 selection。运行后用：

```python
artifacts = job.explain(chapter)   # 本地导出该章诊断
job.export(include_io=True)       # 导出整个任务已有材料
```

`explain` 会按当前阶段导出 `context.json`、`prompt.txt`；有原始请求时保存 `actual-request.json`、`actual-response.json`、`raw-generation.txt`；已提交章有 `commit.json`、`translation.txt`，阻断章有 draft/review-state 等。容量规划失败时可能只有 `budget-diagnostic.json` 和 `prompt-not-sent.txt`，不能称其为已发送 prompt。

工具追问、格式反馈、审校及修订可能产生后续请求；追踪它们应结合保存对象中的 request/generation IDs 与账本。`prompt.txt` 是可读上下文，实际调用以对应保存请求为准。I/O 含用户提供的小说正文，交付时按用户要求保存，不必整份贴进对话。

## 章、段与引用注册表

`stable` 在连续任务中采用 `C...` 章 ID 和 `C....J001` / `C....Z001` 段别名；同章中文可跨多个文档连续编号。独立版本索引使用另一套原生 J/Z 哈希段标识。协议需要原样复制当前注册 ID，不混用标题话数、阅读位置和别名。

审校上下文建立统一 row registry，记录引用能支持的用途。跨版本中文只能支持其风格/术语候选，不能证明 Web 剧情。引用解析成功说明确实指到了可见段，不说明模型的推理成立。

## 定向术语与局部证据怎样选

`targeted_evidence=true` 是确定性召回，不额外调用模型。它从本章片假名/实体表记选词，加入当前命中前后约两段非空正文，并查历史首次/最近出现；中文侧用共享表记或已知译名锚点找局部证据，不按日中段号硬配。

当前默认约束为最多 12 个候选词和 `targeted_evidence_tokens=12288`；预算不足时缩小选取规模，最多若干次减半再检查。该机制可以缓解专名漏召回与局部语义不足，但不能保证解决 fresh/flesh 之类依赖复杂剧情推理的歧义。没有共同表记或候选译名时也可能召回不到中文。

## 模型主动请求的只读工具

EvidenceBroker 在审校/裁决等证据流程中提供受限查询；翻译阶段需 `translation_tools=true` 才启用。请求使用 `@@QUERY` 记录和单行 JSON params，属于应用层协议，不是 Provider 的原生 function calling。

| action | 用途与边界 |
|---|---|
| `search_evidence` | 在允许可见的来源中找表记/上下文 |
| `read_evidence` | 按已注册标识读取局部段落 |
| `read_index` | 获取已存在索引；缺失时不付费隐式构建 |
| `lookup_dictionary` | 查询已配置本地词典；可能返回 not_configured |

默认每个持久 scope 限制 6 个不同动作、约 32000 返回 tokens；同一 scope 的工具结果跨角色、修订和恢复复用，不是每次请求都重新获得无限额度。翻译追问还与该逻辑翻译任务的尝试上限共享，工具开关不代表可以无限往返。

公共新项目未自动配置词典后端；仅有 lookup 工具名不代表运行时有在线日中词典。当前工具不下载资源，也不把候选知识升级为确定事实。

## 代码位置

- `src/translation/sequence/context.py`：`source_message`、`full_pair_message`、检查点/追加上下文编译。
- `src/translation/sequence/targeted.py`：定向表记和局部证据选择。
- `src/translation/references.py`：`chapter_ref`、`pair_rows`、`prompt_projection`，别名与 prompt 投影。
- `src/translation/editions/sequence.py`：跨版本参考合并与最终选择。
- `src/translation/mvp/evidence.py`：`EvidenceBroker`、`tool_guide`，工具范围及耐久限额。
- `src/translation/review/context.py`：统一审校/裁决引用上下文。
- `src/translation/sequence/service.py`：`SequenceEngine.explain_context`，实际请求与诊断导出。
- 回归入口：`tests/translation/test_reference_workflows.py`、`test_library_api.py`。
