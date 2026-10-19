# 05 独立分层索引

[返回目录](index.md) · 上一步：[参考模式](04-reference-modes.md) · 下一步：[跨版本检索](06-reference-retrieval.md)

## 入口和费用边界

`index = project.index([ja, zh])` 为各版本分别规划分层节点并保存本地元数据，不调用模型。`plan()`、`nodes()`、`select_nodes()`、`read()`、`entity_catalog()` 只规划或读取；`build()`、`build_parent()` 会在需要时发起模型请求。读取缺失节点不会偷偷付费构建。

```python
index = project.index([ja, zh])
nodes = index.select_nodes(targets, neighbors=1)
print(index.plan())
# 下行可能调用模型；只构建显式选择的 L2
result = index.build([n["node_id"] for n in nodes], workers=2)
```

## 分块如何确定

`EditionIndexSpec` 综合模型可用输入、目标内容 tokens、每块最大章数，以及文档/逻辑卷边界打包 L2；再把连续子块组织为 L1。章数是上限，不保证每个节点恰好 10/100 章。token 规划保留前驱导航与协议空间，最终请求再次检查。

独立索引的 L1 从子摘要汇总，不要求 100 章正文都放进单次请求，因此不会为了 L1 容量强迫 L2 缩成单章。最小 L2 内容量只能尽量满足，不能跨硬边界或超限。单章本身超限时必须先在输入结构层拆分，当前不会自动把正文任意截成可用章节。

同一 L1 下的 L2 按顺序串行构建，可利用最多两个已经存在的前驱 L2 概括；不同 L1 可通过 `workers` 并发。局部构建不会隐式补建所有前驱，邻接导航不足会影响理解，但仍保留真实覆盖。

## L2 抽取实际收到什么

单次摘要收到**当前这一版本**节点的完整正文、标题、阅读顺序，以及已有前驱导航。日文节点不会自动附上中文对照；跨版译名由下一阶段的实体匹配/正文补查解决。连续翻译中的双语联合抽取属于 [12](12-incremental-knowledge.md)的另一条路径。

输出使用平面记录协议：`@@OVERVIEW`、`@@EVENT`、`@@ENTITY`、`@@TEMPORAL`、`@@KNOWLEDGE`、`@@COVERAGE` 和 `@@END`。记录按模块解析，避免依赖一整个巨大 JSON 的括号匹配。

| 信息 | 保存的内容与用途 |
|---|---|
| 整体摘要 | 当前块剧情、因果、人物行为；2500 字是 prompt 软目标，超长不直接丢弃 |
| 事件 | 块内重要事件、范围和引用；用于关联与导航 |
| 实体 | 原表记、类别、作品内描述和证据；区分技能名称与等级 |
| 时序/知情声明 | 随摘要一起提取的候选关系；不是自动审定的时序图 |
| 覆盖 | 模型报告覆盖与缺口，和解析器机械校验共同记录 |

prompt 中事件数量、实体数量的指导不代表穷尽全块；没有抽到实体不等于它不存在。尤其技能列表、昵称、多线叙事应通过后续定向补查补足，不直接将摘要当作完整事实库。

## 引用与解析的约束

实体 form 要逐字出现在绑定证据段中，不能凭摘要自行补全实名。一般引用匹配不到时可保留 `missing_reference` 等 warning，让摘要继续承担导航功能；不能把缺证据记录当成已绑定事实。结构 complete、引用 bound 与语义正确是三个不同层级。

格式失败保留原始响应及诊断，在累计预算和 `max_attempts` 内恢复。局部容错处理协议变体，不补造剧情。模型输出过长、缺 END、截断、真实证据不符，应分别定位，不能统称“模型不遵循 JSON”。

## L1 汇总与实体目录

`build_parent(parent_id)` 只使用已经构建的子摘要，不隐式抽取缺失子块。默认子节点未齐返回 not_built；只有显式 `allow_partial=True` 才允许部分汇总。`build(..., include_parents=True)` 也不会为未选择的其余子块补建索引。

L1 保存整体摘要、子摘要指纹和实体目录。目录从已建 L2 确定性汇总，`entity_catalog()` 同时报告 covered/missing 子节点；它不需要先有完整 L1 概括，也不是全书权威术语表。子摘要发生变化后，旧 L1 指纹不匹配会被视为过期。

产物位于 `indexes/<identity前24位>/`，含 `index.json`、计划、摘要、parents、关联和 `calls/` 等文件。身份包含正文依赖、分块计划、模型与预算，成功请求可复用；未知远端状态见 [14](14-persistence-recovery.md)。

## 代码位置

- `src/lightnovel_translation/index.py`：`Index`，公共节点选择、构建、读取入口。
- `src/translation/editions/index.py`：`EditionIndexSpec`、`plan_edition_index`，独立分块。
- `src/translation/editions/service.py`：`EditionPipeline.ensure_index_nodes`、`build_parent`、`entity_catalog`。
- `src/translation/editions/prompts.py`、`protocol.py`：单版本摘要输入和记录校验。
- `src/translation/editions/calls.py`：`EditionCaller`，调用缓存、预算和格式反馈重试。
- 回归入口：`tests/translation/test_reference_workflows.py`、`test_library_api.py`。
