# 新分层索引工作流（main 分支）

main 分支只保留新的分层索引能力。旧的 `create_job` / `job.run` / 旧 `sequence` 引擎已归档到 **`legacy-sequence`** 分支；需要旧的完整自动审校/提交管线时，从该分支检出，不要在 main 上寻找。

完整设计与不变量见仓库 `docs/refactor-architecture-design.md`。

## 1. 架构边界

新流程由以下模块组成：

| 模块 | 作用 |
|---|---|
| `translation.index.LevelConfig` | level 1 = 章节组块，level 2 = 父块；未配置高层禁止创建 |
| `Artifact` / `IndexSnapshot` | 不可变章节/块、状态、输入、代际 |
| `plan_reference_blocks` | 参考索引 lazy 规划，产出 `not_created` 边界 |
| `build_context_plan` | 纯函数上下文：层级摘要 + 全文 + 批内日文 + 目标 |
| `TranslationWorkflow` | 把 lazy 参考索引、ContextPlan、workset/术语/线程/定向证据和 `source_message` 装配起来 |
| `TaskStore` / `fair_select` | 持久化 DAG、阶段配额、单调指针/outbox |
| `IndexMetrics` | direct/fallback/split/estimate 偏差等 debug 指标 |

**目标分支**的 accepted 章节需要完整索引；**参考分支**才使用 `not_created` 惰性边界。参考译文只作术语/风格候选，日文是事实来源。当前 `TranslationRunner` 的 accepted 快照只包含 level-0 章节和已生成的 level-2 摘要，尚未自动物化目标 level-1 块；不要把规划 API 当成已接入的自动闭环。

## 2. 导入与配置

```python
from lightnovel_translation import TranslationProject
from translation.index import TranslationWorkflow, GlossaryTerm, IndexMetrics, LevelConfig

with TranslationProject("work/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
    flow = TranslationWorkflow(
        project, "web-ja", "web-zh",
        levels=LevelConfig((10, 10), (3, 1)),
    )
```

`TranslationWorkflow` 使用项目配置中的 provider；不要打印配置文件或 key。

## 3. 参考索引 lazy 规划

```python
plan = flow.reference_plan()
print(len(plan.blocks), len(plan.by_level(1)), len(plan.by_level(2)))
print([(b.start, b.end, b.estimated_tokens, b.oversized) for b in plan.by_level(2)])
```

- 只规划边界，不立即调用模型；所有块状态为 `not_created`。
- 估算使用 `token_estimate_factor=1.15` 和 direct/rollup 两套公式。
- `oversized` 表示估算超出容量；真正创建时按章节边界拆分。
- 未配置的 level 3 不创建，也不进入上下文。

## 4. 选择目标并直出高层摘要

以下示例把参考 0–99 压成一个 level-2 摘要，参考 100–109 保留全文，目标 110/111 实际翻译：

```python
docs = flow.documents(start=0, end=112)
summary, summary_result = flow.summarize_reference(start=0, end=100)
snapshot = flow.make_snapshot(
    summary,
    summary_start=0, summary_end=100,
    full_start=100, full_end=110,
    documents=docs,
)
```

建议：

- `summarize_reference` 是一次性直出辅助，不做容量预检、自动拆段或 rollup；长参考请用 `TranslationRunner.ensure_reference_summary()` 按 `reference_chunk` 分块。
- 高层摘要成功后可缓存；参考低层块保持 `not_created`。
- 如果上下文真正需要低层块，按设计先创建该高层父块的全部低层子块，再 rollup；不要只创建所需子集而留下覆盖缺口。
- 只有高层摘要、没有 level-1 块时，最新摘要可能覆盖到近期全文窗口；ContextPlan 允许这种有限重叠，以避免摘要与全文之间出现空洞。重复是有意的安全余量。
- 参考块使用 `quality_labels=("rebuild_only","reference")`，不进入目标 accepted knowledge。

## 5. 装配上下文

