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
| `FusedWorkflow` | 把 lazy 参考索引、ContextPlan、workset/术语/线程/定向证据和 `source_message` 装配起来 |
| `TaskStore` / `fair_select` | 持久化 DAG、阶段配额、单调指针/outbox |
| `IndexMetrics` | direct/fallback/split/estimate 偏差等 debug 指标 |

**目标分支**的 accepted 章节需要完整索引；**参考分支**才使用 `not_created` 惰性边界。参考译文只作术语/风格候选，日文是事实来源。

## 2. 导入与配置

```python
from lightnovel_translation import TranslationProject
from translation.index import FusedWorkflow, GlossaryTerm, IndexMetrics, LevelConfig

with TranslationProject("work/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
    flow = FusedWorkflow(
        project, "web-ja", "web-zh",
        levels=LevelConfig((10, 10), (3, 1)),
        full_chapters=10,
    )
```

`FusedWorkflow` 使用项目配置中的 provider；不要打印配置文件或 key。

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

## 8. 当前自动化边界与手动接管

main 分支尚未实现完整的新审校/提交/导出闭环。Agent 必须明确：

1. **章节审校、裁决、提交、最终导出**仍需手动接管，或在 `legacy-sequence` 分支使用旧 `create_job/job.run`。
2. **实体目录、线程目录、glossary** 目前由调用者提供或从参考数据构建；不要假定模型总结会自动更新 accepted glossary。
3. **单章拆分**只有接口预留；译文对齐未实现。单章超限时应 blocked，等待人工/后续版本。
4. **token 估算**目前使用保守 byte 估算；接入真实 tokenizer 或实际 usage 校准前，不要把估算值当作精确计费。
5. **参考摘要**可能需要缓存；重复调用会增加费用。
6. 低层 `not_created` 块被真正请求时，先创建高层父块的全部低层子块，再 rollup；这是当前阻塞规则，后续可优化为按需子集。
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
