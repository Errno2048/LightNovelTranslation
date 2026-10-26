# 分层翻译架构重构设计

状态：Draft  
范围：存储、索引、上下文、翻译、物化、审校、提交与并发调度  
目标读者：实现者、审校流程维护者、后续扩展层级/参考索引的开发者

> **Round 2 更新（2026-10-26）**：`pipelined` 模式已经允许审校滞后于翻译，并使用前驱未审 draft/provisional index 作为下游翻译和审校的 **provisional 证据**；这些内容仍然不是 accepted。本文中“审校只使用 accepted 前缀”的早期约束仅适用于 batch 模式。术语表已升级为 `term_entities`/`term_forms`（全名/短名/别名共享实体但各有 target），标题模式使用确定性模板归一化；`adjudication_mode="strict"` 下 unresolved major 进入 `manual_review`，翻译可继续，但 accepted/export 仍按连续前缀 gate。详见 [pipelined 计划](pipelined-translation-plan.md) 和 [使用说明](usage.md)。

## 1. 目标与非目标

### 1.1 目标

1. 把“章节正文、层级摘要、翻译位置、审校状态”拆成清晰、可独立演进的模块。
2. 支持 provisional 索引：翻译可以使用未审校摘要；batch 审校使用 accepted 前缀，pipelined 审校可以把前驱未审 draft/provisional 作为标注来源的 provisional 证据，但不得当作 accepted。
3. 支持跨窗口并发：翻译、物化、审校、提交可重叠执行，同时保持确定的先后约束。
4. 把块边界、上下文范围和阻塞条件写成可测试的纯函数与显式不变量。
5. 为未来更高层摘要（level 3+）预留扩展，但默认不创建未配置层级。
6. 复用现有内容寻址存储、抽取提示、审校/裁决、版本与并发基础设施。

### 1.2 非目标

1. 本阶段不实现单章拆分后的译文自动对齐，只预留数据结构与接口。
2. 本阶段不实现 level 3 及以上的实际物化；只支持配置驱动的层级识别。
3. 本阶段不改变“日文原文为事实权威”的原则。
4. 本阶段不把参考译文作为正文来源；参考索引只用于译名、实体和事件校准。

## 2. 术语与层级

### 2.1 层级编号

统一使用 `level` 表示索引层级，避免 L1/L2 命名歧义：

| level | 含义 | 子节点 | 默认最大子节点数 |
|---|---|---|---|
| 1 | 章节组块（原 L2） | Chapter / TranslationUnit | `max_level1_children = 10` |
| 2 | 一级块的父块（原 L1） | level-1 块 | `max_level2_children = 10` |
| 3+ | 更高层摘要 | level-2 块 | 未配置时禁止创建 |

说明：

- 当前只有 level 1、2 的默认配置；level 3 未配置时不得创建 level-3 块。
- “原 L1 / L2”只作为迁移期别名出现在文档和旧代码中，新代码统一使用 `level`。
- 索引块与章节都使用 `Artifact` 抽象，但两者的“接受”语义不同（见 4.3）。

### 2.2 关键对象

- `SourceChapter`：规范化后的日文章节，事实权威。
- `TranslationUnit`：实际翻译/审校单元；默认与 SourceChapter 一一对应，单章超限时可拆分。
- `ChapterArtifact`：某个 TranslationUnit 的译文版本。
- `BlockArtifact`：某层级的摘要与抽取结果。
- `IndexSnapshot`：某时刻的层级块、状态、指针与覆盖范围。
- `ContextPlan`：由快照、目标位置和操作类型生成的上下文、覆盖账本与 token 估算。

## 3. 参数与默认值

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `max_level1_children` | 10 | 一个 level-1 块最多包含的章节/翻译单元数 |
| `max_level2_children` | 10 | 一个 level-2 块最多包含的 level-1 块数 |
| `max_level3_children` | 未配置 | 未配置时禁止创建 level-3 块 |
| `context_num_full_chapters` | 5 | 上下文至少包含的最近已译章节数，向前对齐到 level-1 边界 |
| `context_num_blocks[]` | `[3]` 起 | 每层摘要至少包含的块数；数组下标 0 对应 level 1 |
| `context_max_tokens` | 模型上下文的保守上限 | 所有层级摘要 + 全文 + 当前批次日文的预算 |
| `translate_batch_size` | 30 | 初始批次大小；预检后可缩小 |
| `num_lookahead_batches` | -1 | 允许领先审校的批次数；`-1` 表示不按审校进度限制 |
| `batch_translation_block_level` | 1 | 下一批翻译前必须物化的最高层级；`level > 该值` 可异步 |
| `batch_review_block_level` | 所有已配置层级 | 下一批审校前必须审校的最高层级；保留作为可调参数 |
| `strict_level2` | false | level-2 是否优先使用全文严格抽取；默认使用二次摘要 |
| `max_unreviewed_batches` | 资源上限（建议显式配置） | 即使 lookahead=-1，也限制未审校批次/索引的积压量 |
| `reference_lazy_index` | true | 参考索引只规划边界、按需创建；目标 accepted 历史仍完整建索引 |
| `token_estimate_factor` | 1.15 | 直接与 rollup 估算共用的经验膨胀系数 |

参数约束：

- `max_levelN_children >= 1`。
- `context_num_blocks[level-1]` 是最小值，不是精确值；实际范围向前靠拢到父层边界。
- `context_num_blocks` 或 `max_level_children` 缺省时，使用当前已配置的最高层值填充；若完全没有配置，使用 level-1 的值。该回退只用于缺省项，不会凭空创建未配置层级。
- 层级必须连续；若 level 2 未配置，则 level 3+ 也不得创建。
- 未配置的层级既不创建块，也不进入上下文；不得用“缺失”伪装成“已覆盖”。
- `batch_translation_block_level` 必须小于等于已配置的最高层级；默认 1。
- `num_lookahead_batches` 只允许 `-1` 或非负整数；`-1` 表示不按审校进度阻塞，但仍受资源上限约束。
- 当前阶段每个层的 `max_levelN_children` 与 `context_num_blocks[level-1]` 可由 agent 在创建任务前决定，并记录在冻结快照中。
- `not_created` 惰性边界只用于参考索引；目标分支已 accepted 的章节必须保持完整索引（设计目标；当前 `TranslationRunner` 尚未自动物化目标 level-1 块，见 §21）。
- direct-first 只在真实 context 越界时触发 fallback；不再单设 0.75 阈值，由 `token_estimate_factor` 吸收保守性。

## 4. 核心数据模型

### 4.1 SourceChapter 与 TranslationUnit

```text
SourceChapter:
  source_id
  order                 # 原文中的阅读位置
  title
  text
  paragraph_ids[]
  paragraph_texts[]
  token_estimate
  match_key

TranslationUnit:
  unit_id
  parent_chapter_id     # 单章拆分时指向 SourceChapter
  part_index
  source_range          # 段落/偏移范围
  paragraph_ids[]
  token_estimate
```