```python
glossary = [
    GlossaryTerm("フラン", "芙兰", "person"),
    GlossaryTerm("イオ", "依尤", "person"),
    GlossaryTerm("ルシル商会", "露西尔商会", "organization"),
]
threads = {
    "active": [
        {"thread_id": "T-ORPHAN", "status": "active",
         "question": "孤儿院为何被领主中止援助？", "subject": ["バルボラ孤児院"]},
    ],
    "resolved": [], "dormant_relevant": [],
}
messages, report, context = flow.assemble(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary,
    unresolved=["孤儿院被领主中止援助的原因与幕后指使"],
    threads=threads,
)
print(report["fits"], report["estimated_input_tokens"], report["safe_input_tokens"])
for segment in context.segments:
    print(segment.representation, segment.level, segment.start, segment.end)
```

装配顺序按“生命周期长 → 短”排列，以复用 provider 的前缀缓存：

1. L0：system、固定输出协议；
2. L1：稳定 core glossary（`prompt_glossary_limit` 条，按 rowid 首次出现顺序，仅追加）和 reference summary；
3. L2：已接受的 target summaries（append-only；旧块不重排、不删除）；
4. L3：本批新增术语、provisional summary、近期全文窗口、批内前序日文；
5. L4：本章 unresolved/threads/target 元数据、有界定向证据、当前目标正文、本次 S 标签表。

`glossary_evidence` 只输出源文对齐的局部片段：同一历史章节必须同时出现日文源词和 target/alias，片段长度受 `evidence_excerpt_chars` 限制，每术语受 `evidence_max_excerpts` 限制，整体受 `evidence_max_chars` 限制；近期全文窗口已经包含的章节会跳过。不要再把历史章节全文作为“摘录”传入。

不要把 `target_order`、`unresolved`、`threads` 等逐章变化的内容放回稳定块之前：一旦前缀在早期分叉，后面的摘要、全文和术语都无法命中缓存。同批所有目标使用同一份历史段选择（`batch_near_reserve`），因此第 k 章的 prompt 是第 k+1 章（更长的批内前文）的字节前缀。

`assemble` 会自动用 `plan_request` 检查预算；不 fit 时先丢弃可选定向证据。仍不 fit 时应缩小 batch，不要截断最近章节前缀。

## 6. 真实翻译

```python
result, report, context = flow.translate(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary, unresolved=["..."], threads=threads,
    output_tokens=16384,
)
print(result.text[:1000])
print(result.usage)
```

输出协议明确禁止标题、段落 ID 和解释。术语优先使用 glossary/实体 `target_form`；参考事实不得覆盖日文事实。

## 7. 任务 DAG 与指针

```python
from translation.index import Task, TaskStore, run_once

store = TaskStore("work/my-novel/tasks.sqlite")
store.add(Task("reference-l2", "materialize", "materialize"))
store.add(Task("translate-110", "translate", "translate"),
          depends_on=("reference-l2",))
store.add(Task("translate-111", "translate", "translate"),
          depends_on=("translate-110",))
```

- 依赖只指向更早的批次/块。
- 指针单调推进；`advance_pointer` 用短事务 + outbox。
- 不在持有写锁时调用模型。
- 阶段配额建议 review/accept 40%、materialize/rebuild 30%、translate 30%。

## 7.1 章节闭环：翻译、审校、提交与导出

`translation.index.pipeline` 在 分层索引上下文之上提供可恢复的章节闭环：

```python
from translation.index import (TranslationRunner, TranslationStore,
                               build_epub, collect_planned_chapters)

with TranslationStore("work/my-novel/translation.sqlite") as store:
    runner = TranslationRunner(
        project, flow, store,
        continuation_start=199, main_end=260,
        reference_texts=reference_texts,   # {Web edition_order: 官方中文正文}
        )
    runner.ensure_reference_summary()
    runner.ensure_glossary()
    runner.run_concurrent(start=199, end=260)
    runner.update_target_summary(force=True)
    chapters = collect_planned_chapters(
        runner=runner, reference_chapters=official_chapters, end=260,
        bilingual=True)
    build_epub(output="book.epub", title="书名", chapters=chapters,
               bilingual=True)
```

