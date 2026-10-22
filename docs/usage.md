# Python 库使用说明（新分层索引架构）

main 分支已切换到新的分层索引工作流。旧的 `sequence` 引擎和 `create_job/job.run` API 完整归档在 git 分支 **`legacy-sequence`**；main 不再提供旧 job/index 入口。

- 架构设计：`docs/refactor-architecture-design.md`
- Agent 工作流：`skills/lightnovel-translation/references/new-architecture.md`
- 运行、恢复与手动接管：`skills/lightnovel-translation/references/operations.md`
- 旧版本文档：`docs/sequence-index-repair-plan.md`、`docs/token-index-entity-catalog-design.md`、`skills/lightnovel-translation/references/manual/` 仅作 legacy 参考

## 安装

```console
python -m pip install .
python -m pip install -e ".[dev,narrative]"
python -m pip install ".[syosetu]"
```

安装名为 `lightnovel-translation`，导入名为 `lightnovel_translation`。新索引位于 `translation.index`。

## 私有配置

```console
python -m lightnovel_translation config init
python -m lightnovel_translation config path
python -m lightnovel_translation config check
```

默认配置为 `~/.lightnovel_translation/config.toml`；可用 `LIGHTNOVEL_TRANSLATION_HOME` 指定私有目录。模板见 [config.example.toml](../skills/lightnovel-translation/assets/config.example.toml)。

用户在本地填写 `[model]` 的 `model`、`base_url`、`api_key`，以及实际 `[capacity]` 和角色输出预算。Agent 不读取、打印、复制或上传凭据文件；`config check` 只返回路径、就绪状态和缺失字段名。

`TranslationProject(workspace)` 自动加载用户配置；显式传入 `TranslationConfig()` 可做无模型本地工作；`config="custom.toml"` 使用指定文件。

## 导入语料

```python
from lightnovel_translation import TranslationProject

with TranslationProject("work/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
```

导入只做规范化，不调用模型。先检查开头、结尾、连接处和可疑超长条目；不能把标题中的数字直接当数组下标。

## 新工作流

### 1. lazy 参考索引

```python
from translation.index import TranslationWorkflow, GlossaryTerm, LevelConfig

flow = TranslationWorkflow(
    project, "web-ja", "web-zh",
    levels=LevelConfig((10, 10), (3, 1)),
    full_chapters=10,
)
plan = flow.reference_plan()
print(len(plan.blocks), len(plan.by_level(1)), len(plan.by_level(2)))
print([(b.start, b.end, b.estimated_tokens, b.oversized)
       for b in plan.by_level(2)])
```

- level 1 = 章节组块，level 2 = 父块；未配置的高层禁止创建。
- 参考分支先只规划 `not_created` 边界，不立即物化。
- 目标分支已接受章节需要完整索引。
- 估算使用 1.15 系数和 direct/rollup 两套公式；超限时按章节边界拆分。

### 2. 直出高层摘要并构建快照

```python
docs = flow.documents(start=0, end=112)
summary, summary_result = flow.summarize_reference(start=0, end=100)
snapshot = flow.make_snapshot(
    summary, summary_start=0, summary_end=100,
    full_start=100, full_end=110, documents=docs,
)
```

远距历史用高层摘要，近距章节用全文。参考译文只作术语/风格候选；日文是事实依据。

### 3. 装配上下文并翻译

```python
glossary = [GlossaryTerm("フラン", "芙兰", "person"),
            GlossaryTerm("イオ", "依尤", "person")]
unresolved = ["前章未决问题"]
threads = {"active": [], "resolved": [], "dormant_relevant": []}

messages, report, context = flow.assemble(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary, unresolved=unresolved, threads=threads,
)
print(report["fits"], report["estimated_input_tokens"])
print([(s.representation, s.level, s.start, s.end) for s in context.segments])

result, report, context = flow.translate(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary, unresolved=unresolved, threads=threads,
    output_tokens=16384,
)
print(result.text[:1000])
```

装配内容：

1. system + 未审候选声明；
2. workset（core、实体卡片、unresolved、gaps、threads）；
3. ContextPlan segments（层级摘要、全文、批内日文、目标）；
4. 定向证据（术语出现位置、参考命中）；
5. `source_message`（只输出正文，不输出标题/段落 ID）；
6. final output-only 指令。