单章拆分只影响 TranslationUnit，不改变 SourceChapter 的数量、顺序和最终导出结构。译文对齐逻辑暂不实现，但字段必须保留。

### 4.2 Artifact

```text
Artifact:
  artifact_id
  kind                  # chapter | block
  level                 # chapter 为 0；块为 1..N
  parent_id             # 块所属父块；章节为 null
  source_ids[]          # 覆盖的原始章节 / 子块
  inputs[]              # (artifact_id/version_id, hash) 或 (source_id, version_id, text_hash)
  status
  content               # text | summary | records
  generation
  revision
  review_id
  supersedes
  created_at / accepted_at
```

### 4.3 状态机

ChapterArtifact：

```text
planned
  -> translated          # 有草稿，未审校
  -> reviewing
  -> accepted            # 已审校，正文权威
  -> revision_required   # 审校要求修订
  -> translated          # 新版本
  -> blocked             # 重试/预算/协议失败
```

BlockArtifact：

```text
planned
  -> provisional         # 由 staged 译文生成，仅用于翻译上下文
  -> rebuild_required    # 所属章节全部 accepted，旧 provisional 作废
  -> reviewing
  -> accepted            # 摘要/抽取基于 accepted 输入
  -> stale               # 输入章节后续修订
  -> rebuild_required
```

关键区别：

- Chapter accepted 表示正文事实；Block accepted 表示派生索引事实。
- Block 可以被重建；Chapter 只能通过新版本修订。
- 块审校不得提出正文修订；正文问题转成 Chapter finding。

### 4.4 不变量

1. `Chapter.accepted` 的正文是最终事实来源。
2. `Block.accepted` 蕴含其所有 `inputs` 仍为当前 accepted 版本。
3. `Block.provisional` 只能用于翻译上下文，不能直接进入块审校。
4. 块审校前必须从 accepted 章节重建该块。
5. 块审校只使用 accepted 前缀。章节审校在 batch 模式使用 accepted 前缀；pipelined 模式可以把前驱未审 draft/provisional 作为明确标注的 provisional 证据，但不得当作 accepted。
6. 未配置的层级不得创建，也不得被上下文标记为已覆盖。
7. 指针单调推进；指针更新与 outbox 事件在同一事务提交。
8. 批次超限时缩小 batch，不截断最近章节前缀。

### 4.5 not_created 参考块

参考索引允许只规划边界而不立即创建内容：

```text
PlannedBlock:
  block_id, level, start, end, source_ids
  estimated_tokens, estimate_version
  status = not_created
  parent_block_id
  source_kind = reference
```

- `not_created` 不是完整 Artifact：没有 content/inputs/review_id。
- 目标分支 accepted 章节不使用 `not_created`；它们按上下文需要完整建索引。
- 上下文规划器把 `not_created` 视为不可用覆盖；不得把计划边界当作已覆盖。
- 低层 `not_created` 时优先使用覆盖该范围的更高层 accepted/provisional 摘要。
- 如果不存在更高层摘要，必须先为包含该范围的高层块懒创建全部低层子块，再 rollup/使用；这是阻塞条件，后续可优化为按需子集（TODO）。
- 高层 direct 创建成功后，低层子块可以继续保持 `not_created`；若后续需要增量重建，再按上述规则补齐子块。

## 5. 索引规划与物化

### 5.1 块边界

level-1 边界由实际翻译后的章节 token 估算决定：

1. 维护 `pending_chapters`：已翻译但尚未纳入 level-1 块的章节。
2. 每翻译完一章，尝试加入 pending。
3. 若 `len(pending) == max_level1_children`，或 pending 的双语 token 估算超过 level-1 物化上下文容量，则创建能容纳的最大前缀块。
4. 剩余章节继续 pending。
5. 批次结束时，不强制创建不足容量的块；边界决策只处理已经满足条件的块，除非用户显式要求 seal。

level-2 边界：

1. 以 level-1 块为子节点。
2. 严格模式：所有子块对应章节全文能放入 level-2 抽取上下文。
3. 二次摘要模式：使用子块摘要/抽取结果做 rollup，达到 `max_level2_children` 或预算触发。
4. 默认使用二次摘要；严格模式仅在小范围或显式配置时使用。

level-3+：

- 只有配置了 `max_levelN_children` 才允许创建。
- 未配置时，level-2 不应等待 level-3；上下文也不应引用不存在的层级。

### 5.2 边界决策与抽取分离

- 边界决策：确定 `[start, end)`、层级、子节点；必须快速、确定性、可持久化。
- 抽取：LLM 生成 summary/records；可以异步、失败重试。
- `boundary_pointer` 只表示边界已决策；`materialized_pointer[level]` 表示抽取已完成。
- 下一批翻译的门槛由 `batch_translation_block_level` 决定：
  - 默认只要求 `level <= 1` 的块已物化；
  - `level > 1` 的块可以异步，不阻塞最近窗口翻译。
- 如果 level-1 抽取未完成，下一批不得开始翻译；不得用全文静默替代，除非显式配置降级策略。

### 5.3 Provisional 与 Rebuild

1. 章节翻译完成后，按当前待物化章节生成 provisional level-1 块。
2. provisional 块用于下一批翻译上下文。
3. 当该块所有章节都 accepted：
   - 旧 provisional 块标记为 `rebuild_required`；
   - 用 accepted 章节重新抽取；
   - 当前最小版本：重建成功后直接标记 accepted，`review_mode='rebuild_only'`，并记录 TODO；
   - 重建失败时保持 `rebuild_required` / `failed`，不得用旧 provisional 摘要替代。
4. 旧 provisional 块不参与块审校，也不进入最终导出。
5. 如果章节在块 accepted 之后再次修订：
   - 该块标记 `stale`；
   - 重新 rebuild/review；
   - 依赖该块的更高级块也标记 `stale`。

## 6. 上下文构造

### 6.1 组成

对于批次 `[B_start, B_end)` 中的目标章节：

```text
[level N 摘要 ...]
[level 2 摘要 ...]
[level 1 摘要 ...]
[最近已译章节全文 ...]
[当前批次前驱日文 ...]
[待翻译正文]
```

范围规则：

1. **全文范围**
   - 从目标位置向前数 `context_num_full_chapters` 个已译章节；
   - 向前对齐到 level-1 块起始边界；
   - 实际可能多于 5 章。
2. **level-1 摘要**
   - 从全文范围起点之前的最后一个 level-1 块开始；
   - 向前至少 `context_num_level1` 个块；
   - 继续向前靠拢到最近的 level-2 边界，包含该边界到全文起点之间的所有 level-1 块。
