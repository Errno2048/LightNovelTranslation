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

## 2. 阶段 A：选择参考模式

参考模式是独立决策，只声明已有中文材料的证据边界；选择模式本身不会构建模型索引，也不会开始翻译。`mode` 最终随任务冻结，但模式选择、索引准备和翻译仍是三个可分别检查、暂停和恢复的流程。

| 模式 | 何时使用 | 中文的用途 |
|---|---|---|
| source_only | 没有中文参考 | 无 |
| parallel | 已确认目标之前的原译对应 | 确认的双语历史材料；进入任务历史层次索引后按覆盖策略注入 |
| cross_edition | Web/文库、重排、改写或对应未确认 | 文风及有证据的术语候选 |

跨版本不要求匹配成功才允许翻译。没有关联时，会选择所提供中文译本中的正文样本，标记为独立版本。此类样本不能支持 Web 的剧情、人物状态或时间线。需要指定更合适的样本时传 `style_reference_chapters=[...]`，无需伪造剧情关联。

`mode="auto"` 只根据是否传入 `parallel_pairs` / `references` 选择模式，不负责判断历史规模，也不负责决定是否建立索引。`parallel_pairs` 是映射声明，已确认的长程双语历史可以用于任务历史索引；它本身不代表所有章节都会以全文进入翻译上下文。逐章全文、L2 和 L1 的选择由任务历史索引与上下文覆盖策略决定。

本节后续示例假设目标连续窗口 `targets`、中文候选窗口 `reference_window` 和所选模式已由调用者确定；续译任务先完成定位，再确定这些输入。

## 3. 阶段 B：准备或复用索引（与翻译分开）

默认先检查并准备本次有用的索引，再运行翻译；这不等于要求全书全量建索引。可以跳过不需要的新建工作，但要记录跳过范围，并完成本节的预检。索引分两类，互不替代：

1. **独立版本索引**：分别处理日文和中文 Edition，用于跨版本 L2 关联、L1 实体目录和译名检索。`cross_edition` 默认先检查并准备目标附近所需节点；如果只把中文当文风样本且明确选择跳过剧情/术语检索，可以不构建。已有 `parallel_pairs` 时通常不需要为映射另建独立索引；`source_only` 不需要跨版本索引。
2. **任务历史索引**：把本次目标之前的冻结历史组织为远距 L1、近距 L2 和最近正文，用于连续翻译的上下文压缩。它按任务检查，默认先复用现有覆盖或准备缺失节点。创建/打开任务后调用 `job.plan_next()`：若返回 `index_required`，必须先 `job.prepare_history()` 并确认 `historical_index.ready`，再重新规划。只有返回 `ready` 才能进入翻译。

模式选择不会自动构建独立版本索引；独立版本索引也不替代任务历史索引。允许跳过独立版本索引，也允许在 `plan_next()` 已返回 `ready` 时不再物化历史索引。若返回 `index_required`，没有跳过选项，必须完成索引准备；不能直接调用 `run()`。

### 3.1 独立版本索引

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

### 3.2 任务历史索引与预检

历史索引属于具体任务，因此阶段 B 需要冻结任务输入后才能检查；冻结和预检不开始翻译。调用 `create_job` 时要确保 `name`、目标连续窗口、模式和参考材料已经确定。新的任务可在本节准备后关闭，阶段 C 再以同一名称打开。

```python
with project.create_job(
    "continuation-v2", source=ja, chapters=targets,
    mode="cross_edition", references=[zh], index=index,
    associations=prepared["associations"],
    entity_matches=prepared["entity_matches"],
) as job:
    plan = job.plan_next()  # 不调用模型
    if plan["state"] == "index_required":
        prepared_history = job.prepare_history()  # 可能调用模型
        if not prepared_history["historical_index"]["ready"]:
            raise RuntimeError("历史索引未完成；停留在阶段 B")
        plan = job.plan_next()
    if plan["state"] == "index_capacity_decision_required":
        raise RuntimeError("读取 historical_index.planning.diagnostics 后，显式调整分块或选择 rollup 策略")
    if plan["state"] != "ready":
        raise RuntimeError(f"索引/翻译预检未通过：{plan['state']}")
    print(job.reference_report())
    print(plan["context"]["budget"])
```

如果 `plan_next()` 已经返回 `ready`，可复用现有覆盖，或记录本次无需新建任务历史索引。如果返回 `index_required`，不得通过缩小窗口、直接调用 `run()` 或解释成“正文缺失”绕过。如果返回 `index_capacity_decision_required`，先查看 `historical_index.planning.diagnostics`，再显式调整分块参数或选择 rollup；不能把容量诊断当成普通缺索引处理。`run()` 内部的自动准备只是安全兜底，不能替代阶段 B 的显式索引检查。`create_job(..., resolve_terminology=True)` 可能进行独立的有界付费术语查询；应把它作为明确的准备选择。

### 3.3 索引准备的费用和覆盖检查

`index.plan()`、`nodes()`、`select_nodes()`、`read()`、`job.plan_next()` 是本地检查；`index.build()`、`build_parent()`、`prepare_references()`、`job.prepare_history()` 可能调用模型。进入阶段 C 前检查索引返回的状态、覆盖范围、omissions、`historical_index.ready`、参考入选结果和预算。