不 fit 时先缩小 batch，不能截断最近章节前缀。

### 4. 章节审校、提交与导出

`TranslationRunner` 把单章翻译、审校、裁决、修订和提交串成可恢复事务：

```python
from translation.index import (TranslationRunner, TranslationStore,
                               build_epub, collect_epub_chapters)

with TranslationStore("work/my-novel/translation.sqlite") as store:
    runner = TranslationRunner(
        project, flow, store, continuation_start=199, main_end=260,
        reference_texts=reference_by_source_order,
        history_chapters=3, summary_every=5, summary_lag=0,
        max_revisions=2,
        translation_tokens=65536, review_tokens=65536,
        adjudication_tokens=32768)
    runner.ensure_reference_summary()   # 官方参考的滚动摘要
    runner.ensure_glossary()            # 日中双语对照术语
    for order in range(199, 260):
        runner.run_order(order)         # accepted 章节自动跳过
    runner.update_target_summary(force=True)
    chapters = collect_epub_chapters(
        runner=runner, reference_chapters=official_chapters, end=260)
    build_epub(output="book.epub", title="书名", chapters=chapters)
```

- 翻译使用 `source-records`：`@@TITLE` + 每条源段一条 `@@TRANSLATION`，程序验证顺序和覆盖。
- 审校使用 `chapter-review`；confirmed 的 critical/major 触发修订，最多 `max_revisions` 轮，之后仍存在则写 `blocked`。
- `TranslationStore` 保存不可变版本、accepted 文本、滚动摘要、术语和事件；重启后从同一 SQLite 继续。
- 官方文库与 Web 的章号可能因拆章/合章/插图页不同，`reference_texts` 必须来自人工核对锚点或模型对齐，不能按序号硬配。

长任务可使用窗口化并发：

```python
spec = project.config.sequence_spec()
runner.run_concurrent(
    start=199, end=260,
    translation_concurrency=spec.translation_concurrency,  # 默认 3
    review_concurrency=spec.review_concurrency,            # 默认 4
    batch_size=30,                                         # 按容量选择
    update_summaries=True,
)
```

批次内章节没有译文依赖：翻译第 i 章时使用“共享已接受前缀 + 批内前序章节的日文原文”，不使用同批其他章节译文。批次开始前会做一次专名预抽取，所有翻译 worker 共享术语表；`translation_concurrency` 个翻译请求并发执行。审校/裁决/修订按 `review_concurrency` 并发，同批已有译文只作为审校的 provisional 历史。主线程按顺序提交 accepted。`batch_size` 过大可能超出上下文，较小模型应减小批次。

## 任务 DAG 与指针

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
- 指针单调推进；`advance_pointer` 使用短事务 + outbox。
- 不在持有写锁时调用模型。
- 阶段配额建议 review/accept 40%、materialize/rebuild 30%、translate 30%。

## 当前自动化边界

章节审校、裁决、修订、提交和 EPUB 导出已由 `TranslationRunner` 覆盖；以下仍需 agent 判断：

1. 跨版本目录对齐必须按标题/内容建立映射，不能把文库章号直接当 Web 话序。
2. 实体/线程目录仍由调用者提供；`ensure_glossary` 的自动术语只作候选，冲突以日文和人工核对为准。
3. 单章拆分只有接口预留，译文对齐未实现；单章超限应 blocked。
4. token 估算仍使用保守 byte 估算；接入真实 tokenizer/usage 校准前不要把估算当精确计费。
5. 参考摘要/术语/审校都会产生模型费用，应缓存并复用 SQLite。
6. 低层 `not_created` 块被真正请求时，先创建高层父块的全部低层子块再 rollup。
7. provisional/参考内容不得标记为 accepted 目标知识，不得混入最终交付。

## 指标与调试

```python
from translation.index import IndexMetrics

metrics = IndexMetrics()
metrics.record("direct_attempt", level=2)
metrics.dump("work/my-novel/metrics.jsonl")
```

建议记录 direct 尝试/成功/失败、rollup、split、estimate/actual 偏差、lazy planned/created 数量、指针推进和每批 token 构成。

## 旧版本

旧完整 `sequence` 引擎位于 `legacy-sequence` 分支：

```console
git switch legacy-sequence
```

该分支保留 `create_job/job.run`、旧审校/提交/导出管线和旧测试；main 不保证这些入口存在。
