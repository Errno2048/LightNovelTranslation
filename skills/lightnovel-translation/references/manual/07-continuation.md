# 07 参考排序与续译定位

[返回目录](index.md) · 上一步：[参考检索](06-reference-retrieval.md) · 下一步：[冻结任务](08-job-planning.md)

## 先排序，再判断“最后已有译文”

`plan_reference_set(paths, edition_id=..., language="zh", order="auto")` 是纯本地操作。它综合文件名、EPUB package title 与开头目录标签识别卷号、上下册和外传；按 SHA256 去除完全相同的文件。混系列、卷号证据冲突或无法确定顺序时标记 `needs_resolution`，`to_input_spec()` 会拒绝导入含歧义的计划。

`overrides` 可按 resolved path 或唯一文件名指定 `volume_ordinal`、`part_ordinal`、`volume_kind`、`reading_order`。`order="provided"` 显式保留给定顺序。需要改顺序时改计划，不直接交换 corpus 行。

可用范围保留为 prefix/window/gapped/unknown；只有 7–9 卷就是 window，不会伪装成从第 1 卷开始的全集。`unavailable_prefix` 与 `internal_gaps` 帮助区分前卷未提供和中间缺卷。这些是文件可用性信息，不是剧情覆盖证明。

## 入口与支持范围

当前定位支持一个日文源 Edition 和一个中文参考 Edition，目标为 `after_latest_reference`、端点为 `latest_mainline`。先用与导入一致的 ReferenceSetPlan 创建本地计划，再调用有界模型搜索：

```python
plan = index.plan_continuation(
    source=ja, references=[zh], reference_set=ordered,
)
# 检查计划；下行可能构建局部摘要并调用模型
result = index.locate_continuation(
    plan, max_new_nodes=20, max_requests=30, max_rounds=4,
)
```

传入计划的内容、顺序及 source_ranges 必须与已导入参考一致。定位不是翻译，得到结果后仍需选择实际源章并创建任务；不能凭结果中的候选编号直接修改已有 job。

## 搜索怎样逐步缩小范围

1. 从最后可用参考卷找叙事尾部，排除明确书名页、后记等；“终章”仍可为正文，不按标题词一概删除。
2. 读取最后参考卷最多两个尾部 L2 范围，建立端点剧情概括。
3. 向模型提供全 Web 标题目录导航，先召回少量候选位置；标题含时序线索，但不单独决定结论。
4. 获取候选 L2 概括，再读取候选章全文、引用窗口与参考真实尾部，按需要发起 REQUEST 获取更多局部材料。
5. 比较主线事件、分支状态、并行叙事、回忆/预告与改编分歧，给出候选起点、推荐起点及证据。

局部导航摘要不自动变成正常完整索引；读取过标题或短摘要也不能等价于读过候选全文。定位有独立搜索轮数、新节点数和请求限制，不能以无限增大候选集合替代证据核对。

## 起点和状态怎样解读

| 状态 | 含义 |
|---|---|
| `suggested` | 有模型建议；仍需查看证据与自动适用条件 |
| `ambiguous` | 多个起点或叙事线尚不能排除 |
| `diverged` | 已看到版本剧情分歧，不能直接等价接续 |
| `insufficient_evidence` | 证据/窗口/预算不足 |
| `no_remaining_source` | 未发现可继续的源正文，仍受本次搜索边界约束 |

`candidate_start` 不一定是 `recommended_start`。混合覆盖章从整章开头继续，允许部分重复，避免猜测精确段内断点。不能把所有命中章号取最大值再加一；双线叙事和倒叙会使该规则跳过内容。

`auto_eligible` 要求无搜索限额命中、无截断修补、推荐源章全文已读，且有至少两组不同的日中 shared-current 证据、状态兼容、多线检查完成、无缺失上下文或冲突等条件。格式修复成功不自动恢复自动资格。结果仍标记 `model_suggested_unreviewed`、`search_complete=false`，不构成人工质量保证。

## 保存结果与排错

结果保存到索引目录下 `locate/<计划哈希前16位>/`，包括 `plan.json`、`calls/`、`summaries/`、`result.json`。检查实际尾部选择、候选全文覆盖、REQUEST 是否满足和停止原因，再决定扩大局部范围还是保留歧义。

前 1–3 卷与仅 7–9 卷都从最后**已提供**主线参考的尾部定位；后者不会额外假定 1–6 卷可用。排序错误需要重新整理并导入，新定位计划不能套在旧顺序 corpus 上。

## 代码位置

- `src/translation/ingestion/reference.py`：`ReferenceSetPlan`、`plan_reference_set`，排序证据、覆盖范围与 override。
- `src/lightnovel_translation/index.py`：`Index.plan_continuation`、`locate_continuation`，公共入口。
- `src/lightnovel_translation/continuation.py`：`plan_continuation`、`locate_continuation`，端点、逐步搜索、证据绑定及自动适用门槛。
- `src/translation/editions/archive.py`：来源内容与阅读范围校验。
- 回归入口：`tests/translation/test_continuation.py`。
