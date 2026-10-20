# 公共 API 与可恢复运行

完整操作步骤见 [workflows.md](workflows.md)，预算、参考缺失和接管见 [diagnostics.md](diagnostics.md)。本页描述 API 约定。

安装：`python -m pip install lightnovel-translation`（已发布时），或使用用户给出的本地项目 / wheel。不要为安装去读取任何凭据。选定实际安装该包的解释器，用同一解释器运行后续命令。

## 从输入到翻译

```python
from lightnovel_translation import TranslationProject

with TranslationProject("work/novel") as project:  # 默认读取用户私有配置
    source = project.import_book("book.epub", edition_id="web-ja", language="ja")
    chapters = source.chapters()
    for c in chapters[:5]:
        print(c.to_dict())
    # 示例只有前三个叙事章；以用户指定范围为准。
    with project.create_job("opening", source=source, chapters=chapters[:3],
                            mode="source_only") as job:
        plan = job.plan_next()
        if plan["state"] == "index_required":
            prepared = job.prepare_history()
            if not prepared["historical_index"]["ready"]:
                raise RuntimeError("history index is incomplete")
            plan = job.plan_next()
        if plan["state"] != "ready":
            raise RuntimeError(f"translation preflight is not ready: {plan['state']}")
        print(plan["context"]["budget"])
        result = job.run(max_chapters=1)
        print(result.to_dict())
        job.export(include_io=True)
```

`ChapterInfo.position` 是 0 基叙事章位置，`edition_order` 是保留其他条目的原生阅读序位，标题章号可能另有含义。下游只用返回的 `id` / ChapterInfo 引用。不要从 C 编号反推出输入书籍的标题号。

`TranslationConfig()` 是显式无模型配置；`TranslationConfig.from_user_config()` 明确加载默认文件。默认构造 Project 使用用户文件（存在时）；没有配置仍可导入、规划。`config init` 不会复制旧工程中的 key。

## 索引与参考模式

参考模式、索引准备和翻译是三个独立阶段：

1. 选择 `source_only` / `parallel` / `cross_edition`，只决定参考材料的证据边界。
2. 默认先准备或复用所需索引；按需建立独立版本索引，并在任务冻结后的预检中处理历史层次索引。允许跳过构建，但 `index_required` 不能跳过。
3. 只有 `job.plan_next()` 返回 `ready` 且预算与覆盖通过后，才调用 `run()` / `resume()`。

`mode="auto"` 只选择参考模式，不会自动生成独立索引摘要或进行跨版关联。`create_job()` 可能创建本地索引计划，这不等于已调用模型完成构建。长历史下不要因为存在 `parallel_pairs` 就跳过索引判断。

```python
index = project.index([source])
nodes = index.nodes(edition=source.id, level=2)
print(index.plan())                           # 本地
print(index.read(nodes[0]["node_id"]))       # 本地，不隐式补建
built = index.build([nodes[0]["node_id"]])    # 可收费
```

L2 按 token/章节上限装箱，L1 默认先直接读取完整父块。严格计划发现最小 L1 不可容纳时会先返回容量决策状态；远端单次上下文超限也不会隐式建 L2。显式 `build_parent(parent_id, method='direct_then_rollup')` 或 `method='child_summary_rollup'` 才启用二次摘要。`build(..., workers=N)` 在同一 L1 内串行，不同 L1 间并发；不要另开并发写入同一 Job。`include_parents=True` 只按节点计划的明确方法处理涉及的父节点，只有完整覆盖才报告 `complete=True`。`build_parent(parent_id, allow_partial=True)` 可显式保存部分汇总，但必须报告缺失覆盖。

三种模式：

- `source_only`：无既有中文参考，使用源侧可见索引和本分支生成历史。
- `parallel`：调用者确认历史映射，给 `references=[zh]`、`parallel_pairs={ja_id: [zh_id, ...]}`。每个日文历史章可对应多个中文文件，不能重用一个中文文档绑定两个源章，不能包含目标或未来源章。
- `cross_edition`：中文作为独立版本参考。若需要区块关联，显式构建选定的两侧 L2，再 `association = index.associate(ja_node_ids, zh_node_ids)`；创建任务传入 `references=[zh]`、`index=index`、`associations=[association]`。可继续 `match_entities(ja_parent, zh_parents)`，通过 `entity_matches=[artifact]` 注入。也可跳过独立索引，只传 `references=[zh]` 使用文风样本。传入 index 必须刚好包括本次 source 和 references。

