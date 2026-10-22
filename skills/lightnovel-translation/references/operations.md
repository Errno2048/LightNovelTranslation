# 运行、恢复与手动接管

本页补充 [new-architecture.md](new-architecture.md) 没有展开的运维步骤。目标是让 agent 只读文档就能判断该检查什么、该修复哪一段、如何续跑和导出，不必先阅读 `translation/index/pipeline.py` 源码。

## 0. 先判断当前阶段

| 现象 | 优先检查 |
|---|---|
| 还没导入或章数不对 | [1.1–1.2](#11-配置与-provider-探针) |
| 中文/日文章号对不上、接续点不明 | [1.3](#13-跨版本目录对齐与接续点) |
| 摘要/术语缺失或摘要停住 | [1.4](#14-参考摘要术语与上下文预算) |
| 运行很慢、并发没有生效 | [2](#2-并发运行参数) |
| 想知道进度、成本或某一章为什么被拒绝 | [3](#3-状态进度与成本) |
| `ChapterBlocked`、两次修订仍失败 | [4](#4-恢复与-blocked-章节修复) |
| 全稿完成但仍有少量确认意见 | [5](#5-事后质量复查) |
| EPUB 导出失败或需要复核 | [6](#6-导出与验证) |
| 502/401/429/524/timeout/truncated | [7](#7-provider-与模型故障) |
| 单章超长、线程目录、模型身份变化 | [8](#8-仍然需要人工判断的事项) |

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
| 翻译 / 审校 / 裁决 / 修订输出 | 65,536 / 65,536 / 32,768 / 65,536 |
| 摘要 / 术语输出 | 16,384 / 16,384 |
| 翻译批大小 | 30 章/批；`translation_concurrency` 个请求并发 |

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

### 2.2 配置项归属

新工作流的默认值在 `[workflow]`：

| 配置 | 作用 |
|---|---|
| `translation_concurrency` / `review_concurrency` | 翻译 / 审校并发请求数 |
| `batch_size` | 批次包含的章节数，必须与上下文容量匹配 |
| `full_chapters` / `history_chapters` | 近期全文窗口 / 审校历史章数 |
| `summary_every` / `summary_lag` / `reference_chunk` | 目标摘要节奏 / 参考摘要与术语分批 |
| `max_revisions` | critical/major 修订上限 |
| `translation_tokens` / `review_tokens` / `adjudication_tokens` | 各阶段输出预算 |
| `summary_tokens` / `glossary_tokens` | 摘要 / 术语抽取输出预算 |

旧 `[sequence]` 仅用于兼容：没有 `[workflow]` 时，`translation_concurrency`、`review_concurrency` 会从旧值迁移；其他旧字段（包括 `max_revisions`）不再驱动新 runner，使用 workflow 默认值。`[limits]` 的 `max_attempts` 控制传输/截断重试；总请求/token 上限在批次边界检查（批次内可能短暂超出）。每次模型尝试（成功或失败）都写入 `events` 的 `model` 记录。配置模板见 [config.example.toml](../assets/config.example.toml)。

### 2.3 输出预算与截断

- 端点可能把推理 token 计入 `max_tokens`。`finish_reason=length` 或 `ProviderError("truncated")` 时，先看 `usage.completion_tokens_details.reasoning_tokens`。
- `_generate` 会在 truncated 时自动把输出预算翻倍，最多到 `capacity.max_output_tokens`。仍然截断时：
  - 审校：减小 `history_chapters`，或把章节拆成更小的段落范围再审；
  - 摘要：减小 `reference_chunk`；
  - 翻译：单章过长时不要静默截断，标记 blocked 并交接（见第 8 节）。
- 32k 审校预算在长章 + 推理模型上可能截断；本次使用 65,536。
- `_generate` 只自动重试状态明确的失败：`truncated`（加预算）、`empty`/`finish_reason`、`rate_limit`（429）。`timeout`、`network`、`server`/502/524 的远端完成状态未知，不会自动重发；先查事件/产物再决定是否新建请求。重试次数由 `[limits] max_attempts` 控制。
- 端点有约 100 秒 Cloudflare 上限时，过大的 prompt 会 524；减小上下文或改用不受该限制的端点。不要用简单重试掩盖容量问题。

### 2.4 KV 前缀缓存

provider 按“从头开始的精确 token 前缀”复用缓存。任何逐章变化的字段只要出现在 prompt 前部，后面再长的参考摘要、全文和术语表都不会命中。当前约定：

- 翻译 prompt 的顺序是 system → 稳定 workset（术语表按首次出现顺序）→ 参考摘要/近章全文/批内前序日文 → 本章动态字段 → 定向证据 → 当前目标 → 输出协议。
- 同批所有章节使用同一组历史摘要/全文段（`batch_near_reserve` 取批内最大近端上下文作为统一预算）；否则预算差异会让某章少选一段旧摘要，前缀在摘要处即分叉。
- 术语表按首次出现顺序追加，不按 `evidence_count` 或 `source` 重排；新词只追加到末尾，已有词条位置不变。不要按“本章出现”过滤掉术语表中间项，否则每次请求前缀都会在术语表处断开。
- 审校 prompt 把 glossary、source、history、unresolved 放在前面，**草稿放在 JSON 最后**；同一章的复审/修订可以复用草稿之前的前缀。修订同样把 confirmed findings 放在 source 之后、draft 之前。
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
- confirmed 的 minor / `source_meaning` 可能保留到事后质量复查（第 5 节）；
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

  它会检查 `mimetype` 首项、container、OPF manifest/spine、每个 XHTML 是否可解析且非空，并返回章节数/总字符数。它不等于完整 EPUBCheck 或语义验收。
- 本导出器只生成文本章节（标题 + 段落），不包含插图/封面；需要插图时应另写资源打包，不要伪造正文。

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