3. **level-2 摘要**
   - 从 level-1 范围之前的最后一个 level-2 块开始；
   - 至少 `context_num_level2` 个，或按预算包含到更高层边界；
   - 若配置了 level 3，则对齐到 level-3 边界；否则按总预算向前扩展。
4. **更高层**
   - 只使用已配置且已物化的层级；
   - 缺失层级不生成占位摘要。
5. **当前批次前驱日文**
   - 对批次内第 i 章，加入 `B_start .. i-1` 的日文原文；
   - 用于保持同批次内尚未翻译章节的上下文。
6. **当前正文**
   - 最后一层是待翻译章节的日文源文。

`not_created` 回退规则：

- 低层块未创建时，优先使用覆盖同一范围的更高层 accepted/provisional 摘要。
- 如果不存在可用更高层摘要，则先为包含该范围的高层块懒创建全部低层子块，再 rollup；不得只创建所需子集而留下覆盖缺口。
- 只有配置了产出层（当前手写）后才允许创建对应层块；未配置层不生成占位摘要。

### 6.2 覆盖账本

ContextPlan 必须返回：

```text
coverage: [
  {level, start, end, representation, status, source_ids, token_estimate}
]
total_tokens
remaining_tokens
truncated: false
warnings: []
```

要求：

- 每个章节最多被一种表示覆盖（全文/摘要/当前正文）；不得同时出现全文和其摘要。
- provisional 块必须在 coverage 中标记 `status=provisional`。
- 如果预算不足，先缩小 batch；不允许截断最近章节全文。
- 如果连单章都放不下，进入 TranslationUnit 拆分流程（TODO）。

### 6.3 批次预检

```text
candidate = translate_batch_size
while candidate >= 1:
    plan = build_context(... candidate ...)
    if plan.fits: use candidate
    candidate -= 1
```

优化：

- 优先在 level-1 块边界处截断 candidate，减少跨块批次。
- 预检只做本地 token 估算，不调用模型。
- 预检结果记录到批次元数据，便于复现与审计。

### 6.4 专有名词预抽取

- 在批次翻译前，使用批次日文、已接受 glossary、最近上下文抽取专有名词。
- 只允许沿用已接受译名；新词必须有本批次日文证据。
- 参考索引可以提供候选，但不得覆盖日文原文事实。

## 7. 审校与提交

### 7.1 章节审校

- 输入：待审章节版本 + accepted 前缀；pipelined 模式还可包含前驱未审 draft/provisional index，必须标注 `provisional`，不得当作 accepted。
- 输出：findings、adjudication、可选的 revision。
- 审校通过后 Chapter 进入 accepted；正文成为权威。strict 模式下 unresolved major 进入 `manual_review`，由人工接受/修订/阻塞。
- 章节修订创建新版本，旧版本保留；依赖块标记 stale。人工修改 provisional 术语时，下游 ready/draft 需要重审或重译。

### 7.2 块审校（当前最小版本）

- 触发条件：该块所有子节点（章节或子块）已 accepted。
- 审校前必须从 accepted 输入重建块；不得直接审校 provisional 块。
- 当前版本**不调用独立块审校模型**：
  - 重建成功即标记 accepted；
  - 记录 `review_mode='rebuild_only'`、`quality_labels=['rebuild_only']`；
  - 记录 TODO：后续补块级 summary/records 审校与重新抽取协议。
- 重建失败时块保持 `rebuild_required` / `failed`；level <= 1 的失败按翻译阻塞规则处理，level > 1 可异步重试。
- 未来块审校只能修正 summary/records/entities/events，或请求重新抽取；不得提出正文修订。
- 正文问题必须生成 Chapter finding，回到章节审校流程。
- 块 accepted 后记录 `inputs`、`review_mode` 与 `review_id`（rebuild_only 时 `review_id` 为空）。

### 7.3 审校阻塞

> 以下规则描述 **batch 模式**。`pipelined` 模式下翻译只需前驱 provisional index 已物化；审校可以滞后，accepted/export 才按连续前缀阻塞。strict 的人工复核 hold 不阻止翻译。

批次 B 的审校开始前：

1. 批次 A（前驱批次）内所有章节已 accepted；
2. 所有 `right_endpoint <= A_end` 的已创建块，层级不超过 `batch_review_block_level`，已 accepted。

说明：

- 默认 `batch_review_block_level` 为所有已配置层级。
- 跨窗口块（右端点在 B 内）不阻塞 A/B 的审校，只阻塞后续批次。
- 该规则不产生环：所有依赖都指向更早的右端点。
- 若未来放宽审校阻塞层级，必须重新证明不会把 provisional 块作为审校证据。

### 7.4 提交/接受语义

- Chapter：`accepted` 即正文提交；最终导出只读取 accepted 章节。
- Block：`accepted` 是索引接受，不是正文提交；不进入最终 EPUB 正文。
- 最终导出必须校验：
  - 章节为 accepted；
  - 章节版本与导出内容 hash 一致；
  - 不包含 provisional/stale 块内容。

## 8. 并发、指针与调度

### 8.1 指针集合

```text
translated_through       # 已翻译到的位置
boundary_through         # 块边界已决策到的位置
materialized_through[L]  # 各层抽取已完成到的位置
reviewed_through         # 章节审校已完成到的位置
accepted_through[L]      # 各层块已接受到的位置
```

要求：

- 每个指针单调递增。
- 不同阶段使用不同指针，减少争用。
- 指针单位统一为“章节位置”；块右端点用于阻塞判断。

### 8.2 指针推进协议

采用“短事务 + CAS + outbox”：

```text
1. 读取当前状态，计算最远可推进位置；
2. 在事务外完成所有纯计算；
3. BEGIN IMMEDIATE；
4. 重新校验指针未变化；
5. 为跨过的每个边界写入块决策事件（幂等 key）；
6. 更新指针；
7. COMMIT；
8. 失败则回滚并重试。
```

约束：

- 不得在持有写锁时调用 LLM。
- 指针跳过多个边界时，必须为每个跨过的边界发出事件，不能只推进指针。
- 遇到未翻译/失败章节时，指针停在该章节之前，不得跳过。
- 抽取完成事件可以乱序，但 `materialized_through[L]` 只能按序推进。
- 块 key 建议为 `(level, start, end, inputs_hash)`，唯一约束 + `INSERT OR IGNORE`。
- 事件消费者必须幂等；重复事件不得重复建块或重复计费。

### 8.3 任务 DAG

```text
translate(chapter/unit)
  -> boundary_decide(level 1..N)
  -> materialize(block, level 1..N)
  -> review(chapter)
  -> accept(chapter)
  -> rebuild(block)
  -> review(block)
  -> accept(block)
```

依赖：

