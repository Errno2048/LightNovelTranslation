# 03 项目、语料身份与校验缓存

[返回目录](index.md) · 上一步：[标准化](02-ingestion.md) · 下一步：[参考模式](04-reference-modes.md)

## 项目注册什么

`TranslationProject(workspace)` 管理配置、Edition 注册表、索引和任务。`project.json` 记录语料位置与指纹；导入产生标准 corpus，`attach_corpus` 则注册已有标准目录而不复制。项目内部路径尽量相对 workspace 保存，外部目录使用绝对路径；移动工作区时必须同时考虑外部语料依赖。

持久化文件的 schema 是版本兼容标识。不要手工改写 schema；恢复项目与任务时必须保留原标识。

## 三种“章号”不能混用

| 字段或标识 | 含义 | 正确用途 |
|---|---|---|
| `ChapterInfo.id` / entry ID | 标准语料中的稳定章节身份 | API 选择、映射、引用和冻结依赖 |
| `ChapterInfo.position` | `Edition.chapters()` 过滤后列表的 0-based 位置 | 对当前列表切片选连续窗口 |
| `edition_order` | 全部条目的阅读序，包括部分非正文条目 | 比较版本内阅读顺序 |
| 标题中的“第 228 话”、S 编号等 | 原书显示名称 | 人工识别，不直接充当数组下标 |

`Edition.chapters()` 选择有正文、属于叙事且不是 extra 的条目。因此它的列表位置与 corpus 全条目序号、站点话数都可能不同。先检查 `id/title/position`，再用 `chapters[start:stop]`；不要把用户给出的标题数字直接填进下标。

`Edition.read(chapter)` 返回选定章的正文与来源材料，`Edition.info()` 查看注册信息。引用粒度到段落时使用该次上下文注册的段 ID；不要按编号做算术推导另一版本的对应段。

## 指纹与复用

来源快照、结构 revision、语料 manifest、entry/paragraph 内容、索引计划、模型身份与冻结输入分别有哈希或修订信息。它们回答不同问题：

- 原书或结构是否发生变化？看 source hash、structure revision 和 corpus fingerprint。
- 某份摘要基于哪些正文？看 document dependency、节点 revision、子摘要指纹。
- 某份译文基于什么参考？看任务冻结输入、上下文对象、版本及提交链。

不能在相同 ID 下覆盖内容后只重算最外层指纹。已有任务要复现旧输入；修正结构/正文后注册新版本并创建新索引、任务。`attach_corpus` 保留外部依赖，外部文件改动也会使依赖过期。

## 为什么不会每次都完整扫描全书

首次打开 corpus 时，`EditionArchive` 检查 manifest、正文与 provenance 等内容关系。项目进程内随后缓存归档；再次访问时比较注册指纹及核心文件的 size、mtime_ns、ctime_ns 等签名，未变则复用已验证归档。

这减少反复计算全书哈希的成本，不是永久关闭验证。`project.validate_inputs(force=True)` 清除缓存并强制重检。发现外部工具可能改写了语料、文件同步异常或需要交付前核验时使用该入口；正常查询不必反复强检。

## 与任务快照的区别

独立索引在操作前仍确认当前注册语料身份。任务创建时则把目标、历史双语和参考包冻结到任务存储中；`open_job` 恢复原输入，不重新从外部 corpus 选参考。修复书籍输入不会自动修复已经冻结的旧任务。

本模块不调用模型。项目、语料与任务对象支持上下文管理，关闭时释放数据库连接；备份时先关闭活动任务，再复制工作区和需要的外部 corpus。

## 代码位置

- `src/lightnovel_translation/project.py`：`TranslationProject._manifest`、`attach_corpus`、`_archive`、`validate_inputs`，注册与进程内校验缓存；`Edition`，用户视图。
- `src/lightnovel_translation/models.py`：`ChapterInfo`，公开 ID/位置字段。
- `src/translation/editions/archive.py`：`EditionArchive`、`document_dependency`，标准语料读取与内容依赖。
- `src/translation/ingestion/util.py`：`stable_block_id`、`stable_entry_id`，来源身份。
- `src/translation/storage.py`：`digest`、读写与原子文件辅助函数。
- 回归入口：`tests/translation/test_library_api.py`、`test_input_normalization.py`。