`mode='auto'` 仅根据 pairs/references 是否存在选择路径，不能诊断两个版本确实等价。只有标题或词语相似时不要假定剧情匹配。关联候选可以判无关；没有证据时仍按原文翻译。只传 `references` 会选择所提供中文正文中的少量文风样本，不会自动建立全书双语映射或付费抽取。

`index.select_nodes(chapters, neighbors=0)` 在原生顺序中选择包含稳定章 ID 的 L2。`index.prepare_references(source_chapters, reference_chapters, workers=1, batch_size=20)` 显式执行局部建索引、区块关联、实体匹配，返回 status/build/associations/entity_matches；可能收费。`index.resolve_terminology(chapter, source_forms=(), associations=(), entity_matches=())` 可直接对未建索引的中文正文进行有界补查。

`create_job(..., style_reference_chapters=[...], packet_token_budget=None)` 可指定中文样本，预算默认从配置容量推导。跨版本中文正文标明只用于文风与有证据的术语，不能覆盖 Web 事实。`resolve_terminology=True` 会在创建时付费补查；启发式发现的名称可能包含普通词，结果仍是候选。

`job.reference_report()` 不调用模型，报告冻结候选与已保存请求的实际 selection；未运行章的 actual_style_document_ids 为 null，先看 plan_next 的 selection。`project.validate_inputs(force=True)` 显式完整重校验；日常只读调用复用文件属性未变化的会话内快照。

任务历史索引的硬门槛：

```python
plan = job.plan_next()
if plan["state"] == "index_required":
    ready = job.prepare_history()
    if not ready["historical_index"]["ready"]:
        raise RuntimeError("history index is incomplete")
    plan = job.plan_next()
if plan["state"] != "ready":
    raise RuntimeError(f"translation preflight is not ready: {plan['state']}")
```

不希望主动建立索引时仍可先调用 `plan_next()`；只有它已经返回 `ready` 才能继续。如果返回 `index_required`，必须完成 `prepare_history()`。`run()` 内部的自动准备是安全兜底，不应替代 agent 对索引覆盖和费用边界的检查。

## 从部分中文卷定位续译起点

`plan_reference_set(paths, edition_id=..., language="zh")` 是本地排序接口，也可从模块根导入。只给用户本次提供的文件；检查 `to_dict()` 的顺序证据和冲突，调用 `to_input_spec()` 后导入。7–9 卷的真实卷号保持为 7–9，缺少 1–6 卷不自动要求补译。Ex 不自动当成最新主线卷。卷号冲突可通过显式 `overrides` 修正，文件内部章节乱序仍走输入修复流程。

```python
from lightnovel_translation import plan_reference_set

refs = plan_reference_set(paths, edition_id="print-subset", language="zh")
zh = project.import_edition(refs.to_input_spec())
index = project.index([source, zh])
plan = index.plan_continuation(source=source, references=[zh], reference_set=refs)  # 本地
result = index.locate_continuation(plan, max_new_nodes=20, max_requests=30)        # 可能收费
```

检查 `status`、`recommended_start`、`candidate_start`、`last_shared_anchors`、`boundary` 和 `search_coverage`。只有正文引文可绑定不代表语义正确；目录召回没有穷尽全书。`suggested` 仍为待审建议，`auto_eligible=False` 时不得直接采用。预算不足时可能返回 `insufficient_evidence`，但保留已核验的 candidate、锚点和待办请求。不能把最大匹配章号加一作为答案。`artifact_dir/calls` 保存完整 I/O，恢复同一计划复用已付费结果；未知远端状态的 pending 请求不重发。