- `translate(B)` 依赖 `materialize(level <= batch_translation_block_level, right_endpoint <= A_end)`。
- `review(B)` 依赖 `accept(chapters in A)` 与 `accept(blocks, right_endpoint <= A_end, level <= batch_review_block_level)`。
- `num_lookahead_batches >= 0` 时，`translate(batch i)` 允许领先审校该数量的批次，超过则等待。
- `num_lookahead_batches = -1` 时，不按审校进度等待；仍必须满足 level <= `batch_translation_block_level` 的物化门槛，并受资源上限约束。
- `materialize(block)` 依赖其子节点已 translated（provisional）或 accepted（rebuild）。
- `review(block)` 依赖所有子节点 accepted。
- `rebuild(block)` 依赖所有子节点 accepted。

### 8.4 队列与配额

建议按阶段划分队列：

1. review/accept（最高优先级）
2. materialize/rebuild
3. accept chapters
4. speculative translate（最低优先级）

全局模型门控：

- 同时限制请求数与 in-flight token 数；
- 大上下文请求按 token 加权；
- 审校/提交优先于投机翻译；
- 物化不能无限滞后，否则 level-1 ready 门槛会阻塞翻译。

### 8.5 批次缩小

- 预检失败时只缩小 batch，不截断前缀。
- 缩小后重新计算上下文和块边界。
- batch 边界优先落在 level-1 块边界。
- 如果单章仍超限，拆分 TranslationUnit；拆分暂不自动对齐译文，标记 TODO 并阻止提交直到人工/后续流程处理。

### 8.6 调度算法

推荐实现：**持久化任务表 + 分层优先级 DAG 调度器 + 全局加权模型门控**。先使用线程池即可，现有 provider 是同步 httpx；后续再考虑 asyncio/多进程。

#### 任务表

```text
Task:
  task_id
  kind            # translate | boundary | materialize | rebuild | review | accept | export
  level           # 块层级；章节任务为 0
  batch_id / source_id / block_id
  priority
  state           # pending | ready | running | done | failed | blocked
  dependencies[]  # 或 successor counters
  idempotency_key
  attempts
  not_before
  payload
```

- 依赖边只指向更早的批次/块，DAG 无环。
- 任务完成时在同一事务中更新状态并递减后继计数器；新就绪任务入队。
- 崩溃恢复时把 `running` 任务标记为 `unknown_remote_result` 或按幂等键重试；重建 ready 队列。

#### 调度循环

```text
while running:
    ready = fetch_ready_tasks(limit)
    selected = fair_select(ready, stage_quotas, gate_capacity)
    for task in selected:
        submit(task.stage_pool, task)
    wait_for_completion_or_new_ready()
```

`fair_select` 建议使用**按阶段配额的 deficit round-robin**：

- 默认权重：review/accept 40%，materialize/rebuild 30%，translate 30%。
- 同阶段内按批次/位置 FIFO；长时间等待的 review 任务获得 aging 加权，避免饿死。
- 全局门控同时限制请求数和 in-flight token 数；大上下文请求按 token 加权，而不是只数请求。
- 某个阶段没有可运行任务时，其配额可临时借给其他阶段。

#### `num_lookahead_batches = -1` 的资源背压

- 不按审校进度阻塞翻译，但仍设置 `max_unreviewed_batches`、最大 provisional 块数、最大 in-flight token 数和磁盘/队列上限。
- 达到资源上限时，暂停新的翻译任务，优先让审校/物化消费积压。
- 建议保留一个“draft-first”模式：
  - 翻译尽快产出全本草稿；
  - 审校在后台按批次推进；
  - 当审校积压超过高水位时，降低翻译权重，而不是完全停止。
- 需要单独的 `draft` 导出模式：
  - `accepted` 导出只包含已审校章节；
  - `draft` 导出包含已翻译但未审校章节，并明确标记质量状态。
- 质量风险：无限 lookahead 会让 provisional 误差链更长；至少记录 `provisional_depth`、审校积压和修订影响范围，作为后续策略输入。

#### 死锁与公平性

- 不在持有数据库写锁时调用模型。
- blocked 任务释放 worker，持久化状态后由重试/恢复流程重新入队。
- 依赖都指向更早的右端点；调度器启动时校验 DAG 无环。
- review/accept 的优先级不能无限压过 translate，否则全本草稿无法推进；使用配额而非绝对优先级。

## 9. 修订传播

### 9.1 章节修订

```text
chapter revision
  -> new ChapterArtifact version
  -> dependent block inputs hash changes
  -> blocks with old inputs => stale
  -> higher-level blocks => stale
  -> affected blocks rebuild from accepted chapters
  -> block review re-run
```

### 9.2 Provisional 块

- provisional 块可以服务 pipelined 下游翻译和审校，始终带 `provisional` 标记，不得进入 accepted/export。
- 当章节 accepted 后，accepted target summary 可以覆盖同范围 provisional 块；provisional 记录保留但不再被 snapshot 选中。
- 已用 provisional 块翻译的后续章节，由章节审校或人工复核发现问题；如果人工/审校改变正文或 provisional canonical，下游 `ready`/draft 需要标记 stale 并重审/重译，不能默认沿用旧结果。

### 9.3 块内容修订

- 块审校只修改 summary/records，不修改正文。
- 已 accepted 块的 summary 变化不会回溯修改已完成译文；如需影响后续翻译，从下一批次生效。
- 如果未来允许块审校提出正文修订，必须重新引入层级回滚协议；当前明确禁止。

## 10. 非匹配参考索引

### 10.1 原则

- 日文目标版本是事实权威。
- 参考译文只用于译名、实体、事件和续译位置校准。
- 参考事实不得进入 accepted knowledge，也不得覆盖日文原文。

### 10.2 匹配

- 默认 level-1 粒度匹配。
- 匹配结果记录：
  ```text
  ReferenceMatch:
    target_block_id
    reference_block_ids[]
    confidence
    method
    unmatched_ranges[]
  ```
- 支持一对多、多对一和未匹配。
- 实体冲突时以日文为准；参考译名只作为候选，不自动成为 accepted glossary。
- 术语实体和表面形式分层：`term_entities` 保存实体身份，`term_forms` 保存每个 source form 的 target；全名/短名可以共享实体但 target 独立。不要用单个 source-target 行表示整个实体。
- 参考索引块使用独立命名空间与状态，不参与目标正文导出。

### 10.3 与现有 cross_edition 能力对齐

现有技能已经提供了可复用的基础能力，新架构应直接复用而不是重新设计：

