# 02 输入标准化与结构修复

[返回目录](index.md) · 上一步：[配置](01-configuration.md) · 下一步：[语料身份](03-corpus.md)

## 输入、输出和调用边界

模块把 EPUB/TXT 转换成带来源的标准语料，不调用模型。入口为 `project.import_book(...)` 或 `project.import_edition(InputSpec)`；希望先检查、修复再注册时，使用 `lightnovel_translation.ingestion` 暴露的分步函数。PDF、DOCX、扫描件和 OCR 不属于当前导入器支持范围，需要先生成可追溯的派生 EPUB/TXT。

`import_book` 的多个路径按调用方提供的顺序处理，不自动按卷号排序。需要整理多卷参考书时先用 [参考排序](07-continuation.md)中的 `plan_reference_set`，然后 `import_edition(plan.to_input_spec())`。

## 从文件到标准段落

1. `inspect_input` 检查输入声明、文件和格式。`InputSpec` 绑定 edition、语言、来源列表及段落策略；相对路径由 base 解析。
2. `extract_sources` 保存来源文件快照、哈希、导航和顺序文本块。EPUB 根据 spine 读取资源，解析 nav/NCX 与正文标题；TXT 保存行定位并执行编码、换行策略。
3. `propose_structure` 结合目录锚点、HTML 标题和正文标题样式提出章节边界与条目分类。一份 XHTML 可分多章，一章也可跨多个资源；标题冲突作为诊断保留。
4. `validate_import` 验证来源块覆盖、顺序、边界、标题引用等机械条件。
5. `export_corpus` 按结构产生 entries、paragraphs、provenance 和 manifest；`normalize_input` 封装通常路径。注册后才得到 `Edition`。

清洗去除零宽等不可见字符并保留报告，不直接改原书。ruby 及源定位保留在抽取材料中；空白、标题和段落策略会影响正文输出，因此不能只对比字节长度判断是否有缺页。

## TXT 段落策略

| 策略 | 适用情形 |
|---|---|
| `auto` | 尝试判断自然段、硬换行和排版折行；不确定时检查报告 |
| `blank_line` | 空行分段，段内连续行属于同段 |
| `line` | 每个非空行独立一段 |
| `wrapped` | 同段内部是排版折行，需要合并 |

编码可显式指定；自动路径采用严格识别，不能把乱码当成已正确解码。编码或原始文本布局需要变化时创建新的导入分支，不在已冻结 extraction 上覆盖内容。

## 为什么校验通过仍需抽查

机械校验可以证明已读取块被有序地分配，但无法证明“这个标题真的是新章”、原下载文件没有缺页、某段是广告还是小说。至少抽查开头、末尾、跨文件连接处、超长/超短条目和目录冲突。全本只有一条或某章异常长时，应先修边界再建索引。

遇到歧义先读 [输入修复操作说明](../input-repair.md)，用 `read_source_range` 获取少量块及邻接正文；不必将整本 extraction 送进 agent 上下文。

## 人工或 agent 结构修复如何生效

结构 patch 带 `schema="import-structure-patch-v1"`、`base_structure_revision` 和有理由的 operations。它引用现有完整来源块，不允许编写替换正文。当前支持：

| operation | 作用 |
|---|---|
| `set_boundary` / `remove_boundary` | 在已有块处拆章/合并相邻章 |
| `select_title` | 选择属于该条目的来源块作标题 |
| `classify_range` | 分类正文、附录等条目 |
| `set_paragraph_policy` | 修改指定范围的段落策略 |
| `set_volume` | 记录指定范围的逻辑卷标记 |

`apply_structure_patch` 检查 base revision、防止旧 patch 套到新结构，保存新结构及决策来源，再重新校验、导出到新语料目录。不能通过删 issue、伪造 fingerprint 或改 JSONL 来让校验通过。需要更正源文件时保留原文件，另生成派生文件并重新导入。

## 产物与排错顺序

常规项目路径如下；分步调用允许由调用方指定其他输出目录。

```text
imports/<edition>/extraction/
  input-manifest.json
  sources.jsonl / blocks.jsonl / navigation.jsonl / issues.jsonl
  blobs/                  来源快照
  structure.json / structures/<revision>.json / patches/
  validation.json
imports/<edition>/corpus/
  entries.jsonl / paragraphs.jsonl / provenance.jsonl / issues.jsonl
  manifest.json / report.md
```

先查 extraction 是否正确读出原书，再查 structure 的章界与标题，再查 corpus 的段落。非空且缺少受支持 manifest 的输出目录拒绝覆盖；导出最后写 manifest，避免把半成品当成有效语料。改变结构后，依赖旧 corpus 的索引和任务应重新创建。

## 代码位置

- `src/lightnovel_translation/project.py`：`TranslationProject.import_book`、`import_edition`，导入与项目注册。
- `src/lightnovel_translation/ingestion.py`：面向调用方的分步导入入口。
- `src/translation/ingestion/schemas.py`：`InputSpec`、`SourceSpec`。
- `src/translation/ingestion/service.py`：`extract_sources`、`propose_structure`、`validate_import`、`read_source_range`、`apply_structure_patch`、`normalize_input`、`export_corpus`。
- `src/translation/ingestion/epub.py`、`txt.py`、`structure.py`、`patches.py`、`export.py`：各层解析与结构操作。
- `src/translation/ingestion/util.py`：`normalize_text`、`clean_text_with_report`，清洗和身份辅助函数。
- 回归入口：`tests/translation/test_input_normalization.py`。
