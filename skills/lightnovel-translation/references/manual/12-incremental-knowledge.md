# 12 增量知识与时序信息

[返回目录](index.md) · 上一步：[审校](11-review-revision.md) · 下一步：[压缩](13-compaction.md)

## 两条抽取路径的区别

| 路径 | 输入 | 挂载位置 | 主要用途 |
|---|---|---|---|
| 独立版本索引 | 单个版本 L2 完整正文 | 独立 L2；L1 汇总子摘要/目录 | 跨版本导航、关联和译名匹配 |
| 连续任务增量知识 | 已提交源文与本分支新译文 | 任务内 sequence-node L2；再汇总 L1 | 后续翻译的术语、剧情、时序及知情候选 |

它们都可同摘要一起提取时序等信息，但不是共享一套权威图谱。普通 `Index` 不会自动返回已经过推理审定的动态关系图。

## 增量节点何时建立

每章提交同时写 `materialize_increment` outbox；正常后续运行对可封闭的节点物化。开放尾部可以先延后，避免每提交一章都重新摘要；`run(materialize=True)` 或检查点流程可强制处理当前尾部。

`current_index` 用 `AdaptiveIndexSpec` 规划本分支可见的历史原译与提交结果。已 sealed 的节点成员保持稳定，只有 open 尾部继续增长；节点记录精确 commit 依赖，另一分支不可悄悄复用不同译文的候选。

自适应规划先考虑完整日中正文，结合章数、输入与输出预算。若要求 L1 直接容纳正文会把 L2 压得过小，或 L1 因输入预算被迫拆分，在配置 child 最小内容量时转为 `summary_rollup`：先保留有用大小的 L2，再以子摘要建立父概括。父摘要的上下文容量仍需预算检查，不是无限层级聚合。

## 联合抽取收到的上下文

单个 L2 的请求包括历史一级导航、同父已存在前驱 L2 摘要，以及当前块完整日文和已提交中文。这里可以从真实双语中提取固定译法，不必从日文猜中文名；但新译文可能有错误，抽取不自动使该译法权威化。

协议记录为 ENTITY、EVENT、STATE、PERSPECTIVE、TEMPORAL、NEED、COVERAGE 等。引用需要绑定当前可见日中来源；译法为 `provisional_rendering`，其他知识多为 `candidate_unreviewed`。实体描述应源于作品证据，不能将通用语言规则伪装成某人物的剧情信息。

## 时间和知情怎样表示

- 叙事披露位置与故事时间分开：`disclosed_in` 在 stable 协议中复制 C 章 ID，`story_time` 描述故事内时点/区间。倒叙可以在后章披露过去发生的事，不必改写阅读序。
- EVENT 描述发生的事，STATE 描述带条件/时间的状态，PERSPECTIVE 区分 knows、believes、claims、suspects、explicitly_ignorant、learns。没有“知道”的记录，不等于明确“不知道”。
- TEMPORAL 引用本次抽取 EVENT ID 描述先后等关系。入库时加节点命名空间，避免多个节点都叫 E001 造成冲突。
- 冲突与重复观察保留为候选；父层汇总不自动把不同时间或不同人物认知合并为当前事实。

这些表示帮助模型避免把“读者已经知道”当成“角色已经知道”，但本身仍是抽取结果，不保证识别叙事诡计或无标注对话说话人。

## 抽取失败如何回退

`ExtractionRecovery` 在最多 3 次尝试内先做严格/容错解析；针对截断在容量上限内增加输出，或重新抽取；必要时执行一次格式转录。转录必须保持原字段载荷一致，不允许新增或修改事实。

失败后保存 attempts、原始响应、缺口与被隔离记录，节点可以是 extracted、partial、summary_only 或 missing 等质量状态。部分坏记录不会因存在摘要就自动进入工作集；失败后也不能把曾需保留的正文当作已经被完整摘要替代。

L1 元信息确定性汇总 L2 records，不执行全局实体/状态真值合并。summary_rollup 需要足够且可用的子摘要才请求父概括，子节点缺失、partial 或覆盖不齐必须显式可见。

## 与 `translation.state` 底层功能的边界

底层另有 StoryTime、TemporalIndex、StateStore、NarrativeState/StateView、知情查询和对话 overlay：可表示部分序关系，检查冲突/环，并按可见范围读取状态。这些不是公共连续主线自动执行的“抽取 → 审核 → 状态图”闭环。

当前 sequence records 不会自动升级成 reviewed state graph，也没有自动维护全书核心人物卡。若需要状态图推理，应独立设计转换、证据审查和授权范围；使用当前公共库时以候选 records、引用和模型主动查证为准。

## 代码位置

- `src/translation/sequence/knowledge.py`：`joint_messages`、`current_index`、`materialize_nodes`、`node_is_valid`。
- `src/translation/narrative/partition.py`：`AdaptiveIndexSpec`、`plan_adaptive_index`，自适应分块和 rollup 选择。
- `src/translation/narrative/extraction.py`：联合抽取协议及引用校验。
- `src/translation/mvp/recovery.py`：`ExtractionRecovery.run`、`reparse`，有界恢复与隔离。
- `src/translation/sequence/context.py`：`_workset`，候选、未决和抽取缺口进入上下文。
- `src/translation/state/schemas.py`、`temporal.py`、`epistemic.py`、`service.py`、`dialogue.py`：底层时序/知情接口，未自动接入公共闭环。
- 回归入口：`tests/translation/test_library_api.py`、`test_reference_workflows.py`。
