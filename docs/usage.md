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

用户在本地填写 `[model]` 的 `model`、`base_url`、`api_key`，以及实际 `[capacity]` 和 `[workflow]` 输出预算。Agent 不读取、打印、复制或上传凭据文件；`config check` 只返回路径、就绪状态和缺失字段名。

新工作流的批次大小、并发数、上下文窗口、摘要节奏和各阶段输出预算在 `[workflow]` 节；模板已给出新默认值。旧的 `[sequence]` 节只在没有 `[workflow]` 时提供兼容迁移（`translation_concurrency`、`review_concurrency`），其余参数使用新的 workflow 默认值。

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
)
plan = flow.reference_plan()
print(len(plan.blocks), len(plan.by_level(1)), len(plan.by_level(2)))
print([(b.start, b.end, b.estimated_tokens, b.oversized)
       for b in plan.by_level(2)])
```

- level 1 = 章节组块，level 2 = 父块；未配置的高层禁止创建。
- 参考分支先只规划 `not_created` 边界，不立即物化。
- 当前 `TranslationRunner` 的 accepted 快照只包含章节和已生成的 level-2 摘要，不会自动物化目标 level-1 块；规划 API 与自动闭环要分开理解。
- 估算使用 1.15 系数和 direct/rollup 两套公式；`oversized` 只是规划标记。当前摘要流程不会自动 split/rollup，长参考请用 `runner.ensure_reference_summary()` 分块。

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

装配内容（按稳定 → 动态排列，便于复用 provider 前缀缓存）：

1. system + 未审候选声明；
2. 稳定 workset（core、术语实体卡片、gaps；术语表按首次出现顺序）；
3. ContextPlan segments（层级摘要、全文、批内前日文）；
4. 本章动态信息（unresolved、threads、target/batch 元数据）；
5. 定向证据（术语出现位置、参考命中）；
6. 当前目标日文正文；
7. 输出协议与 final output-only 指令。

不要把逐章变化的字段放在稳定块之前。`run_concurrent` 会先单独翻译批尾章以预热前缀缓存，再并发其余章节；同一批使用相同的历史段选择。

不 fit 时先缩小 batch，不能截断最近章节前缀。

### 4. 章节审校、提交与导出

`TranslationRunner` 把单章翻译、审校、裁决、修订和提交串成可恢复事务：

```python
from translation.index import (TranslationRunner, TranslationStore,
                               build_epub, collect_epub_chapters)

# {Web edition_order: 官方中文正文}; split Chinese chapters are concatenated.
reference_texts = {ja_order: chinese_text, ...}

with TranslationStore("work/my-novel/translation.sqlite") as store:
    runner = TranslationRunner(
        project, flow, store, continuation_start=199, main_end=260,
        reference_texts=reference_texts)
    runner.ensure_reference_summary()   # 官方参考的滚动摘要
    runner.ensure_glossary()            # 日中双语对照术语
    runner.run_concurrent(start=199, end=260)
    runner.update_target_summary(force=True)
    chapters = collect_epub_chapters(
        runner=runner, reference_chapters=official_chapters, end=260)
    build_epub(output="book.epub", title="书名", chapters=chapters)