人工复核可用 `build_bilingual_html(title=..., chapters=...)` 生成单文件 HTML：每个段落先译文、后原文，原文使用次级字号和左边线；按位置配对并显示段落数不一致提示。该渲染不依赖 JavaScript，也不使用 ruby 承载整段对照。

EPUB 使用同一套可选参数：`collect_epub_chapters(..., bilingual=True)` 会补充 `source_paragraphs`/`source_title`，`build_epub(..., bilingual=True)` 输出 `div.bl-pair` 两行对照；图片仍只随译文渲染一次。默认 `bilingual=False`，不会改变纯译文输出。

`TranslationStore` 保存不可变版本、当前 accepted 文本、滚动摘要、术语表和事件账本。`TranslationRunner.run_order`：

1. 用 `source-records` 逐源段输出翻译（`@@TITLE` + 每个 S 段一条 `@@TRANSLATION`）；
2. 用 `chapter-review` 对照完整 S/T 与近期 R/L 历史审校；
3. 逐条裁决；confirmed 的 critical/major 触发完整重译修订；
4. 复审，最多 `max_revisions` 轮；batch 模式仍有 major 时写 `blocked` 并抛出 `ChapterBlocked`，pipelined + strict 模式写 `manual_review`；
5. 通过后原子写 `accepted`。滚动目标摘要按 `summary_every` 章节奏增量生成；注意 `run_concurrent`/`run` 每批/每章后调用 `update_target_summary()`，而 `run_pipelined` 只在全部结束且无 blocked 时统一补齐——流水线中途的下游上下文由 `target-provisional` 块支撑（见 7.2）。

恢复时直接用同一 SQLite 重新构造 runner；accepted 章节会跳过。`blocked` 章节会重新进入草稿流程，或由 agent 检查 `review` 字段后局部接管；`manual_review` 用 `resolve_manual_review()` 处理。导出续译书稿用 `collect_planned_chapters()`（参考章 + 主线 + 番外按 source order 交错）；旧 `collect_epub_chapters()` 仍保留兼容，但会跳过番外，续译导出不要再用它。

长任务使用 `run_concurrent` 批次执行：

```python
runner.run_concurrent(start=continuation, end=main_end)
```

`translation_concurrency`、`review_concurrency`、`batch_size` 等默认值来自配置文件的 `[workflow]` 节；需要临时覆盖时再显式传参。`batch_size` 必须与模型上下文容量匹配。

- 批次内章节**没有译文依赖**：翻译第 i 章时，上下文使用共享已接受前缀 + 批内前序章节的**日文原文**；不使用同批其他章节的译文。
- 批次开始前做一次专名预抽取，生成所有 worker 共享的术语表；已有术语沿用原译名，新术语统一候选。`translation_concurrency` 控制并发翻译请求数。
- 审校、裁决和修订在 `review_concurrency` 个线程中并发；batch 模式同批已有译文作为 `provisional_target`；pipelined 还可看到前驱未审 draft。主线程按顺序写 accepted，SQLite 保持单写者。
- 某章 blocked 时该批停止；已完成章节保留，修复后从同一 SQLite 续跑。pipelined strict 的 manual_review 不停止后续翻译。
- `batch_size` 决定批内前序原文的长度；1M 上下文可用数十章，较小模型应减小批次或增加高层摘要。

### 7.2 pipelined 并发续译

```toml
translation_mode = "pipelined"
pipeline_review_backlog = 2
pipeline_manual_backlog = 2
adjudication_mode = "strict"   # 或 normal
```

```python
runner.run_pipelined(start=continuation, end=main_end)
runner.run_mode(start=continuation, end=main_end)  # 按配置分派
```

- 批次 N 完成翻译后写 draft 并物化 `target-provisional` 摘要；N+1 只依赖索引物化，不等待 N 审校/accepted。
- 下游上下文包含前驱未审 draft、provisional 摘要和滚动术语表，prompt 标注 `provisional`/`未审草稿`。
- accepted 仍按连续前缀推进；`ready` 表示审校完成但因人工 hold 不能 accepted。
- `manual_review` 队列、`resolve_manual_review()`、`accept_ready_prefix()` 处理人工接受/修订/阻塞；人工改术语时使用 `term_changes`，旧 target 通过 `mark_term_revisions` 记录。
- strict 模式主要面向 pipelined；普通 `run_concurrent` 仍以 blocked 为主。