- `cross_edition` 参考模式：两侧分别构建独立 Edition 索引；中文作为独立版本参考。
- `project.index()` 做本地规划；`index.build()` / `build_parent()` 构建选定范围的索引。
- `index.associate(ja_node_ids, zh_node_ids)`：在 L2 粒度做区块关联，允许非单调、多对多、无对应。
- `match_entities(ja_parent, zh_parents)`：实体候选匹配；结果仍需日文证据校验。
- `index.prepare_references(source_chapters, reference_chapters, workers, batch_size)`：局部建索引、关联、实体匹配的组合入口。
- `index.resolve_terminology(...)`：对未建索引的中文正文做有界术语补查。

设计约束：

- 关联结果是**候选证据**，不证明剧情等价；没有匹配时仍按日文翻译。
- 参考版剧情差异不构成 Web 错译；Web 事实优先。
- `translation.alignment` 中的 `rules`、`local_sequence_align`、`semantic_window` 是底层对齐工具，可用于生成候选；不应直接当作权威一一映射。
- 参考索引的匹配粒度为 level-1（现 L2），除非未来显式配置更高层关联。
- 当前只支持一个已整理的中文参考版本；未提供的卷及其摘要不得混入上下文。

### 10.4 参考索引的惰性物化

参考索引默认使用 `reference_lazy_index=true`：

1. 先按章节目录和 token 估算规划所有层级边界，状态为 `not_created`。
2. 只创建当前实际需要的最高层块：
   - 估算值乘 `token_estimate_factor=1.15`；
   - direct 与 rollup 使用不同的估算公式；
   - direct 失败仅在真实 context 越界时触发 rollup，不设单独 0.75 阈值。
3. direct 成功后，低层 `not_created` 块保持未创建；只有后续确实需要低层时才懒创建。
4. 需要的低层块缺失且没有更高层摘要时，先创建包含该范围的高层父块的全部低层子块，再 rollup。
5. 超限块按章节边界拆分；单章仍超限时进入 TranslationUnit split fallback。
6. 参考索引只提供候选证据，不进入目标 accepted knowledge。
7. direct 块在当前场景下全部来自 accepted 参考章节，因此默认可直接 accepted；为兼容保留 provisional 分支。
8. 记录 direct 尝试、fallback、split、estimate/actual 偏差等指标，用于校准 1.15 系数。

## 11. 失败、恢复与幂等

### 11.1 失败分类

| 失败 | 行为 |
|---|---|
| 章节翻译失败 | 指针停住；按策略重试；超过上限标记 blocked |
| level-1 抽取失败 | 批次翻译不得越过 level-1 ready 门槛；显式失败而非静默降级 |
| level>1 抽取失败 | 允许异步重试；不阻塞最近批次翻译 |
| 章节审校失败 | 重试；batch 超限 blocked；strict pipelined 进入 manual_review 并继续翻译后续批次 |
| 块审校失败 | 重试；块保持 rebuild_required |
| 预算超限 | 停止新任务；保留已提交结果；支持显式扩展 ledger 上限 |

### 11.2 崩溃恢复

- 指针、块决策事件、块内容、审校结果均持久化。
- 指针更新与 outbox 同事务。
- 启动时：
  - 将孤立的 `running` reservation 标记为 `unknown_remote_result`；
  - 重放未消费 outbox；
  - 校验 accepted 块的 inputs 仍有效；
  - 将不再有效的块标记 stale。

## 12. 可观测性

至少记录：

- 每阶段耗时、队列深度、in-flight tokens；
- 每个批次的 ContextPlan 覆盖账本与 token 构成；
- provisional 使用次数、rebuild 次数、stale 次数；
- review findings 严重度、confirmed/dismissed、fallback 率；
- batch 缩小次数、单章拆分次数；
- direct 尝试次数、context 越界 fallback 率、rollup 触发率；
- estimate/actual token 偏差、1.15 系数的实际校准值；
- level 块 planned/not_created/created/accepted 数量；
- lazy 低层补齐次数、父块全子块创建次数；
- 模型请求/缓存命中/计费账本。

## 13. 模块划分与复用

### 13.1 建议模块

```text
storage/
  chapter_store.py      # SourceChapter / TranslationUnit
  artifact_store.py     # Artifact / IndexSnapshot / generation
index/
  levels.py             # level 配置与泛化
  planner.py            # 块边界规划
  materializer.py       # level-1/2/3 物化
  provisional.py        # provisional 块
context/
  builder.py            # 纯函数 ContextPlan
  budget.py             # token 估算与 batch 预检
execution/
  scheduler.py          # DAG 调度、队列、配额
  pointers.py           # CAS + outbox 指针协议
  pipeline.py           # 批次/窗口编排
review/
  chapter_review.py     # 章节审校
  block_review.py       # 块审校
  reference_match.py    # 参考索引匹配
```

### 13.2 可复用的现有能力

- `NarrativeStore`：内容寻址、WAL、只读连接池、缓存、显式 verify。
- `plan_adaptive_index` / `AdaptiveIndexSpec`：level-1/level-2 规划。
- `joint_messages`、`parser`、`ExtractionRecovery`：L2 抽取。
- `_parent_overview`、`_materialize_parent_rollup`：level-2 二次摘要。
- `ReviewSession`、`review/`：审校、裁决、修订、severity。
- `create_version`、`commit_chapter`、attempts/outbox：版本、提交、幂等与恢复。
- `run_pipeline_windows`、`ModelGate`：批次并发、lookahead、资源门控。
- `cross_edition` / reference packets：非匹配参考索引基础。

## 14. 迁移计划

### Phase 1：数据模型与指针

- 引入 Artifact/level/IndexSnapshot/指针抽象，与现有 sequence 对象并存。
- 双读校验：新旧上下文覆盖、块范围、token 估算一致。
- 不切换执行路径。

### Phase 2：ContextBuilder 纯函数

- 把现有 `compile_sequence_context` 的覆盖逻辑抽成 ContextPlan。
- 增加边界矩阵测试（起始位置在块中间、跨 L1、partial block、缺失 level 3）。
- 用 golden 测试对比新旧上下文。

### Phase 3：provisional/rebuild 生命周期

- 块 provisional → rebuild → review → accepted。
- 章节修订触发 stale 传播。
- 批次翻译使用 provisional，审校使用 accepted。

### Phase 4：DAG 调度与指针协议

- CAS + outbox 指针推进。
- 阶段队列、优先级、模型门控、batch 预检/缩小。
- 将现有 `run_pipeline_windows` 迁移为调度器的一个执行器。

### Phase 5：参考索引与高层扩展

- level-1 粒度参考匹配。
- level 3 配置与创建；未配置时保持禁用。
- 清理旧 L1/L2 命名与兼容路径。

## 15. 测试策略

### 15.1 单元测试

- 块边界：10 章上限、token 容量、partial block、跨父块。
- 上下文：起始位置在块中间、最少全文/摘要数、向父边界靠拢、预算不足时缩小 batch。
- 指针：CAS 冲突、跳过多边界、事件幂等、失败章节停住。
- 状态机：provisional → rebuild → reviewing → accepted → stale。
- 参考匹配：一对多、多对一、未匹配、实体冲突。

