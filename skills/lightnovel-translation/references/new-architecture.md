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

**目标分支**的 accepted 章节需要完整索引；**参考分支**才使用 `not_created` 惰性边界。参考译文只作术语/风格候选，日文是事实来源。

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
        full_chapters=10,
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

- 先尝试 direct level-2；只有真实 context 越界才拆成两段/rollup。
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

装配顺序：

1. system + 未审候选声明；
2. workset（core、实体卡片、unresolved、extraction_gaps、threads）；
3. 参考索引/全文/批内日文/目标（ContextPlan segments）；
4. 定向证据（术语出现位置、参考译文命中）；
5. `source_message`（只输出正文，不输出标题/段落 ID）；
6. final output-only 指令。

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
                               build_epub, collect_epub_chapters)

with TranslationStore("work/my-novel/translation.sqlite") as store:
    runner = TranslationRunner(
        project, flow, store,
        continuation_start=199, main_end=260,
        reference_texts=reference_by_source_order,   # 官方参考 -> Web 话序
        history_chapters=3, summary_every=5, summary_lag=0,
        max_revisions=2,
        translation_tokens=65536, review_tokens=65536,
        adjudication_tokens=32768)
    runner.ensure_reference_summary()
    runner.ensure_glossary()
    for order in range(199, 260):
        runner.run_order(order)          # 每章一个可恢复事务
    runner.update_target_summary(force=True)
    chapters = collect_epub_chapters(
        runner=runner, reference_chapters=official_chapters, end=260)
    build_epub(output="book.epub", title="书名", chapters=chapters)
```

`TranslationStore` 保存不可变版本、当前 accepted 文本、滚动摘要、术语表和事件账本。`TranslationRunner.run_order`：

1. 用 `source-records` 逐源段输出翻译（`@@TITLE` + 每个 S 段一条 `@@TRANSLATION`）；
2. 用 `chapter-review` 对照完整 S/T 与近期 R/L 历史审校；
3. 逐条裁决；confirmed 的 critical/major 触发完整重译修订；
4. 复审，最多 `max_revisions` 轮；仍有 major 时写 `blocked` 并抛出 `ChapterBlocked`；
5. 通过后原子写 `accepted`，并每隔 `summary_every` 章生成增量目标摘要。

恢复时直接用同一 SQLite 重新构造 runner；accepted 章节会跳过。`blocked` 章节会重新进入草稿流程，或由 agent 检查 `review` 字段后局部接管。

长任务使用 `run_concurrent` 窗口化执行：

```python
spec = project.config.sequence_spec()
runner.run_concurrent(
    start=continuation, end=main_end,
    translation_concurrency=spec.translation_concurrency,  # 默认 3
    review_concurrency=spec.review_concurrency,            # 默认 4
    batch_size=30,                                         # 按容量选择
    update_summaries=True,
)
```

- 批次内章节**没有译文依赖**：翻译第 i 章时，上下文使用共享已接受前缀 + 批内前序章节的**日文原文**；不使用同批其他章节的译文。
- 批次开始前做一次专名预抽取，生成所有 worker 共享的术语表；已有术语沿用原译名，新术语统一候选。`translation_concurrency` 控制并发翻译请求数。
- 审校、裁决和修订在 `review_concurrency` 个线程中并发；审校历史可把同批已有译文作为 `provisional_target`，仅用于一致性检查。主线程按顺序写 accepted，SQLite 保持单写者。
- 某章 blocked 时批次停止；已完成章节保留，修复后从同一 SQLite 续跑。
- `batch_size` 决定批内前序原文的长度；1M 上下文可用数十章，较小模型应减小批次或增加高层摘要。`lookahead` 参数仅为旧调用兼容保留，不再驱动翻译上下文。

## 8. 当前自动化边界与手动接管

具体的状态查询、blocked 章节修补、事后质量复查、EPUB 验证和 provider 故障处理见 [operations.md](operations.md)。

章节审校/裁决/修订/提交与 EPUB 导出已由 `TranslationRunner` 覆盖。仍需 agent 明确处理：

1. **跨版本目录对齐**：官方文库章号与 Web 话号可能因拆章、合章、插图/设定页而不同，不能按序号硬配。`align_reference_by_titles` 可调用模型对齐；模型不可用时需要人工核对的锚点（见 `work/hero/anchors.py` 一类脚本）。
2. **实体目录、线程目录、glossary**：`ensure_glossary` 提供基于双语对照的自动抽取，但术语冲突仍以日文和人工核对为准；线程目录仍由调用者提供。
3. **单章拆分**只有接口预留；单章超出上下文时应 blocked 并交接。
4. **token 估算**仍使用保守 byte 估算；接入真实 tokenizer 或 usage 校准前，不要把估算值当作精确计费。
5. **审校成本**：每章至少一次翻译、一次审校、一次裁决和可能的修订/复审；应记录 usage 并避免无界重试。
6. 低层 `not_created` 块被真正请求时，先创建高层父块的全部低层子块，再 rollup。
7. 任何 provisional 参考内容都不得标记为 accepted 目标知识；最终交付必须明确哪些章节只是草稿。

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
