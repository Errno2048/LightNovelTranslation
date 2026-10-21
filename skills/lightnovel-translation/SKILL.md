---
name: lightnovel-translation
description: 使用 lightnovel_translation 的新分层索引工作流标准化 EPUB/TXT 轻小说、构建 lazy 参考索引、装配证据绑定上下文并执行日译中翻译；不自动完成审校/提交/导出，需要时明确手动接管。
---

# Light Novel Translation

使用 Python >= 3.11 的 `lightnovel_translation`，安装名为 `lightnovel-translation`。main 分支只保留新的分层索引工作流；旧 `sequence` 引擎完整归档在 `legacy-sequence` 分支。

## 先查文档

- 新工作流：[new-architecture.md](references/new-architecture.md)
- 完整设计与不变量：仓库 `docs/refactor-architecture-design.md`
- 用户配置与运行说明：仓库 `docs/usage.md`
- 输入结构异常：[input-repair.md](references/input-repair.md)
- 旧 API/手册：[api.md](references/api.md)、[manual/index.md](references/manual/index.md)（legacy，仅用于旧分支）

优先使用新工作流和结构化诊断；只有文档不足或任务要求修改库时再读源码。

## 配置与凭据

配置空模板见 [assets/config.example.toml](assets/config.example.toml)。首次执行：

```console
python -m lightnovel_translation config init
python -m lightnovel_translation config check
```

默认路径为 `~/.lightnovel_translation/config.toml`；可用 `LIGHTNOVEL_TRANSLATION_HOME` 指定目录。

Agent 不读取、打印、复制或上传凭据文件；不要要求用户在对话中提供 key。只运行 `config check`，它只返回路径、就绪状态和缺失字段名。缺失配置时仍可导入、规划、构建 lazy 边界和检查上下文；需要模型时等待用户配置完成。

## 工作流

### 1. 导入与确认范围

```python
from lightnovel_translation import TranslationProject
from translation.index import FusedWorkflow, GlossaryTerm, LevelConfig

with TranslationProject("work/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
    flow = FusedWorkflow(project, "web-ja", "web-zh",
                         levels=LevelConfig((10, 10), (3, 1)),
                         full_chapters=10)
```

先核对阅读顺序、标题和起始位置；不能把标题数字直接当数组下标。参考译文只作术语/风格候选，日文是事实来源。

### 2. lazy 参考索引

```python
plan = flow.reference_plan()
print([(b.level, b.start, b.end, b.estimated_tokens, b.oversized)
       for b in plan.by_level(2)])
```

- 只规划 `not_created` 边界，不立即调用模型。
- 目标分支 accepted 章节需要完整索引；参考分支低层块默认懒创建。
- 未配置的高层（例如 level 3）禁止创建。
- direct 摘要只在真实 context 越界时拆段/rollup；估算使用 1.15 系数。

### 3. 直出所需高层摘要

```python
docs = flow.documents(start=0, end=112)
summary, summary_result = flow.summarize_reference(start=0, end=100)
snapshot = flow.make_snapshot(
    summary, summary_start=0, summary_end=100,
    full_start=100, full_end=110, documents=docs)
```

参考高层摘要用于远距导航；近距章节使用全文。不要把参考摘要写成目标 accepted 事实。

### 4. 装配上下文

```python
glossary = [GlossaryTerm("フラン", "芙兰", "person"),
            GlossaryTerm("イオ", "依尤", "person")]
messages, report, context = flow.assemble(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary,
    unresolved=["前章未决问题"],
    threads={"active": [], "resolved": [], "dormant_relevant": []})
```

`assemble` 会加入 workset、实体 target_form、线程/未决、定向证据和 `source_message`（只输出正文，不输出标题/ID）。检查 `report["fits"]` 和 `context.segments`；不 fit 时先缩小 batch，不能截断最近章节前缀。

### 5. 实际翻译

```python
result, report, context = flow.translate(
    snapshot=snapshot, documents=docs,
    target_order=110, batch_orders=[110, 111],
    glossary=glossary, unresolved=["前章未决问题"],
    threads={"active": [], "resolved": [], "dormant_relevant": []},
    output_tokens=16384)
print(result.text[:1000])
```

术语优先使用 glossary/实体 target_form；参考事实不得覆盖日文事实。

### 6. 审校、提交与导出（当前需手动接管）

main 分支尚未实现新审校/裁决/提交/导出的完整闭环。完成翻译后：

- 必须人工或另行编写流程核对原文、术语、段落覆盖和未决问题；
- 不得把草稿标成 accepted，也不得把 provisional/参考摘要混入最终交付；
- 需要旧的完整自动 `create_job/job.run` 管线时，从 `legacy-sequence` 分支检出并在该分支运行；
- 当前低层 `not_created` 块被真正请求时，先创建高层父块的全部低层子块再 rollup；
- 单章拆分只有接口预留，译文对齐未实现；超限应 blocked 并交接。

## 保留的约束

- 不改原书、来源快照、已有导出或已冻结的产物；结构变化使用有版本约束的 patch 或新导入分支。
- 不删除错误标记、伪造指纹、添加空译文、重复段落或凭剧情补齐缺页来通过验证。
- 参考译文只用于译名、实体、事件和文风候选；冲突以日文为准。
- LLM 摘要/实体不是权威事实；partial、未决和抽取缺口必须保留。
- 请求、token 和尝试上限保留持久记账；同一付费响应应复用，未知远端完成状态不可直接重发。
- 指标和参考摘要可能产生费用；记录 direct/fallback/split/estimate 偏差和每批 token 构成。
