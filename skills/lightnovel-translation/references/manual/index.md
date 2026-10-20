# 工作原理手册：按流程查阅

本手册说明当前 `lightnovel_translation` 的实际运行机制。执行调用先查 [操作流程](../workflows.md)和 [API](../api.md)；需要解释行为、判断故障所在层级或修改模块时，再按下表读取对应页。无需一次读完整本手册。

## 工作流程目录

| 顺序 | 模块 | 何时查阅 |
|---|---|---|
| 00 | [原作检索与采集](00-source-acquisition.md) | 从中文文件反查作者与原作，代理下载、清洗 EPUB 及离线重建 |
| 01 | [配置与容量预算](01-configuration.md) | 设置端点、角色输出、分块参数，解释超限与默认值 |
| 02 | [输入标准化与结构修复](02-ingestion.md) | 导入 EPUB/TXT，处理标题、合章、换行、编码 |
| 03 | [项目、语料身份与校验缓存](03-corpus.md) | 理解章 ID、阅读位置、指纹、外部语料和重复校验 |
| 04 | [参考模式与历史对应](04-reference-modes.md) | 决定严格双语样本还是跨版本参考，处理缺章与重排 |
| 05 | [独立分层索引](05-edition-index.md) | 分别建立日文和中文 L2/L1、理解摘要及实体的覆盖 |
| 06 | [跨版本关联与参考检索](06-reference-retrieval.md) | 关联摘要、匹配译名、补查术语、注入中文正文 |
| 07 | [参考排序与续译定位](07-continuation.md) | 仅有前几卷或中间几卷时，寻找参考结束后的 Web 起点 |
| 08 | [冻结任务与运行规划](08-job-planning.md) | 冻结目标、检查任务历史索引门槛与预算，再运行连续翻译 |
| 09 | [翻译上下文与证据工具](09-context-evidence.md) | 查看完整 prompt 的组成、引用规则、局部证据与主动查证 |
| 10 | [译文协议与段落覆盖](10-translation-output.md) | 解释缺段、合段、格式恢复、逐源段输出的限制 |
| 11 | [审校、裁决与修订](11-review-revision.md) | 理解审校证据、数字信号、阻断、人工修订及接纳条件 |
| 12 | [增量知识与时序信息](12-incremental-knowledge.md) | 理解新译文索引、抽取失败回退、知情状态和时间关系 |
| 13 | [上下文压缩与检查点](13-compaction.md) | 配置 R/U/H、解释何时压缩、哪些材料必须保留 |
| 14 | [持久化、恢复与导出](14-persistence-recovery.md) | 断点续跑、核对成本账本、导出完整输入输出、备份工作区 |
| 15 | [Provider、分发及底层接口边界](15-provider-distribution.md) | 更换模型、自定义 Provider、安装技能、区分公共 API 与底层引擎入口 |

## 一次任务中的数据流

```text
私有配置 + 原书文件
  → 提取来源块 → 结构检查/有版本的 patch → 标准语料 → 注册 Edition
  → 阶段 A：选择参考模式
      parallel：调用方确认的历史对应关系
      cross_edition：两侧独立版本，可做 L2 关联/实体匹配/术语补查
      source_only：仅日文
  → 明确目标阅读窗口（可选续译定位；定位本身可使用独立版本索引）
  → 阶段 B：准备/复用独立版本索引（可跳过）与任务历史索引预检
      index.plan/build/prepare_references
      冻结任务 → job.plan_next → index_required 时 prepare_history → ready
  → 阶段 C：运行已通过预检的翻译任务
  → 段落覆盖/规则检查 → 审校 → 裁决 → 有限修订/复审
  → 原子提交 → 待办队列 → 增量索引 → 检查点/上下文压缩
  → 下一章；随时导出已提交译文、实际请求和证据
```

独立版本索引、任务历史层次索引和译后增量索引是不同产物：独立索引分别理解已有版本，任务历史索引压缩目标之前的冻结历史，增量索引理解本分支已提交的原文和新译文。它们的身份、存储、分块器与可见范围分别受约束；建立其中一种不代表其他索引已就绪。

## 按问题直接跳转

| 问题 | 优先查阅 |
|---|---|
| 配置就绪却无法请求；输入或输出越界 | [01](01-configuration.md)、[15](15-provider-distribution.md) |
| 全本被当成一章；目录缺标题；TXT 每行一段 | [02](02-ingestion.md) |
| 章号与数组位置不一致；语料修改后任务冲突 | [03](03-corpus.md)、[08](08-job-planning.md) |
| 昵称被文库实名覆盖；中文参考没有进入 prompt | [06](06-reference-retrieval.md)、[09](09-context-evidence.md) |
| 69 段只生成 68 段；零问题输出仍被拒绝 | [10](10-translation-output.md)、[11](11-review-revision.md) |
| 术语没抽到；时序记录是否已经过推理验证 | [05](05-edition-index.md)、[12](12-incremental-knowledge.md) |
| 修订预算耗尽；崩溃后是否会重复扣费 | [11](11-review-revision.md)、[14](14-persistence-recovery.md) |
| 想查看翻译、审校、裁决的完整输入输出 | [09](09-context-evidence.md)、[14](14-persistence-recovery.md) |

## 状态与源码位置的读法

`complete` 必须连同所属模块理解：结构完整、索引响应解析完成、任务提交完毕，都不等于语义正确。`candidate_unreviewed`、`provisional_rendering`、`missing_reference`、`partial`、`search_complete=false` 等标签需要继续保留，不能提升为确定事实。

各页末尾列出**仓库相对源码路径 + 类/函数符号**。例如 `src/lightnovel_translation/job.py` 的 `TranslationJob.run`。相对路径从 LightNovelTranslation 仓库根目录计算；安装后可在同名模块中定位。使用符号而非易失效的行号，也避免技能导出后出现指向仓库外的无效链接。只有文档不能解释结果或需要修改实现时，才读取对应位置。

手册的唯一维护位置是 `skills/lightnovel-translation/references/manual/`，会随 wheel 和 `skill export` 分发。配置模板唯一维护于 [assets/config.example.toml](../../assets/config.example.toml)。示例中的 ID 和窗口均需从实际对象获取；不要将占位符当作可引用的证据 ID。