### 15.2 Golden 测试

- 使用固定日文/中文样本，断言 ContextPlan 的 coverage 与 token 估算。
- 断言 provisional 块不会进入审校证据。
- 断言最终导出只包含 accepted 章节。

### 15.3 并发测试

- 多线程同时推进指针，验证无丢失更新、无重复块。
- 抽取乱序完成，验证 materialized 指针单调。
- 崩溃恢复：指针已推进但事件未消费、事件已消费但指针未推进。
- 30 翻译 + 30 审校的压力测试，验证模型门控与内存上限。

### 15.4 集成测试

- 90 章、3 批次的端到端运行；分别测试 `-1` 草稿模式和有限 lookahead。
- 质量指标：major/minor findings、fallback、unresolved、术语一致性、审校积压与 provisional 深度。

## 16. 已决定事项与待定事项

### 16.1 已决定

- level 1 = 章节组块（原 L2），level 2 = 原 L1，level 3+ 预留。
- `max_level1_children = 10`，`max_level2_children = 10`；未配置高层禁止创建。
- `batch_translation_block_level = 1`：level <= 1 必须物化，level > 1 可异步。
- `batch_review_block_level` 暂保持“所有已配置层级”，但保留为可调参数。
- `num_lookahead_batches` 默认 `-1`：不按审校进度阻塞翻译，先产出全本草稿；仍受资源上限和 level <= 1 物化门槛约束。
- `max_level_children[]` 与 `context_num_blocks[]` 数组化；缺省项回退到当前最高已配置层的值；当前阶段由 agent 决定分配并记录在冻结快照中。
- 单章拆分最小版本按行数对齐；译文对齐 TODO，对齐失败必须阻止提交。
- 块审校当前为最小版本：从 accepted 输入重建块，成功即 accepted，`review_mode='rebuild_only'`；独立块审校 TODO。
- 块审校不得提出正文修订。
- batch 审校使用 accepted 前缀；pipelined 审校可使用标注过的前驱 draft/provisional；翻译可使用 provisional。strict 的 unresolved major 进入 manual review，翻译可继续。
- 超限时缩小 batch，不截断最近章节前缀。
- 参考索引默认 level-1 粒度，复用 `cross_edition` 的 L2 关联、实体匹配和术语补查；日文为事实权威。
- 调度器采用持久化任务表 + 分层优先级 DAG + 全局加权模型门控；先线程池，后评估 asyncio。

### 16.2 待定

1. `num_lookahead_batches=-1` 时 `max_unreviewed_batches` 的默认值和资源高水位。
2. `draft` 导出是否作为正式产物，以及其质量标记/验收标准。
3. `rebuild_only` 自动 accepted 块的质量门槛、指标和后续补审计划。
4. 单章拆分的行数对齐在行数不一致、空行、章节标题、插图等情况下的具体规则。
5. 调度器的阶段配额、aging 参数和高水位默认值。
6. 参考索引匹配的置信度阈值、未匹配块的处理，以及实体冲突的记录格式。
7. 更高层摘要的上下文预算分配策略；当前先由 agent 决定。

### 16.3 决策记录（采用推荐默认）

以下默认值先落地，后续用实测调整：

1. `max_unreviewed_batches` 默认 `8`。`-1` 表示真正不限制，但不作为默认；资源高水位优先保证质量与内存可控。
2. `draft` 导出作为独立产物：包含已翻译未审校章节，明确标记质量状态；与 accepted 导出严格分离，不混入最终 EPUB。
3. `rebuild_only` 块可以进入翻译上下文和阻塞判定，但必须带 `quality_labels=['rebuild_only']`；记录待补审数量，后续安排块级补审。
4. 无限 lookahead 下章节失败策略：
   - 严格模式：停在失败章节；
   - 草稿模式：允许跳过并标记 `draft_gap`，但不得进入 accepted 导出；最终接受前必须补齐。
5. 单章拆分优先段落对齐，行数对齐作为最小实现；行数不一致、空行、标题、插图等情况直接 `blocked`，不做静默合并。
6. `max_level_children[]` 与 `context_num_blocks[]` 由 agent 在任务创建前决定，写入冻结快照；运行中不得改变；层级必须连续，缺省项回退到最高已配置层。
7. 调度器默认阶段权重：review/accept 40%，materialize/rebuild 30%，translate 30%；使用 aging；`max_unreviewed_batches=8` 作为默认高水位。
8. 参考匹配：未匹配不阻塞翻译；低置信匹配只进入候选，不进入 accepted glossary；实体冲突以日文为准并记录候选来源。
9. 更高层摘要：默认不创建 level 3；只有配置了 `max_level3_children` 才允许创建。每层的 `context_num_blocks` 与预算由 agent 在冻结快照中决定。

### 16.4 参考索引惰性物化决策（采用）

1. “已有译文”指 **cross_edition 参考译文**；目标分支在运行后按上下文需要完整建索引，不使用惰性 `not_created`。
2. `not_created` 是参考索引的 planned boundary，不是完整 Artifact。
3. token 估算初始系数 1.15；direct 与 rollup 使用两套估算公式。
4. direct-first 只在真实 context 越界时 fallback；不单设 0.75 阈值。
5. direct 块在当前场景下全部来自 accepted 参考章节，默认可直接 accepted；兼容 provisional。
6. 低层块懒创建；不需要查询的块不创建。
7. 超限块按章节边界拆分；单章仍超限时进入 TranslationUnit split fallback。
8. 上下文规划器优先使用更高层摘要；若低层块被真正请求且无高层可用，先创建该高层父块的全部低层子块，再 rollup（TODO：后续可改为按需子集）。
9. 增加 direct/fallback/split/estimate 偏差等 debug 指标日志。

## 17. 验收标准

1. 从任意起始位置（包括块中间）可构建确定性的 ContextPlan。
2. 批次超限时只缩小 batch；单章超限有明确的 blocked/TODO 行为。
3. provisional 块可被翻译使用，但不会进入块审校或最终导出。
4. 章节修订能正确使相关块 stale，并触发 rebuild；`rebuild_only` 块带明确质量标记。
5. 指针并发推进无丢失更新、无重复块；崩溃后可恢复。
6. level <= 1 物化阻断下一批翻译；level > 1 不阻断。
7. `num_lookahead_batches=-1` 时可在资源上限内产出草稿全本，审校后台推进，不产生无界积压。
8. 跨窗口 L1 不造成审校死锁。
9. 90 章、k=1、30/30 并发的端到端测试通过，且质量指标可观测。

## 18. 首次真实探针结果（2026-10-22）

测试数据：

