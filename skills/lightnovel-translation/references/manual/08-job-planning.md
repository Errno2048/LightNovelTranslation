# 08 冻结任务与运行规划

[返回目录](index.md) · 上一步：[续译定位](07-continuation.md) · 下一步：[上下文与证据](09-context-evidence.md)

## 阶段 B：冻结任务与历史索引预检

参考模式和独立版本索引属于阶段 A/B 的独立步骤；创建任务是阶段 B 为历史索引建立冻结边界的动作，不会开始翻译。不要用 `project.translate(...)` 直接掩盖这些步骤，长历史应先显式调用 `create_job`、`plan_next` 并检查历史索引。

`project.create_job(name, source=..., chapters=..., ...)` 校验连续目标窗口、版本语言和参考模式，构造历史材料及每个目标的参考包，再创建持久分支。冻结内容包括目标正文、历史原译、显式缺口、参考包、关联/译名候选、配置、模型公开身份和 token 计数器身份。

通常创建只执行本地读取和持久化；显式 `resolve_terminology=True` 可能在创建时付费补查。`index.build` 等前置索引操作也是独立的费用边界。使用同一 name 重新创建只适用于冻结身份完全相同的输入；名称不是可覆盖的临时目录名，冲突时应创建新任务。

```python
with project.create_job(
    "window-01", source=ja, chapters=targets,
    mode="cross_edition", references=[zh],
    # 若阶段 B 已构建独立版本索引，再传 index=index、associations=...；否则可跳过。
) as job:
    print(job.status())
    print(job.reference_report())
    plan = job.plan_next()
    if plan["state"] == "index_required":
        prepared = job.prepare_history()
        if not prepared["historical_index"]["ready"]:
            raise RuntimeError("历史索引未完成；停留在阶段 B")
        plan = job.plan_next()
    if plan["state"] != "ready":
        raise RuntimeError(f"翻译预检未通过：{plan['state']}")
```

具体签名见 [API](../api.md)，模式规则见 [04](04-reference-modes.md)。`project.translate(...)` 是创建并运行的便捷方法，适合已知不需要显式索引检查的短任务；它不应替代长任务的阶段 B 预检。

## 本地规划与实际执行的区别

`job.plan_next()` 不调用模型，但可以创建初始本地检查点等元数据。它返回下一目标、上下文及预算、是否需要 checkpoint、哪些增量节点待物化。已有 context 的规划不等于已经发过请求。

采用 `history_index_profile="hierarchy-v2"` 的新任务首次规划可能返回 `state="index_required"`（旧任务的 `hierarchy-v1` 仍可恢复）。这只表示冻结历史的 L1/L2 导航尚未物化，不表示历史剧情缺失；调用 `job.prepare_history()` 后再规划即可。如果 token 预检发现最小 L1 也不能直接容纳，规划会返回 `state="index_capacity_decision_required"` 与诊断，要求先调整容量/下限或显式选择 `child_summary_rollup` / `direct_then_rollup`；此时不会发送模型请求。严格模式下远端单次上下文超限也不会自动建满 L2，需显式选择回退策略。远处 L2 默认懒构建，只有翻译、审校或显式 `ensure_details()` 请求时才物化。

预检时检查当前完整日文、参考入选结果、历史索引状态、`budget.fits` 及 pending materializations。若 `plan_next()` 返回 `state="index_required"`，这是硬门槛：先调用 `job.prepare_history()`，确认 `historical_index.ready` 后重新规划；不能直接运行翻译。若返回 `index_capacity_decision_required`，先读取 `historical_index.planning.diagnostics` 再选择容量策略。若已返回 `ready`，可以记录“本任务跳过了显式历史索引构建”的决定。需要付费的历史抽取或压缩由显式 `prepare_history()`、执行阶段或 `checkpoint()` 完成；不要因为 `plan_next` 便宜就假定接下来的 run 只会调用一次翻译。

## 阶段 C：运行翻译

只有阶段 B 的 `plan_next()` 返回 `ready` 后，才执行 `run()` / `resume()`。如果重新规划又返回 `index_required`，先回到阶段 B 完成 `prepare_history()`，不能直接运行。

```python
with project.open_job("window-01") as job:
    plan = job.plan_next()
    if plan["state"] != "ready":
        raise RuntimeError(f"任务尚未通过阶段 B：{plan['state']}")
    result = job.run(max_chapters=1, materialize=True)
    print(result.to_dict())
```

`max_chapters` 限制本次新增提交数；恢复后可以继续下一个窗口。`materialize=True` 在本次运行末尾还会封口抽取增量尾部，可能产生额外模型调用，甚至在译文已提交后因索引失败返回 blocked。连续任务的提交译文使用独立的 append-only `sequence-node` 增量索引；检查点按统一覆盖账本选择最近完整双语、邻近 L2 和更远 L1，未物化范围保留正文回退，不会误报为 `explicit_gap`。

## 每章状态如何推进

```text
前次已提交知识的待办物化
  → 检查预算/必要时建立检查点
  → 冻结本章上下文 → 翻译原始响应 → 译文版本
  → 段落覆盖 → 规则检查 → 审校 → 裁决
  → 如需修订：新版本 → 重新覆盖/检查/审校/裁决
  → 准备接纳 → 原子提交译文、工作集和索引待办
```

每个阶段保存对象，模型请求也有耐久账本。blocked 会保留当前版本、响应和失败位置；再次 `resume()` 复用可复用的成功结果，不需要重建任务。

## 运行结果与草稿

| 字段/状态 | 含义 |
|---|---|
| `complete` | 此任务冻结窗口全部完成工程提交 |
| `paused_at_limit` | 达到本次 max_chapters，正常暂停 |
| `blocked` | 有需处理的请求、审校、预算或物化问题 |
| `RunResult.completed` | 本次运行新提交的源章 ID |
| `RunResult.committed_count` | 此任务累计已提交数 |
| `RunResult.ok` | complete/paused_at_limit 为 true；不表示语义正确 |

`on_event` 回调提供 chapter_started、chapter_committed、chapter_blocked，可用于进度显示。`job.draft()` 是当前阻断草稿及审校/错误信息；`job.translations()` 只返回已提交译文并保留质量标签与未决事项。

## 恢复与新任务的选择

`project.open_job(name)` 恢复冻结任务，不重做参考准备或术语补查。模型/计数器身份变化会被拒绝。网络或格式故障通常保留原任务诊断与恢复；修正输入、换模型、增加新的参考材料或改变 prompt 协议应新建任务，不能悄悄改变旧请求。

原任务恢复的修订额度扩展和 agent 段落修订属于有记录的专门操作，见 [11](11-review-revision.md)。公共 API 没有把任意已提交章直接覆盖成另一译文的入口；底层分支机制见 [15](15-provider-distribution.md)。

## 代码位置

- `src/lightnovel_translation/project.py`：`create_job`、`open_job`、`translate`。
- `src/lightnovel_translation/job.py`：`create_job`、`TranslationJob.plan_next`、`run`、`resume`、`status`、`draft`。
- `src/translation/sequence/service.py`：`SequenceEngine.plan_next`、`advance_sequence`，阶段编排。
- `src/translation/sequence/store.py`：`create_sequence`、`SequenceRepository`，冻结分支及提交。
- `src/lightnovel_translation/models.py`：`RunResult`。
- 回归入口：`tests/translation/test_library_api.py`。