### 7.3 实体/形式术语与标题模式

- `term_entities` 保存实体身份，`term_forms` 保存每个 source form 的 target；全名/短名共享实体但 target 独立。
- `aliases` 只是同一 form 的中文写法；同一实体的其它 source form 是独立行。
- `link_term_form` / `merge_term_entities` 显式维护全名/短名关系；合并只改 `entity_id`，不改各 form target。
- `promote_provisional_terms` 拒绝把 target 提升为另一个 form 已占用的 canonical，并记录/暴露 `term_conflicts`。
- 标题模式存为 `form_type="title_pattern"`，由 `translation.index.titles` 解析固定前缀/序号/后缀并渲染；只归一化重复模式，`update_title_pattern()` 只更新 `title_zh`。

### 7.4 章节角色、样式、补译与统一导出（0.5）

```python
from translation.index import (
    ChapterPlan, StyleProfile, BackfillPlan, plan_backfill,
    collect_planned_chapters, build_review_report)
```

- **角色计划**：`ChapterPlan` / `ChapterClassifier`（`translation.index.roles`）把条目分为 `main`、`extra`、`character_sheet`、`setting`、`preface`、`afterword`、`unknown`。`chapter_plan.json` 是首选来源；采集 manifest 的 `section` 比标题启发式更可靠。`main_ratio < 0.90` 需要人工确认，`< 0.75` 硬停。
- **runner 角色策略**：`TranslationRunner(..., translate_roles=..., summary_roles=...)`；默认翻译 `main`+`extra`+`character_sheet`+`setting`+`preface`+`afterword`。`run`/`run_concurrent`/`run_pipelined` 不再静默跳过番外。
- **参考术语**：存在 `reference_texts` 时，`ensure_glossary()` 是必需前置；runner 会自动补跑并对空术语表报 `reference_glossary_empty`。`unresolved` 中专名冲突会升级为 major。
- **样式**：`StyleProfile` 从参考标题/正文抽取；标题由 `render_main_title()` 规则渲染，正文由 `normalize_punctuation()` 规范化。只在提示词中强调不足以约束模型。
- **统一导出**：`collect_planned_chapters(runner, reference_chapters, end, bilingual=True)` 按 `source order` 交错参考章、主线译文和番外；参考章 `bilingual_mode="blocks"`，生成章 `pairs`。
- **审校报告**：`build_review_report(runner, markdown_path, json_path)` 输出 unresolved、confirmed findings、term checks、manual fixes 和 accepted-with-major。
- **补译**：`plan_backfill(runner, ...)` 生成 `BackfillPlan`（gap + region + before/after + prefix_hash）；`runner.run_backfill(plan)` 使用 `assemble_backfill()` 的 Layer A（稳定术语/摘要/风格）→ Layer B（region 前后文）→ Layer C（单缺口目标）布局。补译应先于主线续译。
- **资产**：`load_asset_index` 在 source EPUB 无图片资源时跳过逐页解析。
- **双语**：`build_epub(..., bilingual=True)` 读取每章 `bilingual_mode`；`blocks` 不再产生逐段“段落数不一致”提示。

### 7.5 层级摘要块物化（0.6 已接入）

0.6 起，层级摘要块（`translation.index.summary_index` / `block_builder` 的 `SummaryIndex`）是主要历史来源，已接入全部 runner 执行路径：

