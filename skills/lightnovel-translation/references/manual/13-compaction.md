# 13 上下文压缩与检查点

[返回目录](index.md) · 上一步：[增量知识](12-incremental-knowledge.md) · 下一步：[恢复导出](14-persistence-recovery.md)

## 压缩的对象与目的

压缩重建后续翻译使用的消息前缀，不删除已保存译文、原始请求或规范摘要。它以历史覆盖和候选知识为边界，把远处正文逐级替换为可追溯导航，控制连续增长并尽量保留共享前缀。

软件不直接管理服务端 KV cache。消息前缀稳定有助于支持缓存的端点复用，但是否命中取决于网关；缓存命中也不会扩大模型上下文窗口。

## R、U、H 三种策略

| strategy | 实际行为 | 容量限制 |
|---|---|---|
| `R` | 以滚动方式本地重建检查点，保留配置要求的材料 | 不代表无限容纳，仍检查最低必要上下文 |
| `U` | 保留追加增长，不自动执行 H 式分层压缩 | 到预算限制时可能阻断 |
| `H` | 达到软输入阈值或跨 sealed 父边界时建立分层检查点 | 默认 trigger 0.85、target 0.65；目标比例不是保证 |

`n_chapter` 默认 3、`n_child` 默认 2。`soft_input_tokens` 可更早限制输入；最终预算仍受 capacity、角色输出和校准系数约束。

## 一个检查点保存什么

检查点记录 head、依赖提交、节点 revisions、messages、知识 workset、未决、抽取缺口和覆盖清单。按层次选择：

1. 最近若干章保留完整日文与中文。
2. 邻近若干 L2 保留规范整体概括及详细记录。
3. 更远范围使用 L1 整体导航，必要时再压缩导航文字。
4. 尚未可靠抽取的已提交章仍保留 mandatory full_pair，即使它已经离当前章很远。

历史范围必须有 full_pair、L2、L1 或 explicit_gap 的覆盖说明，缺失则 `coverage_gap`。explicit_gap 表示已知缺口，不表示该剧情已被掌握。不能为缩短 prompt 把抽取失败的历史静默丢弃。

## 为什么不是反复“摘要的摘要”

需要压缩远处导航时，每次从保存的规范摘要重新生成，而不是接着上一次有损压缩继续压缩。规范原件、压缩结果及 loss 元信息分别保留；候选结果若 token 数没有更短则沿用原摘要。

导航压缩可采用约 400 字软目标，这不同于索引抽取时的 2500 字目标。缩短导航不等于删去全部实体和未决信息；知识工作集按自己的引用与候选规则保存。父级 summary_rollup 与“为了下次请求缩短导航”也不是同一步。

## 哪些操作可能调用模型

首次 `plan_next()` 可创建初始本地检查点，不代表已执行付费压缩。正常 run 在必要时物化已提交尾部并建立新检查点，这些步骤可能调用 indexer/compressor。显式 `job.checkpoint()` 也可能收费；没有下一目标时返回 None。

判断是否真的压缩、是否发生模型调用，要看 checkpoint 的依赖、压缩对象和请求账本，而不是只看是否存在 checkpoint ID。

## 触发后仍放不下怎么办

压缩先尝试配置目标比例，但不能保证达到它。必要原文、失败抽取留下的 full pairs、协议与输出预留合计仍超限时，返回 `minimum_context_does_not_fit` 等诊断。先检查超限来自参考正文、待物化历史、输出预留还是目标单章；不要先删数据库或直接截断当前原文。

索引容量和上下文压缩设置会影响冻结计划；需要改变模型容量、分块协议或参考材料时使用新任务。只增加修订次数不会使上下文变小。

## 代码位置

- `src/translation/sequence/context.py`：`build_checkpoint`、`compile_sequence_context`、`_compact`、`_parent_overview`、`_workset`。
- `src/translation/sequence/service.py`：`SequenceEngine.plan_next`、`checkpoint`，策略判断及切换。
- `src/translation/sequence/store.py`：`SequenceRepository.switch_checkpoint`，按 expected head 原子切换。
- `src/translation/sequence/knowledge.py`：`materialize_nodes`，检查点需要的历史抽取。
- `src/translation/budget.py`：实际消息计数和校准。
- 回归入口：`tests/translation/test_library_api.py`。