```

- 翻译使用 `source-records`：`@@TITLE` + 每条源段一条 `@@TRANSLATION`，程序验证顺序和覆盖。
- 审校使用 `chapter-review`；confirmed 的 critical/major 触发修订，最多 `max_revisions` 轮。批量模式下仍存在则写 `blocked`；pipelined + strict 模式写 `manual_review`，由人工接管。
- `TranslationStore` 保存不可变版本、accepted 文本、滚动摘要、术语和事件；重启后从同一 SQLite 继续。
- 官方文库与 Web 的章号可能因拆章/合章/插图页不同，`reference_texts` 必须来自人工核对锚点或模型对齐，不能按序号硬配。

长任务可使用批次并发：

```python
runner.run_concurrent(start=199, end=260)
```

默认参数来自配置文件的 `[workflow]` 节（并发数、批次大小、上下文窗口、摘要节奏和输出预算）；需要临时覆盖时再显式传参。批次内章节没有译文依赖：翻译第 i 章时使用“共享已接受前缀 + 批内前序章节的日文原文”，不使用同批其他章节译文。批次开始前会做一次专名预抽取，所有翻译 worker 共享术语表；`translation_concurrency` 个翻译请求并发执行。审校/裁决/修订按 `review_concurrency` 并发，同批已有译文只作为审校的 provisional 历史。主线程按顺序提交 accepted。`batch_size` 过大可能超出上下文，较小模型应减小批次。

### 4.1 流水线并发续译

```toml
[workflow]
translation_mode = "pipelined"
pipeline_review_backlog = 2
pipeline_manual_backlog = 2
adjudication_mode = "strict"   # normal 或 strict
```

```python
runner.run_pipelined(start=199, end=260)
# 或按配置分派
runner.run_mode(start=199, end=260)
```

- 批次 N 完成翻译后立即写 draft，并物化 `target-provisional` 摘要块；批次 N+1 不必等待 N 审校/接受。
- N+1 的上下文包含 N 的未审 draft、provisional 摘要和滚动术语表；这些内容标记为 provisional，不冒充 accepted。
- accepted 仍按连续前缀推进；`ready` 表示自动审校完成但因前面的人工/阻塞无法 accepted。
- `pipeline_review_backlog` 限制自动审校积压；`pipeline_manual_backlog` 限制人工复核积压，达到上限时暂停启动新翻译。

### 4.2 实体/形式术语表

术语表不再把 `source→target` 和实体身份混在一行。每行 form 有独立 target，实体由 `entity_id` 表示：

```python
entity = store.term_entity("天津気 刹那")
store.link_term_form("セツナ", entity["entity_id"],
                     form_type="short", target="刹那")
forms = store.term_entity_forms(entity["entity_id"])
```

- 全名和短名可以共享实体，但各自 target 不同；短名不会被强制成完整名。
- `aliases` 只表示同一 form 允许的其它中文写法，不再表示“同一实体的其它 source 形式”。
- `promote_provisional_terms()` 会拒绝把某个 form 的 target 提升为另一个 form 已经占用的 canonical，避免 `セツナ→天津气刹那` 这类首见错误。
- `merge_term_entities()` 合并实体时只改 `entity_id`，保留每个 form 的 target；合并/链接应低频、显式进行。
- 审校的 `lookup_term` 会返回同实体的 form 列表，以便判断全名/短名关系。

### 4.3 标题模式

标题模式保存在同一术语表中，`form_type="title_pattern"`，例如：

```text
source: @title:あなたの為のオーケストラ 其の{n}
target: 为您独奏的管弦乐团 其{n}
```

- 程序解析源标题的固定前缀、序号、后缀和标记；
- 已知模式直接渲染目标标题，不再让模型逐章重译固定部分；
- 新标题模式先采用一次模型标题，登记为 provisional，并统一序号风格；
- 序号使用单一中文/阿拉伯数字格式（由模板决定）；
- 只归一化重复/已知模式，唯一标题保持原样；
- 标题模式变更用 `runner.update_title_pattern(pattern_key, new_target)`，只更新 `title_zh`，不需要重译正文。

### 4.4 严格裁决与人工接管

```python
queue = runner.manual_review_queue()
runner.resolve_manual_review(
    199, decision="accept",      # accept / revise / blocked
    text=None, notes="人工复核通过")
runner.accept_ready_prefix()
```

- strict 模式下，修订预算用尽仍有 unresolved major 时，章节写 `manual_review`，不抛出硬性 pipeline 错误。
- 翻译/自动审校继续；manual 之前的 accepted 前缀不受影响，manual 之后的批次写 `ready`，不会自动 accepted 或导出。
- `resolve_manual_review(decision="accept")` 接受该章并尝试按顺序提升后续 `ready` 批次；`revise` 写回正文并重新进入 draft；`blocked` 保持阻塞。
- 若人工修改术语，使用 `term_changes`；旧 canonical 会通过 `mark_term_revisions` 记录受影响章节。下游是否重译/重审需要再执行相应流程。
- strict 目前主要面向 pipelined 模式；普通 `run_concurrent` 仍以 blocked 为主。

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
2. 线程目录仍由调用者提供；术语表已有实体/form/link/merge API，但自动抽取只提供候选，全名/短名合并与 target 冲突仍需审校或人工确认。
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

`IndexMetrics` 需要调用方手动记录；`TranslationRunner` 不会自动写入 direct/fallback/split/estimate 指标。建议另行记录 lazy planned/created 数量、指针推进和每批 token 构成。

## 旧版本

旧完整 `sequence` 引擎位于 `legacy-sequence` 分支：

```console
git switch legacy-sequence
```

该分支保留 `create_job/job.run`、旧审校/提交/导出管线和旧测试；main 不保证这些入口存在。