定位成功后按稳定 chapter ID 选择现有 `source.chapters()` 中的连续窗口，再显式创建翻译任务；定位不会自动开始翻译，也不会自动生成可靠译名对应或跨版语言参考。当前支持一个已整理的中文参考版本；本次未提供的中文卷及其摘要不能混入上下文。

## 恢复与上下文压缩

```python
with TranslationProject("work/novel") as project:
    with project.open_job("opening") as job:
        print(job.status())
        plan = job.plan_next()       # 不触发模型，可能写本地 checkpoint 元数据
        if plan["state"] == "index_required":
            prepared = job.prepare_history()
            if not prepared["historical_index"]["ready"]:
                raise RuntimeError("history index is incomplete")
            plan = job.plan_next()
        if plan["state"] not in {"ready", "complete"}:
            raise RuntimeError(f"translation preflight is not ready: {plan['state']}")
        result = job.resume()        # 只运行尚未完成的部分
        print(result.to_dict())
```

`run(max_chapters=N)` 表示本次最多新提交 N 章。`paused_at_limit` 是正常暂停，`complete` 才表示冻结窗口全部执行完成。`blocked` 不是成功，error 给出原因，已经生成和提交的内容仍在 SQLite 中。

层次历史任务首次 `plan_next()` 可能返回 `state="index_required"`；这是本地导航待物化状态，使用 `job.prepare_history()`，不要把它当作正文缺失。`job.ensure_details([node_id, ...])` 只构建明确请求的 L2 细节，旧检查点保持可追溯。父级 L1 默认直接读取整块原文；只有显式选择容量回退时才采用 L2 rollup。若 `plan_next()` 返回 `state="index_capacity_decision_required"`，读取 `historical_index.planning.diagnostics` 并显式调整分块范围或选择 rollup 策略；规划阶段没有发起模型请求。

`job.entity_catalog()` 返回完整合并目录的紧凑条目、核心 ID 与计数，不附带成员列表、章节来源清单或原文摘录；需要查证时以稳定实体 ID 调用 `job.entity_evidence(entity_id)`，它只返回该实体的出处。

`checkpoint()` 显式压缩已提交前文；`run(materialize=True)` 还会构建已提交尾部的增量知识，两者都可能收费。检查点上下文采用最近完整双语、邻近 L2、远距 L1 的非重叠覆盖；未建 L2 只表示待物化，不表示 `explicit_gap`。所有 index / job 分别有持久预算，不是整个 Project 的总额度。

模型 / tokenizer 身份变化不能直接恢复旧任务；语料修复、重新选参考或配置策略改变不会改写旧任务。需要新任务以冻结新输入。不要修改 SQLite 让原任务看似接受了新配置。

当前草稿可用 `job.draft()` 读取版本 ID、段落 alias、修订轮数和既有 review。`extend_revision_budget(additional_rounds=1, reason=...)` 在 review_blocked 草稿上追加可追溯修订额度，不改冻结 spec、不重置持久调用预算，上限 8。`submit_revision({alias: text}, expected_version_id=..., reason=...)` 生成新版本并保留历史未决问题，后续 resume 必须复审；它要求已有完整审校和剩余修订额度。详见 diagnostics.md。

## 阻断定位与交付

`job.explain(chapter_id)` 或 `export(include_io=True)` 导出完整 messages、实际请求、原始响应、覆盖、审校、裁决与译文；读取这些工作产物时无需打开用户配置文件。

- `configuration_incomplete` / 配置检查未就绪：指引用户本地修改配置，只重跑脱敏检查。
- 预算不足：检查目标全文和必要上下文，配置真实容量或减少可选资料；更改冻结策略要新任务。不要虚报容量使检查通过。
- 输出格式 / 段落覆盖失败：查看已付费响应和解析错误；保留协议重试上限，不能靠空白或重复段落补数。
- 摘要 / 术语缺证据：补建真正相关的节点、查实际正文；没有原文支持就保留未决。
- 语义疑点：结合 Web 实际原段和允许前文判断，跨版本文库事实不能覆盖 Web。

交付使用 `job.translations()` 与 export 的已提交内容，说明状态和未决问题。不要把未提交草稿混作完成章节；用户需要诊断时可以单独给草稿及失败原因。
