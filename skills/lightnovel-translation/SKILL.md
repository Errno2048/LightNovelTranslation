---
name: lightnovel-translation
description: 使用 lightnovel_translation 的新分层索引工作流标准化 EPUB/TXT 轻小说、构建 lazy 参考索引、装配证据绑定上下文，并执行翻译→chapter-review 审校→裁决→修订→accepted 提交→EPUB 导出；跨版本目录对齐和 blocked 章节可能需要手动接管。
---

# Light Novel Translation

使用 Python >= 3.11 的 `lightnovel_translation`，安装名为 `lightnovel-translation`。main 分支只保留新的分层索引工作流；旧 `sequence` 引擎完整归档在 `legacy-sequence` 分支。

## 先查文档

- 新工作流：[new-architecture.md](references/new-architecture.md)
- **运行、恢复、blocked 修补、质量复查、导出与故障排查**：[operations.md](references/operations.md)
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
from translation.index import TranslationWorkflow, GlossaryTerm, LevelConfig

with TranslationProject("work/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
    flow = TranslationWorkflow(project, "web-ja", "web-zh",
                               levels=LevelConfig((10, 10), (3, 1)))
```

先核对阅读顺序、标题和起始位置；不能把标题数字直接当数组下标。参考译文只作术语/风格候选，日文是事实来源。

### 2. lazy 参考索引

```python
plan = flow.reference_plan()
print([(b.level, b.start, b.end, b.estimated_tokens, b.oversized)
       for b in plan.by_level(2)])
```

- 只规划 `not_created` 边界，不立即调用模型；估算使用 1.15 系数。
- 参考分支低层块默认懒创建。当前 `TranslationRunner` 的 accepted 快照只包含章节和已生成的 level-2 摘要，**不会自动物化目标 level-1 块**；不要把规划能力当成已接入的自动闭环。
- 未配置的高层（例如 level 3）禁止创建。
- `summarize_reference` 是一次性直出辅助，**不做容量预检/自动拆段/rollup**；长参考请用 `runner.ensure_reference_summary()` 按 `reference_chunk` 分块。

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

`assemble` 按 L0 固定协议 → append-only core glossary/reference summary → 已接受摘要 → 本批新增术语/近期全文 → 本章有界证据与正文排列；实体 target_form、线程/未决和 `source_message` 仍会加入。历史术语证据只保留源文对齐的短片段，并受 `evidence_*` 上限控制。检查 `report["fits"]` 和 `context.segments`；不 fit 时先缩小 batch，不能截断最近章节前缀。

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

### 6. 审校、提交与导出

使用 `TranslationRunner` 跑可恢复的章节闭环：

```python
from translation.index import (TranslationRunner, TranslationStore,
                               build_epub, collect_epub_chapters)

# {Web edition_order: official Chinese prose}
# Build this from the verified alignment; never reverse the direction or pass
# a directory object. Split Chinese chapters are concatenated with "\n\n".
reference_texts = {ja_order: chinese_text, ...}

with TranslationStore("work/my-novel/translation.sqlite") as store:
    runner = TranslationRunner(
        project, flow, store, continuation_start=199, main_end=260,
        reference_texts=reference_texts)
    runner.ensure_reference_summary()
    runner.ensure_glossary()
    runner.run_concurrent(start=199, end=260)
    runner.update_target_summary(force=True)
    chapters = collect_epub_chapters(
        runner=runner, reference_chapters=official_chapters, end=260)
    build_epub(output="book.epub", title="书名", chapters=chapters)
```

`run_order` 会逐源段翻译、用 `chapter-review` 审校、裁决、修订并复审；只有通过的章节写为 accepted。长任务改用 `run_concurrent`（见 [operations.md](references/operations.md#2-并发运行参数)）。

遇到 `ChapterBlocked`、摘要停住、目录对齐、provider 502/524、事后质量复查或导出失败时，按 [operations.md](references/operations.md) 的对应小节操作；其中包含精确修补目标段落、恢复 accepted、再次导出和结构验证的完整代码。不要跳过 blocked 章节伪造 accepted，也不要凭空删除审校记录。跨版本目录必须按标题/内容锚点对齐，不能把中文文库章号直接当 Web 话序；单章拆分和线程目录仍可能需人工接管。

## 保留的约束

- 不改原书、来源快照、已有导出或已冻结的产物；结构变化使用有版本约束的 patch 或新导入分支。
- 不删除错误标记、伪造指纹、添加空译文、重复段落或凭剧情补齐缺页来通过验证。
- 参考译文只用于译名、实体、事件和文风候选；冲突以日文为准。
- LLM 摘要/实体不是权威事实；partial、未决和抽取缺口必须保留。
- 每次模型尝试（含失败）写入 `events`；`limits.max_attempts` 控制传输/截断重试。总请求/token 上限在批次边界检查，批次内可能短暂超出。timeout/524 等未知远端完成状态不自动重发。
- 指标和参考摘要可能产生费用；`IndexMetrics` 需要调用方手动记录，runner 不会自动写入 direct/fallback/split/estimate 指标。
