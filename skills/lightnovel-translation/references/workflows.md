# 从输入到连续翻译的操作手册

本页面向调用已安装库的 agent。示例使用稳定章 ID / `ChapterInfo`，不要求理解内部引擎。先选实际安装包的 Python 解释器；其余操作保持同一解释器、同一工作区。凭据处理见 SKILL.md。

需要解释某一步为什么这样工作、参数如何生效或产物保存在哪时，查[工作原理手册目录](manual/index.md)。目录按本流程排序，每个模块独立成页，附源码文件与符号；不必遍历引擎或加载其他模块全文。

## 1. 导入、排序与选择目标

```python
from lightnovel_translation import TranslationProject, plan_reference_set

with TranslationProject("work/novel") as project:
    ja = project.import_book("web.epub", edition_id="web-ja", language="ja")
    ordered = plan_reference_set(reference_paths, edition_id="print-zh", language="zh")
    # 本地检查顺序证据与冲突；7–9 卷继续保留原卷号。
    print(ordered.to_dict())
    zh = project.import_edition(ordered.to_input_spec())
    chapters = ja.chapters()
    print([c.to_dict() for c in chapters[:3]])
```

`import_book` 的文件列表按照给定顺序处理；需要自动按卷排序时先用 `plan_reference_set`。机械校验不证明章节切分正确，应抽查开头、结尾和异常超长章。结构问题查 input-repair.md，避免编辑语料 JSONL。

从标题章号定位时，先检查 `raw_title` / `ChapterInfo.title`；不要把 Web 228 当成 `chapters[228]`。续译定位见 api.md 中 `plan_continuation` / `locate_continuation`。`suggested` 是建议，可能需要用户或 agent 核对支线与重复情节。

## 2. 选择参考模式

| 模式 | 何时使用 | 中文的用途 |
|---|---|---|
| source_only | 没有中文参考 | 无 |
| parallel | 已确认目标之前的原译对应 | 历史完整双语样本 |
| cross_edition | Web/文库、重排、改写或对应未确认 | 文风及有证据的术语候选 |

跨版本不要求匹配成功才允许翻译。没有关联时，会选择所提供中文译本中的正文样本，标记为独立版本。此类样本不能支持 Web 的剧情、人物状态或时间线。需要指定更合适的样本时传 `style_reference_chapters=[...]`，无需伪造剧情关联。

## 3. 局部建立跨版本索引（可选、收费）

如果已有需要使用的索引产物，复用它们。不要为十章窗口默认重新抽取全书。

```python
index = project.index([ja, zh])
targets = ja.chapters()[start_position:start_position + 10]
reference_window = zh.chapters()[reference_start:reference_stop]

source_nodes = index.select_nodes(targets)  # 本地，返回原生 L2
print([(n["node_id"], n["titles"]) for n in source_nodes])

prepared = index.prepare_references(
    targets, reference_window, workers=2, batch_size=20,
)
print(prepared["status"])
# 不完整时检查 build、associations 和 entity_matches；不能把 partial 当全覆盖。
```

`prepare_references` 构建这两组章节所属的 L2，进行区块关联，再在相关 L1 的已建实体目录之间匹配。它不会自动建满 L1 的所有子块，也不证明所选中文窗口包含全部译名。关联结果可为 related_context / unrelated，仍可使用独立文风样本。大量候选区块的笛卡尔积可能很贵，应从有依据的局部窗口开始。

需要前文人物描述辅助检索时，可将已经阅读过的源侧章节加入 source_chapters，或复用此前已建节点。`select_nodes(chapters, neighbors=1)` 可预览相邻 L2 后再显式 build；它只选择节点，不会自动构建。

更细控制可分别调用 `build`、`associate`、`match_entities`。`build` 同一 L1 内串行，不同 L1 间并发。`matching_coverage` 明确区分已报告、supported、未逐项判定与目录缺口；不能用记录中没有 unmatched 推断全覆盖。

## 4. 创建与预检

```python
with project.create_job(
    "continuation-v2", source=ja, chapters=targets, mode="cross_edition",
    references=[zh], index=index,
    associations=prepared["associations"], entity_matches=prepared["entity_matches"],
    resolve_terminology=True,
    # 可选：优先作为文风样本的实际中文章节
    style_reference_chapters=reference_window[-2:],
) as job:
    print(job.reference_report())
    plan = job.plan_next()
    print(plan["context"]["budget"])
    print(plan["context"].get("edition_reference_selection"))
    result = job.run(max_chapters=1, materialize=True)
    print(result.to_dict())
    job.export(include_io=True)
```

没有 prepared 产物时省略 associations/entity_matches 即可。`resolve_terminology=True` 会在创建任务时进行有界付费补查；其余创建/规划动作不调用模型。它从当前原文发现片假名/技能名候选并结合已有实体，不要求先有同事件匹配。启发式发现可能含普通词，候选不是术语规范。

预检须看两层：`reference_report` 是冻结的候选包，`plan_next.context.edition_reference_selection` 才是经过最终容量检查的入选结果。report 的 `actual_style_document_ids` 给出已保存翻译上下文选入的正文；尚未保存上下文时为 null。上下文可先于 HTTP 请求保存，是否实际发送仍用 `explain` 的 actual-request 确认。发现没有中文正文时先查 omissions，不能把这一轮作为中文 few-shot 效果证据。

## 5. 恢复、增量与交付

```python
with TranslationProject("work/novel") as project:
    with project.open_job("continuation-v2") as job:
        print(job.status())
        result = job.resume(max_chapters=3, materialize=True)
        print(result.to_dict())
        job.export(include_io=True)
```

恢复既有任务不重新准备参考，不重复已成功的模型请求。`max_chapters` 是本次新增提交的上限。`paused_at_limit` 是正常分段执行，`complete` 表示任务窗口全完成；CLI 退出成功不表示翻译窗口完成。

`materialize=True` 从已提交译文构建增量知识；`checkpoint()` 可显式压缩已提交历史，两者可能收费。若 `plan_next()` 返回 `index_required`，先执行 `prepare_history()`：它先尝试直接构建冻结历史 L1，只有容量错误才回退为 L2 逐块抽取和 L1 汇总。后续近处保留 L2，远处按父块汇总 L1，未物化范围暂时保留完整正文；`ensure_details()` 可按需建立指定远距 L2。是否真正发生模型压缩，应根据调用记录判定，不能仅凭存在 checkpoint 对象下结论。

用 `translations()` / export 的已提交内容交付；阻断草稿单独标识。报告完成章数、阻断原因、实际参考覆盖及尚未核验的语义问题。恢复方法见 diagnostics.md。
