# 14 持久化、恢复与导出

[返回目录](index.md) · 上一步：[压缩](13-compaction.md) · 下一步：[Provider 与分发](15-provider-distribution.md)

## 工作区的几种持久数据

```text
workspace/
  project.json                       Edition 注册与依赖
  imports/<edition>/                 来源提取和标准 corpus
  indexes/<identity前24位>/           独立计划、摘要、关联、calls
    locate/<plan前16位>/              续译定位及局部证据
  jobs/<name>/
    job.json / config.redacted.json   冻结身份与脱敏配置
    sequence.sqlite                  分支对象、阶段、提交、账本、outbox
    last-result.json                 最近一次 run 的结果
    export/                          译文、状态及可选 I/O
    io/                              显式 explain 的默认目录
```

标准 corpus 可位于外部 attach 目录。完整备份要包含这些依赖；可先关闭 Project/Job，再复制工作区，避免漏掉活跃 SQLite WAL。导出的 translation.txt 不是可恢复数据库的替代品。

## 请求怎样避免无谓重复

索引 `EditionCaller` 保存 JSON 请求缓存；身份包含任务、prompt、Provider 与预算。成功响应可复用。发起前先写 pending，崩溃后若远端结果未知，会阻断并保留状态，不把“没保存成功”直接等同于“未计费”。结构化结果在限额内复用旧对话并附格式反馈。

连续任务 `SequenceCaller` 在 SQLite 中保存 request、raw response、解析结果和 attempts。先预留成本，再请求并保存原始响应；响应已有而阶段未推进时可从保存数据恢复。逻辑任务共用尝试上限，截断时在容量允许范围内增加输出预留。

两条路径对未知请求状态的处理细节不同；网络超时也不证明远端未完成。因此系统不承诺外部模型调用 exactly once。失败或未知调用保留已预留的 requests/tokens，不自动退回预算以反复重发。

## 提交怎样保持本地一致

不可变对象使用 `kind + 内容哈希` 定址，读取时验证。`commit_chapter` 在 SQLite `BEGIN IMMEDIATE` 事务内检查 prepared/admission/version/review、前驱依赖与 expected head，并以幂等键提交。

同一事务写入译文提交、新 workset、chapter 状态、head 和增量抽取 outbox。事务内崩溃则回滚；事务后但调用方未收到成功时，重放同一幂等提交会返回已有结果。索引物化在 outbox 后执行，不要求模型调用嵌入数据库事务。

这保证本地提交链不出现“译文已推进但没有知识待办”，不保证模型抽取一定成功。译文已提交、尾部索引失败时 `run(materialize=True)` 仍可能返回 blocked；应按 phase 检查，避免误以为译文全丢了。

## 恢复步骤

```python
with TranslationProject("work/novel") as project:
    with project.open_job("window-01") as job:
        print(job.status())
        print(job.draft())
        result = job.resume(max_chapters=2, materialize=True)
        print(result.to_dict())
        job.export(include_io=True)
```

先按失败层级查：配置/网络 → 单次容量 → 格式及段落覆盖 → 证据 → 审校修订 → 增量物化 → 总预算。`open_job` 使用冻结输入，不重新建索引或检索；总预算、尝试与修订计数也不会因重新打开而清零。

修订不足用 [11](11-review-revision.md)的有界接口；累计预算不足没有“删账本恢复”的快捷方法。需要改变语料、模型、参考或协议时新建任务，旧任务保留用于比较；不要不断换名字掩盖未知远端状态。

## 导出什么，怎样判断它是最终结果

`job.translations()` 只读已提交结果，含 source_id、title、commit_id、text、quality_labels、unresolved。`job.draft()` 是阻断草稿，不包含在最终译文列表里。

`job.export(output=None, include_io=False)` 输出 UTF-8 `translation.txt`、`chapters.jsonl` 和 `status.json`；`include_io=True` 为有上下文/提交的章调用 explain，附实际请求、响应及审校/裁决对象。默认每次 run 也刷新译文导出和 last-result。

`job.explain(chapter, output=...)` 是本地诊断导出，按阶段区分未发送 prompt、实际请求、raw-generation、draft 与 commit；文件详见 [09](09-context-evidence.md)。需要追踪多轮工具查询或修订时，顺着导出中的对象 ID 查对应保存对象/账本，不把第一次生成等同于最终版本。

审校/裁决的完整消息和原始输出保存在同一 SQLite `objects` 表：`review-request` 保存 version_id、kind 和 request.messages 等；`review-generation` 保存对应请求及 generation。按目标 version_id 筛选，kind 区分 review、adjudication、revision 等角色。`attempts` 表关联调用状态与 generation_id；工具往返另保留 mvp-tool-turn/result。公共 explain 主要导出这些流程的结果对象，不会自动把每次角色调用都展开为独立 prompt 文件。需要全部角色 I/O 时，使用只读 SQLite 连接按上述对象类型和版本定向提取，禁止手改表或将全库一次性灌入 agent 上下文。这属于底层诊断格式，不是新增公共查询 API。

交付报告应同时说明累计完成、阻断位置、实际参考覆盖、未决质量问题及保存位置。工程 complete 不会自动移除工作稿标签。

## 代码位置

- `src/translation/editions/calls.py`：`EditionCaller.call`、`structured`，JSON 缓存及 pending 行为。
- `src/translation/sequence/requests.py`：`SequenceCaller`，耐久请求、尝试上限及响应恢复。
- `src/translation/narrative/store.py`：`NarrativeStore.put`、`reserve`、`finish`，不可变对象和预算账本。
- `src/translation/sequence/store.py`：`SequenceRepository.commit_chapter`、`switch_checkpoint`，CAS/幂等提交与 outbox。
- `src/lightnovel_translation/job.py`：`run`、`resume`、`translations`、`explain`、`export`。
- `src/translation/sequence/service.py`：`explain_context`，诊断文件生成。
- 回归入口：`tests/translation/test_library_api.py`；分发恢复验证见 `scripts/verify_distribution.py`。