- 块边界由 token 预算贪心划分：level 1 受 `level1_capacity` 与 `child_max_chapters` 约束，level 2 按 `expected_l2_count` / `parent_max_chapters` 归堆。runner 检测到配置处于退化默认（单块容纳全部章节）时自动改用设计默认（L1=10 章、L2=100 章）。
- `runner.snapshot()` 优先把 `summary_blocks` 表中的 committed/provisional 块包装成 level 1/2 artifacts 喂给 ContextPlan；项目没有任何分层块时回退旧扁平 `summaries` scope（`target` 每 `summary_every` 章一块、`target-provisional` 每批一块），legacy 项目行为不变。
- 每批翻译前 `_enforce_barriers` 检查 `translation_barrier`：`execution_policy="strict"` 要求 committed 覆盖，`concurrent`（默认）接受 provisional；缺口先自动物化，无法闭合抛 `summary_barrier`。`review_barrier` 无论模式只认 COMMITTED（审校基于 accepted 事实）。
- 批内 DAG 交错物化：`prefix_as_completed`/`as_completed` 路径下，批内某 L1 块的章全部译完即物化该块摘要（覆盖章全部 accepted→committed，否则 provisional，事件 `index_block_interleaved`），后续章节审校可见；所有路径批结束后 rollup 全 committed 子块的 L2 父块，经 `commit_block` 统一提交。
- 上下文分层选择：level 1 保留最近 `context_num_level1`（默认 3）块 + sibling 补齐，level 2+ 按 `context_num_level2`（默认 "all"）用预算回填更早父块；全文窗口自动对齐到 L1 边界，不泄漏 target 之后的块。
- 导出前稳定化 `runner.stale_review_pass()`：accept 之后发生过术语修订、人工修补或相关块重建的 accepted 章，做一次全章重审（指纹防重复）；仍有 major → `manual_review`。

扁平 scope 的触发/边界表与运维细节见 [operations.md 0.2](operations.md#02-层级摘要块物化06-已接入)。`SummaryIndex` 仍由 runner 内部使用，未从 `translation.index` 导出。

## 8. 当前自动化边界与手动接管

具体的状态查询、blocked 章节修补、事后质量复查、EPUB 验证和 provider 故障处理见 [operations.md](operations.md)。

章节审校/裁决/修订/提交与 EPUB 导出已由 `TranslationRunner` 覆盖。仍需 agent 明确处理：

1. **跨版本目录对齐**：官方文库章号与 Web 话号可能因拆章、合章、插图/设定页而不同，不能按序号硬配。`align_reference_by_titles` 可调用模型对齐；模型不可用时需要人工核对的锚点（见 `work/hero/anchors.py` 一类脚本）。
2. **实体目录、线程目录、glossary**：`ensure_glossary` 提供基于双语对照的自动抽取；术语表已有 `term_entities`/`term_forms`、`link_term_form`、`merge_term_entities` 和 target-conflict 保护，但全名/短名合并、target 冲突和实体关系仍需审校或人工确认；线程目录仍由调用者提供。
3. **单章拆分**只有接口预留；单章超出上下文时应 blocked 并交接。
4. **token 估算**仍使用保守 byte 估算；接入真实 tokenizer 或 usage 校准前，不要把估算值当作精确计费。
5. **审校成本**：每章至少一次翻译、一次审校、一次裁决和可能的修订/复审；应记录 usage 并避免无界重试。
6. 低层 `not_created` 块被真正请求时，先创建高层父块的全部低层子块，再 rollup。
7. 任何 provisional/ready/manual_review 参考内容都不得标记为 accepted 目标知识或进入最终导出；最终交付必须明确哪些章节仍只是草稿/待人工复核。

## 9. 调试指标

```python
from translation.index import IndexMetrics
metrics = IndexMetrics()
metrics.record("direct_attempt", level=2)
metrics.dump("work/my-novel/metrics.jsonl")
```

建议记录 direct 尝试/成功/失败、rollup、split、estimate/actual 偏差、lazy planned/created 数量、指针推进和每批 token 构成。

## 10. 旧文档

以下文档描述旧 `sequence` 引擎，仅作为 legacy 参考：

- `references/api.md`
- `references/workflows.md`
- `references/diagnostics.md`
- `references/manual/*`

旧完整实现位于 git 分支 `legacy-sequence`；新设计文档位于 `docs/refactor-architecture-design.md`。