阶段 B 完成条件是：独立版本索引已按本次用途准备或明确跳过；任务 `plan_next()` 返回 `ready`；预算与参考覆盖检查通过。若用户跳过了索引新建，也必须满足这些条件。续译定位可在目标窗口确定前单独执行，并可能使用独立版本索引；它不会自动开始翻译。

## 4. 阶段 C：运行翻译

本阶段只执行已在阶段 B 冻结并通过预检的任务。若重新打开任务后发现 `index_required`，回到阶段 B 完成索引，再回来运行；不要把这一状态当作可忽略提示。

```python
with project.open_job("continuation-v2") as job:
    plan = job.plan_next()
    if plan["state"] != "ready":
        raise RuntimeError(
            f"任务尚未通过阶段 B；当前状态：{plan['state']}"
        )
    result = job.run(max_chapters=1, materialize=True)
    print(result.to_dict())
    job.export(include_io=True)
```

`materialize=True` 从已提交译文构建增量知识；它发生在翻译之后，不能替代阶段 B 的独立版本索引或任务历史索引。`checkpoint()` 可显式压缩已提交历史，两者可能收费。远处 L2 默认懒构建；只有翻译、审校或显式 `ensure_details()` 请求时才物化。是否真正发生模型压缩，应根据调用记录判定，不能仅凭存在 checkpoint 对象下结论。

阶段 B 的参考检查分三层：`reference_report` 是冻结的候选包，`plan_next` 的 `historical_index` / `context.coverage` 是历史索引覆盖，`plan_next.context.edition_reference_selection` 才是经过最终容量检查的入选中文参考。report 的 `actual_style_document_ids` 给出已保存翻译上下文选入的正文；尚未保存上下文时为 null。上下文可先于 HTTP 请求保存，是否实际发送仍用 `explain` 的 actual-request 确认。发现没有中文正文时先查 omissions，不能把这一轮作为中文 few-shot 效果证据。

### 4.1 窗口并行翻译与批量 materialize

长任务默认按窗口执行，不再每章 materialize：

```python
result = job.translate_window(
    size=None,          # 默认 window_l2_blocks * child_max_chapters，约 30 章
    concurrency=None,   # 默认 translation_concurrency
    materialize=True,   # 窗口提交后统一 materialize
)
```

窗口流程：

1. 按 `window_l2_blocks`（默认 3）和 `child_max_chapters`（默认 10）确定约 30 章的窗口；
2. 从窗口日文和历史实体/译名目录预抽取新专词，冻结窗口 glossary；
3. 同一窗口内的章节并行预翻译，共享冻结 glossary 和窗口开始前的历史上下文；
4. 按阅读顺序逐章审校、裁决、提交；同窗口内后续章节**不会**看到前章新译文；并行审校的 minor confirmed 记入 unresolved 后允许 prepare/commit，仅审校协议/覆盖不完整或 confirmed critical/major 回退到串行重审；
5. 窗口全部提交后再 materialize 一次，更新 L2/L1、实体目录和线程目录；
6. 若 L1 已封口，再执行 L1 批量线程判定；
7. 重建下一窗口的 workset/checkpoint。

约束：

- 窗口内并行翻译只用于草稿；正式译文侧索引只在审校/提交后 materialize；
- 新专词必须有窗口日文证据；已有历史译名不得被模型改译；
- 同窗口译文可能出现术语/语气差异，窗口结束后应做一次一致性检查；
- `translation_concurrency` 受实际模型端点并发和限流约束；
- 提交顺序、L1 汇总、线程目录合并和下一窗口上下文仍必须串行。

## 5. 恢复、增量与交付

```python
with TranslationProject("work/novel") as project:
    with project.open_job("continuation-v2") as job:
        print(job.status())
        plan = job.plan_next()
        if plan["state"] == "index_required":
            prepared = job.prepare_history()
            if not prepared["historical_index"]["ready"]:
                raise RuntimeError("历史索引仍未完成；返回阶段 B 检查")
            plan = job.plan_next()
        if plan["state"] != "ready":
            raise RuntimeError(f"任务未就绪：{plan['state']}")
        result = job.resume(max_chapters=3, materialize=True)
        print(result.to_dict())
        job.export(include_io=True)
```

恢复既有任务不重新准备参考，不重复已成功的模型请求。`max_chapters` 是本次新增提交的上限。`paused_at_limit` 是正常分段执行，`complete` 表示任务窗口全完成；CLI 退出成功不表示翻译窗口完成。

`materialize=True` 从已提交译文构建增量知识；`checkpoint()` 可显式压缩已提交历史，两者可能收费。若 `plan_next()` 返回 `index_required`，先执行 `prepare_history()`：它先尝试直接构建冻结历史 L1，只有容量错误才回退为 L2 逐块抽取和 L1 汇总。后续近处保留 L2，远处按父块汇总 L1，未物化范围暂时保留完整正文；`ensure_details()` 可按需建立指定远距 L2。是否真正发生模型压缩，应根据调用记录判定，不能仅凭存在 checkpoint 对象下结论。

用 `translations()` / export 的已提交内容交付；阻断草稿单独标识。报告完成章数、阻断原因、实际参考覆盖及尚未核验的语义问题。恢复方法见 diagnostics.md。
