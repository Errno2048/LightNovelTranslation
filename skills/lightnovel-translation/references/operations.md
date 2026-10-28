# 运行、恢复与手动接管

本页补充 [new-architecture.md](new-architecture.md) 没有展开的运维步骤。目标是让 agent 只读文档就能判断该检查什么、该修复哪一段、如何续跑和导出，不必先阅读 `translation/index/pipeline.py` 源码。

## 0. 先判断当前阶段

| 现象 | 优先检查 |
|---|---|
| 还没导入或章数不对 | [1.1–1.2](#11-配置与-provider-探针) |
| 角色分类错、番外被跳过、导出丢内容 | [0.1](#01-角色计划番外补译与样式05) |
| 译名混用、标题/引号风格断层 | [0.1](#01-角色计划番外补译与样式05) |
| 参考区间内有缺译条目需要补译 | [0.1](#01-角色计划番外补译与样式05) |
| 中文/日文章号对不上、接续点不明 | [1.3](#13-跨版本目录对齐与接续点) |
| 摘要/术语缺失或摘要停住 | [1.4](#14-参考摘要术语与上下文预算) |
| 运行很慢、并发没有生效 | [2](#2-并发运行参数) |
| 想知道进度、成本或某一章为什么被拒绝 | [3](#3-状态进度与成本) |
| `ChapterBlocked`、两次修订仍失败 | [4](#4-恢复与-blocked-章节修复) |
| 全稿完成但仍有少量确认意见 | [5](#5-事后质量复查) |
| EPUB 导出失败或需要复核 | [6](#6-导出与验证) |
| 502/401/429/524/timeout/truncated | [7](#7-provider-与模型故障) |
| 单章超长、线程目录、模型身份变化 | [8](#8-仍然需要人工判断的事项) |

## 0.1 角色计划、番外、补译与样式（0.5）

续译前先确认以下事项，旧版“番外直接跳过”的行为已改变。

### 角色计划

```python
from translation.index import ChapterPlan, load_chapter_plan
plan = load_chapter_plan(project, "web-ja")
print(plan.counts(), plan.main_ratio, plan.warnings)
```

- 优先读取项目根目录的 `chapter_plan.json`；没有时用 `ChapterClassifier` 从标题/`section` 在线分类。
- 角色：`main`、`extra`、`character_sheet`、`setting`、`preface`、`afterword`、`unknown`。
- 默认翻译角色由 runner 的 `translate_roles` 决定：`main` + `extra` + `character_sheet` + `setting` + `preface` + `afterword`。
- `main_ratio < 0.90` 输出 `classification_suspect`，应人工确认；`< 0.75` 硬停。`unknown` 不得自动翻译。
- 采集 manifest 的 `section` 是番外/人物表的主要信号；标题规则只是补充。不要用固定 `^\d+话` 作为唯一判据。

### 参考术语前置

存在 `reference_texts` 时，`ensure_glossary()` 必须先完成；runner 在缺失时会自动补跑，空术语表会抛 `reference_glossary_empty`。参考术语通常带 `confidence=1/2`，会进入确定性 `term_checks`。不要在未完成术语预载的情况下跑续译。

并行 review worker 必须使用主线程传入的 `glossary` / `tool_context`，不能自己调用 `self.store.glossary()`、`self.store.translations()` 等；包括 unresolved 专名检查。SQLite 连接只允许主线程使用，否则高并发下会出现 `sqlite3.ProgrammingError`。

### 番外与统一导出

- `run`/`run_concurrent`/`run_pipelined` 默认纳入番外与人物表；不再使用 `_is_extra_title` 跳过。
- 导出用 `collect_planned_chapters()`：

```python
from translation.index import collect_planned_chapters
chapters = collect_planned_chapters(
    runner=runner, reference_chapters=official_chapters, end=main_end,
    bilingual=True)
```

- 参考章 `bilingual_mode="blocks"`（按章对照），生成章 `bilingual_mode="pairs"`（逐段 S/T）。`build_epub(..., bilingual=True)` 会按字段分支。
- 旧 `collect_epub_chapters()` 仍保留兼容，但它会跳过番外；续译导出不要用它。

### 样式与标题

- runner 从参考标题/正文抽取 `StyleProfile`（标题 marker、序号格式、引号主风格、全半角、省略号、few-shot 示例）。
- 标题序号由规则渲染，模型只翻译后缀；不再依赖 `title_pattern_unparsed` 后自由保留。
- 引号/全半角由确定性检查规范化；审校上下文包含 `style`。不要只靠提示词强调。

### 补译（backfill）

```python
plan = runner.backfill_plan(start=0, end=runner.continuation_start)
print(plan.report)              # 缺口、region、预计量
# 给用户确认后再执行
runner.run_backfill(plan)
```

- 补译缺口 = 参考区间内、无参考、未 accepted 的条目。
- `plan_backfill` 按 source order 分组 region；每个 region 的 before/after 固定，Layer A（稳定术语/摘要/风格）→ Layer B（region 前后文）→ Layer C（单缺口目标）排列。
- 目标放最后；不要把 `target_order`、缺口序号写进 region 前缀。
- 补译应先于主线续译执行；完成后一次性更新术语/摘要，避免反复失效。

### 资产与配置

- 无图片 EPUB 会跳过全页资产解析；有图片时才构建 `assets/<edition>.json`。
- 生成阶段使用 `TranslationProject(path)`；`TranslationConfig()` 是空配置，不会自动加载用户模型配置。
- provider 探针使用 `max_tokens >= 256`；16 token 在部分端点会直接 `truncated`。

### 新增配置

```toml
[workflow]
glossary_concurrency = 2   # 参考术语块的模型并发；写库仍由主线程串行
translate_extras = true
translate_reference_pages = true
summary_extras = true
main_ratio_floor = 0.90
require_reference_glossary = true
auto_reference_glossary = true
strict_term_conflicts = true
style_profile = true
bilingual_mode = "auto"   # auto | pairs | blocks
backfill = "ask"          # ask | all | extras | none
failure_policy = "stop_batch" # stop_batch | continue
review_start = "barrier"      # barrier | as_completed | prefix_as_completed（后两者需 continue）
```

实测提示：`batch_size=30` + 30/30 能获得很高的单批吞吐，但当前 provider 在 30 并发下会出现 network/timeout，`repair_pending()` 又是串行的。生产建议 `batch_size=30`、`translation_concurrency=8–10`、`review_concurrency=8–10`；若坚持 30/30，保留 `failure_policy="continue"` + `review_start="as_completed"`，并在导出前跑 `repair_pending()`。review worker 只能在主线程传入的 `tool_context`/`glossary` 上工作，不能读写 SQLite。

`review_start` 三个取值的语义：

- `barrier`（默认）：批内全部翻译完成后才开始审校。
- `as_completed`：任一章翻译完成即提交审校；此时审校历史里可能还看不到批内更早章节的译文。需 `failure_policy="continue"`。
- `prefix_as_completed`：某章的批内前序章节全部落定（译完或终态失败）后才提交该章审校，保住审校历史的完整性；吞吐介于两者之间。需 `failure_policy="continue"`。

### 0.2 层级摘要块物化（规划中，TODO）

当前 runner **没有** level 1 / level 2 分层摘要物化。`summaries` 表里只有四个扁平 scope，块位置按章数/批次界定，与 token 预算和层级无关：

| scope | 触发点 | 块位置 |
|---|---|---|
| `reference` / `reference-progress` | `ensure_reference_summary()`，翻译前手动调用 | 0 到 `continuation_start`，按 `reference_chunk`（默认 16 章）分块，progress 记录断点 |
| `target-provisional` | pipelined 每批全部 draft 落库后 | 整批一块 `[batch_first, batch_last+1)`；模型失败降级为每章前 300 字拼接 |
| `target` | `update_target_summary()`：batch 每批后、逐章模式每章后、pipelined 仅收尾无 blocked 时 | 从 `continuation_start` 起、按 `summary_every`（默认 5 章）对齐的连续 accepted 前缀；范围内有未接受章节则指针停住 |

`translation.index.summary_index` 与 `translation.index.block_builder`（`SummaryIndex`）实现了真正的分层物化，但**尚未接入 runner、未从 `translation.index` 导出**，属于规划能力：

- 块边界由 `plan_blocks()` 按 token 预算贪心划分：level 1 受 `level1_capacity`（上下文窗口 − 摘要输出 − 安全余量 − 8192）和 `child_max_chapters` 约束，level 2 按 `expected_l2_count` / `parent_max_chapters` 归堆。
- level 2 物化先尝试 direct 直出，捕获 `context_budget` 后回退为“先物化全部 level-1 子块，再 rollup”（`lazy.required_children_for_high_level` 的不变量）。
- `translation_barrier()` / `review_barrier()` 把全文窗口对齐到 level-1 边界，用 `coverage_gap` 返回必须先补齐的区间；调用方负责物化。
- provisional → committed 的门槛是块覆盖章节全部 accepted（`commit_block`），与章号无关。

已知待办：`review_barrier` 当前直接复用 `translation_barrier`，`strict=False` 时审校屏障也接受 PROVISIONAL 覆盖，与“审校只基于 accepted 事实”的设计意图不符（docstring 已标注 TODO）。接线前需先裁决该差异。

## 1. 运行前检查

### 1.1 配置与 provider 探针

先运行 `config check`，它只报告就绪状态，不发起请求。然后在真正翻译前用一个极小请求验证端点：

```python
from lightnovel_translation import TranslationProject
from translation.schemas import GenerationRequest

with TranslationProject("work/my-novel") as project:
    result = project.provider.generate(GenerationRequest(
        [{"role": "user", "content": "只回复OK"}],
        max_tokens=16, task="probe"))
    print(result.text, result.usage)
```

常见结果：

- `authentication`/401：本地 key 缺失或无效。
- `server`/502 且空 body：本地网关到上游失败；`config check` 仍可能显示 ready。
- `timeout`/524：大请求超过端点/Cloudflare 时限；见 [2.3](#23-输出预算与截断)。
- 成功但 `reasoning_tokens` 很高：模型仍在推理，输出预算会被推理占用。

改名、换模型或换 `base_url` 会改变生成身份。不要把不同模型产出的 accepted 文本当作同一冻结任务继续；至少记录模型名和切换原因，必要时另建项目/导出分支。新 pipeline 不会替你强制校验 provider 身份。

### 1.2 导入数量与主線过滤

```python
chapters = project.edition("web-ja").chapters()
print(len(chapters), chapters[0].to_dict(), chapters[-1].to_dict())
```

注意：

- `ChapterInfo.position` 是过滤后的 0 基位置；`edition_order` 是 edition 内实际顺序，**可能有空洞**（因导入器过滤了插图/设定页）。不要用列表下标代替 `edition_order`。
- 中文文库主線通常以数字开头，可用 `^\s*\d{1,4}(?!\d)` 识别；不要只匹配“数字+空格”，`01脑袋有问题的少女` 这类没有空格。
- 日文 Web 的非主線页面常见标记：`※`、`人物紹介`、`設定紹介`、`後書き`、`あとがき`、`番外編`、`外伝`。主線末尾通常在最后一个正篇章节之后出现这些页面。
- `main_end` 是 **exclusive** 的 order。先人工看最后若干标题，确定最后一个正篇章节，再设 `main_end = 最后一章 order + 1`；不要用总话数直接当结束位置。
- 作者前后记可能作为正文段落进入语料（例如 `※...愚人节企划...`）。流水线会忠实保留。若产品要求删除，应在导入或导出阶段显式过滤，不能在中途删除段落来伪 Coverage。

### 1.3 跨版本目录对齐与接续点

官方文库与 Web 版通常不是一比一：

- 一话拆成上下两章（中文章号多于 Web）。
- 插图、人物介绍、设定页在 Web 有、文库没有，或反之。
- 文库可能合章、改标题、提前/延后收录。

因此**不能按章号直接配对**。实际可行的做法：

1. 打印中文主線列表与日文候选区间标题，人工确认若干锚点，例如：
   - 中文 `199 暗灰色的战场` ↔ 日文 `鈍色の戦場`（`edition_order=187`）。
2. 用锚点生成 `reference_texts`：`{日文 edition_order: 对应中文正文}`。一话对应多个中文章节时，把它们的正文用 `\n\n` 连接；**不要把中文标题拼进 `reference_texts`**，否则会污染 R/L 审校历史。
3. 校验接续点：`continuation = max(已映射日文 order) + 1`，再向前跳过 `extra`/缺失 order。
4. 用至少 5–10 个锚点核对标题，而不是只看最后一个。
5. 保存锚点/对齐 JSON 到工作目录，后续 resume 复用。

模型对齐接口 `align_reference_by_titles(runner, chinese_entries, source_entries)` 的输出是：

```json
{"pairs": [{"zh": 198, "ja": 187, "confidence": "high"}],
 "continuation_ja": 188,
 "mapping": {"187": 198}}
```

- `zh` 是中文主線列表中的 **0 基下标**（不是 `edition_order`）；构造 `reference_texts` 时通过 `zh_main[pair["zh"]]["order"]` 取实际 order。
- `ja` 是日文 `edition_order`。
- 允许一个 `ja` 对应多个 `zh`（拆章）。
- 方向必须是 `{日文 edition_order: 中文正文}`；反过来会取错历史参考。
- 模型可能把编号错配；必须用已知标题锚点复核。模型不可用时使用人工锚点插值，不要用简单标题相似度直接按顺序对齐（本次实测会产生十几章的偏移）。

### 1.4 参考摘要、术语与上下文预算

初始化阶段：

```python
runner.ensure_reference_summary()  # 按 reference_chunk 章递进，保存 reference-progress
runner.ensure_glossary()           # 双语对照抽词，按 chunk 记录 glossary_chunk
```

- 摘要和术语都是可选的；失败不应阻止翻译，但会降低远距上下文质量。
- 摘要/术语分块默认 16 章；输出预算至少 16,384，因为推理 token 也计入 `max_tokens`。
- 术语表是候选，不是审定规范；冲突以日文和人工核对为准。
- 目标摘要默认由 `[workflow]` 的 `summary_every` / `summary_lag` 控制（当前默认 5 / 0）。摘要与近期全文**允许有限重叠**，用于避免“摘要结束在全文窗口之前”造成空洞。
- 不要把 `summary_lag` 设成 `full_chapters` 同时把 `summary_every` 设为 10：这会在两次摘要之间留下最多 5 章的覆盖空洞。
- 续跑前先调用 `runner.update_target_summary()` 补齐摘要；它会跳过番外/缺失 order。若摘要停在某个番外话号附近，检查 `contiguous_accepted_end` 是否已修复为跳过 extra（本仓库已修复）。

上下文金额参考（本次实测配置）：

| 项目 | 值 |
|---|---|
| `full_chapters` | 5（最近全文窗口） |
| `history_chapters` | 3（审校历史） |
| `summary_every` / `summary_lag` | 5 / 0 |
| `reference_chunk` | 16 |
| `prompt_glossary_limit` | 512（append-only 稳定术语卡） |
| `evidence_excerpt_chars` / `evidence_max_excerpts` / `evidence_max_chars` | 120 / 2 / 12,000 |
| 翻译 / 审校 / 裁决 / 修订输出 | 65,536 / 65,536 / 32,768 / 65,536 |
| 摘要 / 术语输出 | 16,384 / 16,384 |
| 翻译批大小 | 30 章/批；`translation_concurrency` 个请求并发 |

### 1.5 术语置信度、确定性检查与 lookup 工具

术语表现在带 `confidence` / `aliases` / `locked`：

- `confidence=1`：已固定译名，确定性检查会视为必须使用的 canonical；
- `confidence=2`：需要根据语境判断，检查会标记但由审校模型决定；
- `confidence=3`：仅候选，不进入强制检查。

`ensure_glossary` 现在对每个参考块运行两次抽取：

1. 通用抽取：人名、地名、组织、称号、职务、种族、物品、技能；
2. 专项抽取：只提职务、称号、组织职位、种族/族裔、组织称谓，避免 40 项名额被人名占满。

专项结果写入 `glossary_focus_chunk` 事件；失败写 `glossary_focus_failed`，不阻断翻译。两次抽取都要求 `confidence`、`aliases`，并使用参考译文的前 160 字作为目标字形提示，避免输出简体。`ensure_batch_glossary` 对从未在参考日文中出现的新词强制 `confidence=3`。

`put_glossary` 保留更权威的 canonical，并把其它写法放入 aliases。`locked` 词条不会被后续批次改写。

术语抽取使用可续写的 `@@TERM` 记录协议：

- 每块上限 = `glossary_terms_per_chapter × 该块有效章节数 + glossary_terms_base`，默认 `5x+40`；
- 模型逐条输出 `@@TERM` 及 `source/target/kind/confidence/aliases` 字段，全部完成后单独一行输出 `@@END`；
- 响应因输出预算被截断时，丢弃最后一条未完成记录，把已完成记录作为 assistant 历史，并要求从下一条继续；最多 `glossary_max_continuations` 轮；
- 按 source 去重；无新增记录或达到上限时停止；事件记录 `terms/cap/rounds/truncated/complete/cap_reached/no_progress`；
- `glossary_complete` 带协议版本；旧版 JSON 抽取结果会按新协议重新抽取。批次专名预抽取也使用同一协议；
- 修改 `reference_chunk` 或 `glossary_terms_*` 后，已有 `glossary_complete` 不会自动失效；用 `ensure_glossary(force=True)` 或新库重跑。
- 底层术语表是 `term_entities` + `term_forms`：每个 source form 有自己的 target，实体用 `entity_id` 关联；`glossary()` 是兼容视图。全名/短名/称号的链接和合并见 2.2.1。

审校前会运行确定性术语检查：

- 对源文中出现的 `confidence<=2` 词条，若对应译文段落没有 canonical/alias，生成 `term_checks`；
- 结果写入 review context，**不会直接替换正文**；
- 审校模型必须为每个 check id 输出 `@@TERMCHECK`，`verdict=violation` 时还必须输出 `category: term` 的 FINDING；
- 同一词条的多处出现合并为一个 check，`occurrences` 列出最多 8 个位置；
- 裁决阶段会收到同一批 `term_checks`；`status=fixed_missing`（confidence=1 或 locked）的 canonical 具有规范权威，不能当作“未确认实体候选”驳回；
- 已确认的 fixed_missing 术语 FINDING 即使标为 minor 也会触发修订，因为它破坏已固定的译名一致性。

审校模型可使用原生工具：

- `read_chapter(order, include_source=false)`：读取当前章之前的参考/已接受/未审 draft；draft 返回 `language=zh-provisional`、`authority=provisional_draft`；
- `lookup_term(term, limit=5)`：查询规范译名、同一实体的其它 source form，以及参考/已接受/未审 draft 的实际用法。

预算来自 `[workflow]`：

```toml
glossary_terms_per_chapter = 5
glossary_terms_base = 40
glossary_max_continuations = 4
lookup_chapters = 8
lookup_terms = 50
max_term_checks = 50
max_tool_rounds = 4
```

预算和已用数量写入 review context 的 `lookup_budget`，每次工具结果也返回剩余预算。工具调用写入 `review_tool` 事件；模型调用事件包含 `tool_calls` 数量。`needs_lookup` 字段仍保留，但有工具时应优先实际调用工具。

## 2. 并发运行参数

### 2.1 `run_concurrent` 的真实语义

```python
runner.run_concurrent(start=continuation, end=main_end)
```

`translation_concurrency`、`review_concurrency`、`batch_size` 等默认值由配置文件的 `[workflow]` 节提供；临时覆盖时才显式传参。

- 批次按 `batch_size` 切分。**同批次章节之间没有译文依赖**：翻译第 i 章时，上下文由“共享已接受前缀”加“批内前序章节的日文原文”组成；前序章节的译文不进入 prompt。
- 翻译前执行一次批次专名预抽取：模型读取本批次日文原文与已有术语表，沿用已有译名并补充新专名；所有翻译 worker 使用同一份术语表。这样即使批内看不到彼此的译文，也能统一人名/地名/组织名。
- 批次开始先单独翻译**最后一章**，等它完成后再把其余章节提交到线程池并发。最后一章的 prompt 包含批内所有前序日文，先完成它等于为整批预热 provider 的前缀缓存；其余请求的公共前缀随后基本都是缓存命中。代价是每批多一次串行等待，换来整批大段上下文按缓存价计费。
- `translation_concurrency` 控制预热之后批次内并发翻译请求数；`review_concurrency` 控制审校/裁决/修订并发。主线程按顺序写 accepted，SQLite 保持单写者。
- 批内前序**译文**只在审校阶段作为 provisional 历史使用（所有译文已完成），用于检查跨章一致性；它们不参与翻译上下文。
- 某章 blocked 时该批停止；已完成 accepted 的章节保留，其余 draft 不自动接受。修复后从同一 SQLite 续跑，未 accepted 的章节会重新进入批次翻译。
- 顺序调试/单章复现用 `run_order(order)`；长任务用 `run_concurrent`。
- `batch_size` 不是越大越好：第 30 章的 prompt 会包含前 29 章的日文原文。1M 上下文可支持数十章；较小模型应减小批次或依赖更高层摘要，否则会触发 `context_budget`。批内前序原文是完整源文，不包含同批译文。

### 2.2 `run_pipelined`：允许审校滞后的批次流水线

```toml
[workflow]
translation_mode = "pipelined"   # "batch"（默认）或 "pipelined"
pipeline_review_backlog = 2      # 允许多少个已翻译批次等待审校
```

```python
runner.run_pipelined(start=continuation, end=main_end)
# 或按配置分派：
runner.run_mode(start=continuation, end=main_end)
```

流水线语义：

- 批次翻译完成后立刻写 draft；随后物化 `target-provisional` 摘要块，并开始下一批翻译；审校和 accepted 可以滞后。
- 下一批翻译上下文包含前驱未审 draft、provisional 摘要和滚动术语注册表；prompt 标注 `未审草稿` / `provisional`，不伪装成 accepted。
- 批次专名预抽取在 pipelined 模式下把参考范围外的新词提升为 `confidence=1`、`provisional=1` 的强制规范；后续批次必须沿用。首批实际用词优先，后到候选只进 aliases。
- 审校历史与工具（`read_chapter` / `lookup_term`）可读取前驱 draft，返回 `authority=provisional_draft`；`term_checks` 覆盖 provisional canonical，发现不同写法会触发修订。
- accepted 仍按批次顺序在主线程写入；`pipeline_batch` 事件记录每批结果。硬错误/普通 blocked 会停止启动新翻译并收尾；strict 的 unresolved major 不硬停，进入 `manual_review`（见 2.2.3）。
- `materialize_provisional_index` 失败时会写 fallback 摘要并记录 `index_block` 事件；`export` 仍只使用 accepted 文本。
- 当前一致性策略是“首次 draft 用词成为 provisional canonical”。如果之后需要改 canonical，使用 `store.mark_term_revisions(old_target)` 标记受影响章节，再重译/定点修订；自动全局术语裁决仍需上层规则或人工。

#### 2.2.1 实体/形式术语表

术语表是 `term_entities` + `term_forms` 两层结构，不再用一行 `(source,target,aliases)` 同时承担实体身份和形式译名：

- `term_entities.entity_id` 是稳定实体根，无 parent 链/path compression。
- `term_forms` 每个 source 一行，拥有自己的 `target`、`form_type`、`target_aliases`、provisional/locked。
- 全名、短名、称号可以共享 `entity_id`，但各自 target 独立；短名不会被强制成完整名。
- `aliases` 只表示同一 form 的另一中文写法，不再表示同一实体的其它 source 形式。
- 新增/链接/合并 API：`put_glossary`、`link_term_form`、`merge_term_entities`、`term_entity_forms`、`term_form`。
- `promote_provisional_terms` 会拒绝把 form target 提升为另一个 form 已占用的 canonical，并写 `term_target_conflict`；`term_conflicts` 会进入审校上下文。
- 实体合并只改 `entity_id`，不覆盖各 form target；合并是低频、显式操作。`lookup_term` 返回同实体 form 列表。

#### 2.2.2 标题模式

标题模式存放在 `term_forms`，`form_type="title_pattern"`：

```text
source: @title:あなたの為のオーケストラ 其の{n}
target: 为您独奏的管弦乐团 其{n}
metadata: {"ordinal_style":"chinese","marker":"其の","suffix":""}
```

- `translation.index.titles` 解析固定前缀、序号、后缀和标记；
- 已知模式直接按模板渲染，序号由单一中文/阿拉伯格式生成，避免批次间漂移；
- 新模式采用一次模型标题，登记 provisional，并规范序号；
- 只归一化重复/已知模式，唯一标题不强行抽象；
- 标题模式变更用 `runner.update_title_pattern(pattern_key, new_target)`，只更新 `title_zh`，不重译正文。

#### 2.2.3 严格裁决与人工接管

```toml
adjudication_mode = "strict"     # normal 或 strict
pipeline_manual_backlog = 2
```

- strict 模式下修订预算耗尽仍有 unresolved major，章节写 `manual_review`，不抛硬性 pipeline 错误。
- 翻译/自动审校继续；manual 之前的 accepted 前缀不变，manual 之后的批次写 `ready`，不自动 accepted/导出。
- `runner.manual_review_queue()` 查看队列；`runner.resolve_manual_review(order, decision="accept"|"revise"|"blocked", text=None, term_changes=None, notes="")` 处理。
- accept 后自动尝试 `accept_ready_prefix()`；revise 写回正文，等重新审校；blocked 保持阻塞。
- 人工修改术语时可在 `term_changes` 里给 source/target；旧 target 通过 `mark_term_revisions` 记录，下游重审/重译由调用者或后续流水线执行。
- `pipeline_manual_backlog` 达到上限且有 manual 队列时暂停启动新翻译；已有翻译/审校可收尾。
- strict 主要面向 pipelined；普通 `run_concurrent` 仍以 blocked 为主。

### 2.3 配置项归属

新工作流的默认值在 `[workflow]`：

| 配置 | 作用 |
|---|---|
| `translation_concurrency` / `review_concurrency` | 翻译 / 审校并发请求数 |
| `translation_mode` | `batch` 或 `pipelined`；后者允许审校滞后 |
| `pipeline_review_backlog` | pipelined 模式下允许等待审校的批次数 |
| `pipeline_manual_backlog` | 允许挂起的人工复核批次数 |
| `adjudication_mode` | `normal` 或 `strict`；strict 把 unresolved major 交给人工复核 |
| `batch_size` | 批次包含的章节数，必须与上下文容量匹配 |
| `full_chapters` / `history_chapters` | 近期全文窗口 / 审校历史章数 |
| `summary_every` / `summary_lag` / `reference_chunk` | 目标摘要节奏 / 参考摘要与术语分批 |
| `prompt_glossary_limit` | 稳定 core glossary 的条数；0 关闭，按 rowid append-only |
| `evidence_excerpt_chars` / `evidence_max_excerpts` / `evidence_max_chars` | 定向证据片段半径 / 每术语片段数 / 总片段字符上限 |
| `max_revisions` | critical/major 修订上限 |
| `translation_tokens` / `review_tokens` / `adjudication_tokens` | 各阶段输出预算 |
| `summary_tokens` / `glossary_tokens` | 摘要 / 术语抽取输出预算 |

旧 `[sequence]` 仅用于兼容：没有 `[workflow]` 时，`translation_concurrency`、`review_concurrency` 会从旧值迁移；其他旧字段（包括 `max_revisions`）不再驱动新 runner，使用 workflow 默认值。`[limits]` 的 `max_attempts` 控制传输/截断重试；总请求/token 上限在批次边界检查（批次内可能短暂超出）。每次模型尝试（成功或失败）都写入 `events` 的 `model` 记录。配置模板见 [config.example.toml](../assets/config.example.toml)。

### 2.4 输出预算与截断

- 端点可能把推理 token 计入 `max_tokens`。`finish_reason=length` 或 `ProviderError("truncated")` 时，先看 `usage.completion_tokens_details.reasoning_tokens`。
- `_generate` 会在 truncated 时自动把输出预算翻倍，最多到 `capacity.max_output_tokens`。仍然截断时：
  - 审校：减小 `history_chapters`，或把章节拆成更小的段落范围再审；
  - 摘要：减小 `reference_chunk`；
  - 翻译：单章过长时不要静默截断，标记 blocked 并交接（见第 8 节）。
- 32k 审校预算在长章 + 推理模型上可能截断；本次使用 65,536。
- `_generate` 只自动重试状态明确的失败：`truncated`（加预算）、`empty`/`finish_reason`、`rate_limit`（429）。`timeout`、`network`、`server`/502/524 的远端完成状态未知，不会自动重发；先查事件/产物再决定是否新建请求。重试次数由 `[limits] max_attempts` 控制。
- 端点有约 100 秒 Cloudflare 上限时，过大的 prompt 会 524；减小上下文或改用不受该限制的端点。不要用简单重试掩盖容量问题。

### 2.5 KV 前缀缓存

provider 按“从头开始的精确 token 前缀”复用缓存。任何逐章变化的字段只要出现在 prompt 前部，后面再长的参考摘要、全文和术语表都不会命中。当前约定：

- 翻译 prompt 的生命周期顺序是：L0 system/固定协议 → L1 稳定 core glossary（`prompt_glossary_limit`，append-only）+ reference summary → L2 已接受 target summaries → L3 本批新增术语/provisional summary/近期全文/批内前序日文 → L4 本章动态字段/有界证据/当前目标/S 标签表。
- 同批所有章节使用同一组历史摘要/全文段（`batch_near_reserve` 取批内最大近端上下文作为统一预算）；否则预算差异会让某章少选一段旧摘要，前缀在摘要处即分叉。
- 术语卡按首次出现顺序追加，不按 `evidence_count` 或 `source` 重排；核心投影只取前 `prompt_glossary_limit` 条，新词只追加到末尾，已有词条位置不变。批次新增术语放在 L3 尾部，不要重新过滤并重排整个 core。
- `glossary_evidence` 是源文对齐的有界片段：同一历史章节必须同时出现日文源词和 target/alias，只取命中附近窗口；`evidence_max_excerpts` / `evidence_max_chars` 控制每术语和总量，近期全文已覆盖的章节跳过。不要再把整章历史译文写进证据。
- 审校 prompt 先输出固定协议，再放 glossary/source/history/draft；**草稿放在 context JSON 最后**，同一章复审可复用草稿之前的前缀。依赖本章 term_checks/term_conflicts 的条件提示放在证据之后。修订同样先放固定协议，再放 evidence，S 标签表放最后。
- 裁决 prompt 的固定指令放在 findings JSON 之前；工具轮次用尽后保持工具 schema 不变，改用 `tool_choice="none"`，避免缓存前缀因工具定义消失而重置。
- 批次预热见 2.1。若 provider 不支持前缀缓存，预热只损失一次串行等待，不影响正确性。

查看命中情况（DeepSeek 兼容端点通常在 `usage` 返回 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`）：

```sql
SELECT json_extract(payload, '$.task') AS task,
       json_extract(payload, '$.usage.prompt_cache_hit_tokens') AS hit,
       json_extract(payload, '$.usage.prompt_cache_miss_tokens') AS miss
FROM events WHERE kind='model' ORDER BY event_id DESC LIMIT 50;
```

若网关在多个账号/节点间轮询，缓存亲和性可能变差；这属于网关配置，prompt 顺序无法修正。

## 3. 状态、进度与成本

所有状态在 `translation.sqlite`。常用查询：

```sql
-- 已完成/待处理
SELECT edition_order, status, title_zh, version FROM chapters ORDER BY edition_order;
-- 摘要覆盖
SELECT scope, start_order, end_order, length(text) FROM summaries ORDER BY scope, start_order;
-- 术语
SELECT source, target, kind, evidence_count FROM glossary ORDER BY evidence_count DESC;
-- 最近模型调用
SELECT event_id, edition_order, kind, payload, created_at FROM events
WHERE kind IN ('model','accept','blocked','revise','summary','glossary_chunk','quality_fix','manual_fix')
ORDER BY event_id DESC LIMIT 20;
```

事件含义：

| kind | 含义 |
|---|---|
| `model` | 一次成功的生成调用（含 task/usage/elapsed/request_id） |
| `translate` / `accept` / `blocked` / `revise` | 章节生命周期 |
| `summary_chunk` / `summary` | 参考/目标摘要进度 |
| `glossary_chunk` / `glossary_complete` / `glossary_failed` | 术语抽取 |
| `quality_fix` / `quality_fix_blocked` | 由外部质量修补脚本/agent 写入，不是 `TranslationRunner` 自动发出 |
| `manual_fix` | 人工精确修补 |

`events.payload` 是 JSON。`model` 记录包含 `success`、`attempt`、`task`、`usage`、`elapsed`；失败尝试也保留。模型 `usage` 中 `prompt_tokens`/`completion_tokens`/`reasoning_tokens` 可用于成本分析。审校通常是最大开销：本次 4 章窗口约 7 分钟审校/修订，翻译只占其中约 3–5 分钟。

轮询时不要重建整个 `TranslationProject`；SQLite WAL 允许只读查询。不要删除 `-wal`/`-shm` 文件，也不要并发启动两个写 runner。

## 4. 恢复与 blocked 章节修复

### 4.1 恢复原则

- 用同一 SQLite 重新构造 `TranslationStore`/`TranslationRunner`，accepted 章节会自动跳过。
- `blocked` 章节不会自动通过；必须先修文本或确认是误报。
- 同一窗口里的 draft 如果未 accepted，会在续跑时重新翻译。
- 续跑前调用 `runner.update_target_summary()` 补齐摘要。

### 4.2 查看阻断原因

```python
row = store.chapter(order)
review = json.loads(row["review"])
print(row["status"], row["title_zh"])
for finding in review.get("findings") or []:
    decision = (review.get("decisions") or {}).get(finding["id"], {})
    print(finding["severity"], finding["category"], finding["source_ref"],
          finding["target_ref"], decision.get("decision"))
    print("claim:", finding.get("claim"))
    print("suggestion:", finding.get("suggestion"))
    print("source:", finding.get("source_text"))
    print("target:", finding.get("target_text"))
    print("reason:", decision.get("reason"))
```

裁决策略（当前 runner）：

- confirmed 的 `critical` / `major` 触发完整重译修订；
- confirmed 的 `category: term` FINDING 若对应 `fixed_missing` 术语检查，即使为 minor 也触发修订；
- confirmed 的其它 minor / `source_meaning` 可能保留到事后质量复查（第 5 节）；
- `unresolved` 不触发修订；若它是 major，需要人工读原文/上下文判断，不能仅因为“未确认”就当成正确。

### 4.3 精确修补一个已确认段落

`T` 编号就是译文段落序号（1 基），`S` 编号对应原文段落。修补前必须确认原文/译文和裁决理由，不要用相近段落冒充。

```python
import json
from translation.storage import digest

row = store.chapter(order)
paras = row["text"].split("\n\n")
index = 209                      # T209
old = paras[index - 1]
new = old.replace("一起发现各种——模样吧", "和我一起发现各种——大人吧")
assert old != new and old in paras[index - 1]

paras[index - 1] = new
review = json.loads(row["review"])
review.setdefault("manual_fixes", []).append({
    "ref": f"T{index:03d}", "old": old, "new": new,
    "finding": "f1", "reason": "确认的敬称误译，按原文精确修补。"})
review["accepted_with_major"] = []
review["text_hash"] = digest("\n\n".join(paras))
version = store.put_draft(
    order=order, source_id=row["source_id"], source_hash=row["source_hash"],
    ja_title=row["ja_title"], title_zh=row["title_zh"],
    text="\n\n".join(paras), status="accepted", review=review)
store.event("manual_fix", order=order, version=version, findings=["f1"])
```

之后可直接续跑；也可以对修补后的章节跑一次 `review_order` 复核。若复核发现新的 major，应改回 `blocked` 并继续修，不能无条件接受。

### 4.4 修订预算用尽 / 建议反复失败

- 先读 `review["findings"]` 的 `source_text`/`target_text`/`claim`/`suggestion`，判断是原文事实、译文遗漏还是裁决误报。
- 若确认是误报（例如裁决 `unresolved` 且原文支持译文），记录人工覆盖理由并接受，不要为了“零 finding”反复重译。
- 若是真错误，按 4.3 精确修补目标段；不要删除审校记录或改 `max_revisions` 无限重试。
- 同一章反复出现同类错误时，优先检查术语表/历史译名/上下文，而不是增加修订次数。

### 4.5 协议失败

- 审校/裁决/修订输出不合法时 runner 会重试；仍失败会 blocked。
- 不要手工把 `complete=False` 的协议结果改成 accepted；要么重跑该章，要么按原文精确修补并记录。
- `truncated` 先增加输出预算/减少上下文；`timeout` 的远端完成状态未知时先查事件和产物，不要盲目重发付费请求。

## 5. 事后质量复查

当前接受策略只对 critical/major 自动修订；因此 accepted 章节仍可能有 confirmed 的轻微语义残留。全稿后建议做一次质量复查：

```python
for order in store.accepted_orders():
    row = store.chapter(order)
    review = json.loads(row["review"] or "{}")
    decisions = review.get("decisions") or {}
    findings = [f for f in review.get("findings") or []
                if decisions.get(f["id"], {}).get("decision") == "confirmed"
                and f.get("basis") == "source_meaning"]
    if findings:
        print(order, row["title_zh"], findings)
```

处理方式：

1. **精确修补**：suggestion 明确且原文可核对时，按 4.3 替换目标段。
2. **定向重译**：构造 `DraftChapter`，对确认意见做一次 `runner.revise`，再 `review_order`/`adjudicate`，只有无 major 才写回 accepted。示例：

```python
from translation.index import DraftChapter, ReviewOutcome

row = store.chapter(order)
review = json.loads(row["review"])
findings = [...confirmed, basis == "source_meaning"...]
decisions = {f["id"]: review["decisions"][f["id"]] for f in findings}
draft = DraftChapter(order=order, source_id=row["source_id"],
                     source_hash=row["source_hash"], ja_title=row["ja_title"],
                     title_zh=row["title_zh"], text=row["text"],
                     paragraphs=tuple(row["text"].split("\n\n")),
                     version=row["version"], review=review)
revised = runner.revise(draft, ReviewOutcome(
    findings=tuple(findings), unresolved=tuple(review.get("unresolved") or []),
    coverage_complete=True, zero_findings=False, raw=""), decisions)
new_review = runner.review_order(revised)
new_decisions = runner.adjudicate(revised, new_review)
# 若 new_review 仍有 confirmed major，保持 blocked；否则写 accepted 并记录 quality_fix。
```

- 修补章节后如需继续翻译，调用 `update_target_summary()`；仅导出则摘要不影响 EPUB 正文。
- 不要因为质量复查而修改已 accepted 章节的 `source_id`/`source_hash` 或冻结语料。

## 6. 导出与验证

```python
from translation.index import build_epub, collect_epub_chapters

chapters = collect_epub_chapters(
    runner=runner, reference_chapters=official_chapters, end=main_end)
build_epub(output="work/my-novel/final.epub", title="书名", chapters=chapters)
```

- `collect_epub_chapters` 要求从 `continuation` 到 `main_end`（排除番外）全部 accepted；缺章会抛 `missing_accepted`，不要跳过缺章导出。
- `reference_chapters` 是官方中文主線章节列表（`{"title", "paragraphs", "order"}`），按阅读顺序；番外/人物介绍不要混入。
- `build_epub` **拒绝覆盖已存在文件**；换新路径或先重命名旧文件。`run.py` 可用 `--output` 指定新路径。
- 导出后用 `verify_epub` 做结构检查：

```python
from translation.index import verify_epub
print(verify_epub("work/my-novel/final.epub"))
```

  它会检查 `mimetype` 首项、container、OPF manifest/spine、每个 XHTML 是否可解析且非空、图片资源是否存在，以及声明的图片 media-type 是否与字节魔数一致，并返回章节数/总字符数/图片数。它不等于完整 EPUBCheck 或语义验收。

### 图片资产

图片通过 edition 资产索引接入，不进入翻译文本：

```python
from translation.index.assets import load_asset_index

ja_assets = load_asset_index(project, "web-ja")
zh_assets = load_asset_index(project, "web-zh-norm")
reference_assets = {order: zh_assets.chapter_assets(order)
                    for order in zh_assets.chapters}
chapters = collect_epub_chapters(
    runner=runner, reference_chapters=official_chapters, end=main_end,
    reference_assets=reference_assets)
```

- `load_asset_index` 从源 EPUB 和导入语料的 `source_ranges` 建立块级/行内图片位置，按 SHA256 去重，缓存到 `assets/<edition_id>.json`。图片 media-type 按字节魔数嗅探，避免源 EPUB 把 JPEG 标成 `.png`。
- **块级图片**作为 sidecar 记录在 `assets.block`（`after_paragraph`/`before_paragraph`），不进入 prompt；导出时插入。
- **行内图片**在翻译时转成 `[[IMG:<asset_id>]]` 占位符，`parse_translation` 用规则校验每个占位符恰好出现一次且顺序不变；审校模型不需要主动发现图片问题。目标偏移记录在 `assets.inline`。
- `build_epub(..., assets=..., cover_asset=...)` 复制图片、生成 manifest、插入 `<img>` 并写入封面。`assets` 是 `{asset_id: {"data": bytes, "media_type": str, "alt": str}}`。
- 参考章图片来自参考 edition，续译章图片来自源 edition；不要在同一章混用两个版本的图片。封面/无文本图片页是全书级或最近章节的块级资产。
- 在图片支持之前已 accepted 的续译章，若源文含**行内**图片，无法从旧译文恢复占位符位置，需要重译；块级图片可以在导出时从源 edition 合并。

## 7. Provider 与模型故障

- **502（空 body）**：常见于本地 LiteLLM 到上游失败/密钥失效；`config check` 仍可能 ready。先用极小请求探针确认。
- **401**：key 缺失/无效；不要打印 key。
- **429**：限流；降低并发/等待，`_generate` 会对可重试错误退避。
- **524**：大 prompt 超过网关/Cloudflare 约 100 秒；减小上下文/拆分请求，而不是无限重试。
- **timeout**：远端完成状态未知；先查事件/产物，再决定是否重发。
- **truncated**：输出预算被推理占满；见 2.3。
- 代理：本地网关通常不应走代理；远程 OpenAI 兼容端点可能需要 `HTTP(S)_PROXY`（例如 `http://127.0.0.1:7890`）。只对当前进程设置，不要改用户全局配置，不要在日志中输出带认证信息的代理 URL。
- 更换模型/端点后不要静默续跑同一冻结任务；记录新身份并把新旧产物分开。

## 8. 仍然需要人工判断的事项

1. **跨版本锚点**：模型对齐只能当候选；最终接续点必须用标题/内容锚点核对。
2. **单章超长拆分**：当前只保留接口；单章超过容量时应 blocked，不要截断原文或伪造段落对齐。
3. **线程/实体目录**：`ensure_glossary` 只抽术语；线程目录、核心实体优先级仍由调用者或人工提供。
4. **裁决争议**：`unresolved` 的 major 需要读原文/前文；不能把“未确认”当“无错误”。
5. **模型身份变化**：换模型/端点需要明确的新任务/新导出，不要混入同一 accepted 分支。
6. **作者前后记/番外取舍**：这是产品决策；要么保留并标注，要么在导入/导出阶段显式过滤，不能中途删段伪 Coverage。