- 日文 `転生したら剣でした.epub`：1359 个条目；
- 中文参考 `转生成为魔剑 Web卷.epub`：1310 个条目；
- 主章节按 `edition_order` 对齐；测试目标 `order=110`（日文“109 解体”，中文“第109章 解体”），批次 110–111。

结果：

1. **lazy 参考索引规划**：145 个 planned block（level 1 共 131，level 2 共 14），0 个 oversized。目标 level-2 块 0–100 估算 946,941 tokens（byte counter × 1.15）。
2. **direct 摘要**：成功；775 字摘要，11.8s，prompt 188,318 tokens，completion 1,464 tokens。
3. **ContextPlan**：估算 209,750 tokens，fits；覆盖为 level-2 摘要 0–100、全文 100–109、目标正文 110。
4. **任务 DAG**：`reference-l2 → translate-110 → translate-111`，由持久化 TaskStore 执行；`materialized` / `translated` 指针推进，指标已写入 `probe-metrics.jsonl`。
5. **真实翻译**：
   - order 110：13.5s，prompt 51,113，completion 2,356；
   - order 111：14.5s，prompt 54,054，completion 2,060，缓存命中 50,304；
   - 译文通顺度明显优于参考机翻，能保留“露西尔商会”“芙兰”等既有译名。

发现的问题：

1. **byte 估算过于保守**：估算 946,941，实际 prompt 188,318；即使包含 1.15 系数，仍约为实际的 5 倍。建议接入真实 tokenizer 或按实际 usage 校准，而不是长期使用 byte×1.15。
2. **术语一致性不足**：新译文使用“伊奥小姐”，参考译文使用“依尤桑”；L2 摘要本身不能强制既有译名。需要在翻译 prompt 注入 glossary/实体匹配候选。
3. **标题泄漏**：模型输出了“【109 解体】”标题行；翻译指令应明确只输出正文，或在后处理剥离标题。
4. 本次探针覆盖了 lazy 规划、direct 摘要、ContextPlan、TaskStore/指针和真实翻译；尚未覆盖章节审校、provisional rebuild、review/commit、90 章并发窗口。

后续：

- 用真实 tokenizer/usage 校准估算；
- 在 ContextPlan 渲染中注入已接受 glossary 与实体候选；
- 翻译 prompt 明确“只输出正文”；
- 接入审校/裁决与 provisional rebuild 后，再跑 90 章/全量窗口。

## 19. 旧实现上下文对比与可复用能力

旧 `sequence` 的上下文不是“L1/L2 + 全文”这么简单，而是由 checkpoint 冻结的一组消息和证据组成：

1. **系统提示**：`workflow.SYSTEM` + “摘要/实体/时序均为未审候选”。
2. **workset**：core、unresolved、extraction_gaps、实体卡片、按配额选出的 K 记录、线程卡片、catalog ID 与选择统计。
3. **历史层次导航**：L1/L2 entry（summary、state、semantic_status、build_method）以及 `detail_nodes` 的 built/not_built 状态。
4. **完整历史**：`select_context` 选出的 full pairs；无中文对应时使用 source-only 历史。
5. **近期已提交全文**：full-tail 的 source message + assistant 译文 turn。
6. **当前目标**：`source_message`，包含前章 unresolved、完整日文、STABLE aliases，并明确“只翻译本章、只输出正文、不输出段落 ID”。
7. **定向证据**：片假名/实体候选、局部原文窗口、历史首末次出现、中文候选片段、alignment_status、omitted 统计。
8. **预算与压缩**：`TokenCounter`/`plan_request`/calibration、soft input limit、H 策略短摘要、`minimum_context_does_not_fit` 诊断。
9. **审校上下文**：near chapters、targeted evidence、prior review contexts、hierarchical blocks（global_knowledge/few_shot/coarse_overview/child_detail/far/near block overview/detail/translation_metadata/cross_edition_reference）、前轮 findings、规则 signals、工具取证结果。

### 19.1 组件对比

| 信息 | 旧实现 | 新 ContextPlan（探针） | 处理建议 |
|---|---|---|---|
| L1/L2 摘要 | hierarchy entries + summary + detail built/not_built | block summaries，not_created 边界 | 保留；把 not_created 映射为 detail `not_built` |
| 近期全文 | full pairs / source-only / full-tail turns | full source+reference translation | 复用 `full_pair_message`/`source_message` |
| 当前目标 | aliases + 完整日文 + unresolved + 输出协议 | target source | 复用旧 source_message，避免标题泄漏 |
| workset | core/entity/K records/threads/gaps/selection | 无 | 必须补入；否则术语、未决和长线状态丢失 |
| 实体目录 | name/kind/description/target_form/ambiguity | L2 summary 中的名称 | 注入 entity cards 和 target_form，解决“伊奥/依尤” |
| 线程目录 | active/resolved/dormant + question/resolution/evidence | 无 | 补入；否则长期悬念和已解决状态丢失 |
| 定向证据 | 片假名/实体候选、局部窗口、历史命中、alignment | 无 | 复用 `build_targeted_evidence` |
| 未决/缺口 | unresolved + extraction_gaps | 无 | 补入；避免把“没有检索到”当成“没有信息” |
| token 估算 | TokenCounter/calibration/soft limit/overflow fallback | ByteCounter × 1.15 | 复用旧 budget 机制；最好接真实 tokenizer |
| 审校上下文 | coverage/checks/report/adjudication/tool evidence | 无 | 直接复用旧 review 链路 |
| 参考索引 | cross_edition prepare/associate/match_entities/resolve_terminology | lazy planned blocks + 自定义摘要 | 用旧 cross_edition 能力构建/校验参考块 |

### 19.2 探针问题对应的旧解法

1. **token 估算失真**：旧实现使用 `TokenCounter`（可接 tokenizer）、`plan_request`、`observe_usage` 校准、`input_soft_limit` 和 `is_context_overflow`。新实现应复用这套；byte×1.15 只应作为无 tokenizer 的保守 fallback。
2. **术语不一致**：旧 `_known_glossary`、entity catalog 的 `target_form`、workset 实体卡片、targeted evidence 都能提供既有译名。新 ContextPlan 需要增加 `entity/glossary` 段。
3. **标题泄漏**：旧 `source_message` 明确“只翻译本章、只输出译文正文，不输出段落定位 ID”。新翻译 prompt 应直接复用该协议。
4. **缺少审校/提交**：旧 `compile_review_context` + checks + adjudication + coverage 已覆盖语义绑定、段落覆盖、规则信号和工具取证；新架构应把 ContextPlan 作为 review context 的输入，而不是另写一套审校。
5. **参考索引能力**：旧 `cross_edition` 已有 L2 关联、实体匹配、术语补查和 allowed_uses 语义；lazy reference index 应调用这些能力，而不是只做本地摘要。

### 19.3 建议的上下文装配顺序

