---
name: lightnovel-translation
description: 使用 lightnovel_translation 库标准化 EPUB/TXT 轻小说、建立日中参考索引、执行与恢复翻译审校任务，并修复自动管线无法判定的章节边界、标题、编码或段落结构。适用于有本地小说文件的翻译与输入修复任务。
---

# Light Novel Translation

使用 Python >= 3.11 的 `lightnovel_translation`，安装名为 `lightnovel-translation`。优先调用公共 API，保留来源、冻结任务和已付费响应。

## 先查文档

执行翻译任务时，先阅读 [操作流程](references/workflows.md)，再按需要查 [API](references/api.md)。阻断、参考缺失或预算问题查 [诊断与恢复](references/diagnostics.md)；输入结构异常查 [输入修复](references/input-repair.md)。这些说明随安装包分发，可在安装环境中查阅。

需要理解内部工作机制时，从 [工作原理手册目录](references/manual/index.md)按流程选择模块：配置与容量、标准化、参考模式、独立索引、跨版检索、续译定位、任务、上下文、输出、审校、增量知识、压缩与恢复。每页附源码位置；先查对应说明，不一次读完整本手册。

优先使用文档示例、公共 API 和结构化诊断完成任务。只有文档缺少必要信息、公开 API 返回无法解释的结果，或任务本身要求修改库时，再定位对应源码。不要为查参数遍历整个引擎。

## 配置与凭据

配置空模板见 [assets/config.example.toml](assets/config.example.toml)。首次执行 `python -m lightnovel_translation config init`，只创建空模板，不覆盖已有文件。默认路径为 `~/.lightnovel_translation/config.toml`；用户可通过 `LIGHTNOVEL_TRANSLATION_HOME` 指定其他目录。

让用户在本机编辑该文件，填写 `model`、`base_url`、`api_key` 和实际模型容量。告诉用户文件路径和需填的字段；不要要求用户在对话中提供 key。用户已告知配置完成时直接继续验证，无需反复要求确认。

Agent 不使用 cat/Get-Content、搜索、截图、工具输出或附件读取凭据文件，也不把配置文件复制到工作区、Git、prompt 或报告。库在进程内部读取它即可。只运行：

```console
python -m lightnovel_translation config check
```

它仅返回路径、就绪状态和缺失字段名。缺失配置时仍可完成导入、结构检查和本地规划；需要模型的部分待用户本地填写后续跑。不要读取浏览器凭据或其他项目的密钥来自动填充配置。

`TranslationProject(workspace)` 自动加载上述用户配置。显式传入 `TranslationConfig()` 可做完全无模型的本地工作。不要打印 ModelConfig 的 `__dict__` / dataclasses.asdict，也不要向用户请求上传完整配置；`user_config_status()` 是配置排查入口。

## 工作路径

需要先从中文译本定位或下载 Syosetu 日文原作时，使用随包独立技能 `syosetu-fetch`（`skill export <目录> --name syosetu-fetch`）及 `lightnovel_translation.syosetu` 公共 API。先核对中文头部作者，再检索候选；下载返回的 `epub_path` 可直接作为下述输入。没有采集需求时跳过此步。

先确定用户输入、需要翻译的阅读区间、已有译文版本关系和输出位置。能从本地文件与上下文判断的就继续处理；只有无法判定的歧义才询问用户。读取少量标题和正文确认阅读顺序，不能把标题中的数字直接当作数组下标。

1. 用 `import_book` / `import_edition` 导入。即使机械校验通过，也抽查开头、结尾、文件连接处和可疑超长条目，确认没有多个章节被当作一个章节。
2. **阶段 A：选择参考模式。** 只决定已有中文材料可以证明什么；模式选择本身不构建索引、不翻译。选择 `source_only`、`parallel` 或 `cross_edition`。没有经过确认的双语映射时，不按两侧序号硬配。`parallel_pairs` 表示确认过的历史映射；它不意味着所有对应章节都会以全文进入翻译 prompt。跨版本索引是后续独立流程；文库不决定 Web 事实。
3. **阶段 B：准备索引，默认优先执行但允许跳过。** 先检查并复用已有索引，再只构建本次需要的范围；不要求默认全书抽取。
   - `cross_edition` 若要使用剧情关联、实体目录或译名检索，先调用 `project.index()` 做本地计划，再按需显式调用 `index.build()` / `build_parent()` / `prepare_references()`。只用文风样本时可以明确跳过独立版本索引；`parallel` 映射和 `source_only` 也不自动要求独立版本索引。
   - 连续任务的前置历史由任务历史层次索引管理。冻结任务后在阶段 B 调用 `job.plan_next()`，检查 `reference_report()`、索引覆盖和预算。若返回 `state == "index_required"`，必须先调用 `job.prepare_history()`，确认 `historical_index.ready`，重新规划得到 `ready` 后才能进入翻译；不得把它当作可忽略提示。若返回 `index_capacity_decision_required`，先查 `historical_index.planning.diagnostics`，再显式调整分块参数或选择 rollup 策略。`run()` 会自动准备索引作为兜底，但不能替代 agent 显式检查。
   - 用户选择跳过索引时，独立版本索引可不构建；任务历史索引只有在 `plan_next()` 已返回 `ready`、预算与覆盖通过时才可跳过物化。若返回 `index_required`，必须准备索引。
4. **阶段 C：运行翻译。** 只对阶段 B 已冻结且预检为 `ready` 的任务调用 `job.run()` / `resume()`。若重新规划时发现 `index_required`，回到阶段 B；不要直接翻译。`run(materialize=True)` 的增量索引发生在已提交译文之后，不能替代阶段 B 的索引。blocked 时保留工作区，先检查状态、`draft()` 与该章 `explain()`，不要不断换任务名或删数据库来重试。
5. 修订机会用尽且仍在用户已授权的翻译范围内时，可说明理由，通过 `extend_revision_budget()` 追加有界机会后恢复；明确的局部错误可由 agent 用 `submit_revision()` 提交新版本，再 `resume()` 完成复审。两条路径都保留账本与旧证据，不能直接把草稿标成通过。具体上限、并发及失败分类见诊断文档。新索引材料、修正语料、模型身份改变仍需新任务。
6. 用 `export(include_io=True)` 保留译文、实际请求、原始模型输出和审校证据。报告完成章数、阻断位置和未决质量问题；工程 complete 不等于语义正确或人工接受。

基本调用示例及参考模式见 [API 与运行恢复](references/api.md)。遇到格式、漏章、分块异常、解码或结构问题时读 [输入检查与人工修复](references/input-repair.md)，按问题层级处理。

## 保留的约束

- 不改原书、来源快照、已有导出或已冻结的任务。结构变化使用有版本约束的 patch；需要改解码 / 源文本布局时生成新导入分支或派生文件。
- 不删除错误标记、重算伪造指纹、添加空译文、重复段落或凭剧情补齐缺页来通过验证。
- `parallel_pairs` 只给目标之前的明确历史对应；目标译文和未来参考答案不能混入该模式。
- `cross_edition` 的译名与事件关联都是候选证据，参考版剧情差异不构成 Web 错译。没有选到中文证据时检查上下文，不默认关联已经成功。
- LLM 摘要 / 实体不是权威事实。缺引用、partial、未决意见需要保留；能定位到段落不表示该意见正确。
- 请求、token、尝试上限保留持久记账。模型失败先定位是配置、容量、格式、出处还是语义；同一付费响应应复用，未知远端完成状态不可直接重发。
- 修复工具覆盖不了的阅读顺序、正文缺失或 OCR 歧义，要给用户准确的文件/位置和可选处理方式，继续完成不依赖该歧义的工作。