新 ContextPlan 的 `segments` 之后应增加一个 `ContextAssembler`。为复用 provider 的前缀缓存，必须按“全章稳定 → 逐章变化”排序：

1. system + 未审候选声明；
2. 稳定 workset（core、gaps、按首次出现顺序的 entity cards）；
3. 层级导航（L1/L2 + not_built 详情）；
4. 参考/目标全文或摘要 segments；同批所有目标使用同一组历史段；
5. 本章动态信息（unresolved、threads、target/batch selection）；
6. 定向证据；
7. 当前目标 `source_message`（只输出正文，输出协议随目标变化）；
8. 可选的 review/checks/evidence 上下文，草稿放在最后。

逐章变化字段若出现在第 2–4 步，后面的长上下文全部无法命中缓存。术语表应按首次出现顺序追加，不要按频次或字形重排。

Token 预算应由旧 `plan_request` 统一计算；workset/实体/线程/定向证据都应有明确配额，超出时按旧 `selection` 的 omitted 统计记录，而不是静默丢弃。

### 19.4 旧实现仍然不如新架构的地方

- 旧 checkpoint 是冻结消息列表，状态和覆盖依赖不够显式；新 IndexSnapshot/Artifact/指针更清晰。
- 旧 provisional 语义没有 `not_created`/planned boundary/父块全子块补齐规则；新 lazy reference 设计更明确。
- 旧物化/审校阻塞靠 batch 串行；新 DAG/指针可以更细粒度并发。
- 旧参考索引没有“只规划最高层、低层懒创建”的成本策略；新 lazy 规划补上了这一点。

结论：新架构应保留旧的**上下文内容和证据协议**，只替换**调度、快照、指针和惰性物化**。不要为了新 ContextPlan 丢弃 workset/实体/线程/定向证据/审校上下文。

## 20. 融合探针结果（2026-10-22）

测试目标仍为 order 110/111；在新 ContextPlan/TaskStore 之上加入：

- 旧式 workset：core、实体卡片、unresolved、extraction_gaps、active threads；
- 实体 `target_form` / ambiguity（手工候选）；
- 定向证据：片假名/术语出现位置、参考译文命中；
- `source_message` 输出协议：“只输出正文，不输出标题/段落 ID”；
- `plan_request` + TokenCounter 预算；
- lazy level-2 参考摘要（1059 字，已缓存）。

结果：

| 项目 | 未融合探针 | 融合探针 |
|---|---|---|
| 标题泄漏 | 有“【109 解体】” | 无 |
| order 110 prompt | 51,113 | 55,244 |
| order 110 completion | 2,356 | 2,068 |
| order 111 prompt | 54,054 | 66,763 |
| order 111 completion | 2,060 | 4,518 |
| 术语 | “伊奥”“阿曼达”“金猪” | “依尤”“艾曼达”“黄金猪” |
| 其他术语 | 部分未命中 | “芙兰”“乌鲁希”“巴尔博拉孤儿院”“露西尔商会”“阿匹斯”“格林卡姆比” |

上下文估算：

- order 110：226,697 estimated / 238,032 safe，fits；
- order 111：274,383 estimated / 288,103 safe，fits；
- 实际 prompt：55,244 / 66,763，byte 估算仍约为实际的 4 倍。

结论：

1. **融合有效**：实体 `target_form` + workset + source_message 解决了术语漂移和标题泄漏。
2. **旧协议应作为 ContextPlan 的装配层**，而不是被新 ContextPlan 替换。
3. **token 估算仍需校准**：`plan_request` 的预算结构可复用，但 byte counter 的 1.15 系数过于保守；应接真实 tokenizer 或用实际 usage 校准。
4. 本次实体/线程/未决仍是手工候选；生产环境应复用旧 entity catalog、thread catalog 和 cross_edition 的 `prepare_references/associate/match_entities`。
5. 尚未融合章节审校、provisional rebuild、review/commit；下一步应把 ContextAssembler 接入旧 review 链路，再跑 90 章窗口。

## 21. main 分支清理与当前实现状态

已执行：

- 旧代码最后提交 `92a2645` 归档到 `legacy-sequence` 分支并推送到 origin。
- main 删除旧 `translation/sequence` 执行引擎、旧 `lightnovel_translation/job.py`、`index.py`、`continuation.py`，以及对应旧测试。
- 共享能力迁移：
  - `sequence/schemas.py` → `translation/config_spec.py`
  - `sequence/output.py` → `translation/review/output.py`
  - `sequence/targeted.py` → `translation/evidence/targeted.py`
  - `source_message`/`full_pair_message` → `translation/index/prompts.py`
- 新增 `translation/index/workflow.py`，提供 `TranslationWorkflow`：lazy 参考规划、高层摘要、快照、ContextPlan + workset/实体/线程/定向证据装配、`plan_request` 预算和真实翻译。
- 新增 `translation/index/pipeline.py`，提供 `TranslationStore`、`TranslationRunner`、`build_epub`：逐章 `source-records` 翻译、`chapter-review` 审校、裁决、有限修订/复审、accepted 提交、滚动摘要、术语抽取和 EPUB 导出；状态存 SQLite，可中断续跑。
- `TranslationRunner.run_concurrent` 支持批次并发：同批次章节没有译文依赖，翻译第 i 章使用共享已接受前缀 + 批内前序章节的日文原文；批次开始前统一做专名预抽取。`translation_concurrency` 个翻译请求并发，`review_concurrency` 个审校/裁决/修订并发，主线程按顺序提交 accepted。批量大小由 `batch_size` 控制，需按容量选择。
- 新增离线示例 `examples/library_offline.py`，验证新 API 不依赖模型或本地语料。
- 新文档：
  - `docs/usage.md`：新工作流与当前自动化边界
  - `skills/lightnovel-translation/references/new-architecture.md`：agent 具体工作流、手动接管项
  - `skills/lightnovel-translation/SKILL.md`：main 工作流入口
  - 旧 `api.md`/`workflows.md`/`manual/`/旧设计文档已标注 legacy

当前 main 的自动化边界：

- 已实现：导入、lazy 参考索引规划、direct/rollup 摘要、ContextPlan、workset/实体/线程/定向证据装配、预算、TaskStore/指针、逐章翻译、`chapter-review` 审校/裁决/修订/提交、滚动摘要、术语抽取、EPUB 导出和指标。
- 仍需 agent 判断：跨版本目录锚点、实体/线程目录、单章拆分译文对齐、blocked 章节的局部修复。
- 需要旧完整自动管线时使用 `legacy-sequence` 分支。
- 验证：共享测试、新索引测试和 pipeline 离线测试通过；离线示例输出摘要 + 全文 + target 段并完成两次 provider 调用；真实 Web 章节已跑通翻译 → 审校 → 裁决 → 修订 → 复审 → accepted。
