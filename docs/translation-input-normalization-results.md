# 上游格式标准化：首批实施与验证

日期：2026-10-19。基线：`21a9376` 的格式标准化设计。当前完成一个无需 LLM 的 EPUB/TXT 导入闭环；完整目标见[设计文档](translation-input-normalization-design.md)，后续预算视图和两条翻译路径的完整验收仍未完成。

## 1. 已实现的流程

```text
InputSpec → 来源字节快照、文本块、导航
          → 保守章节提案 → 可选块级结构补丁
          → 来源/覆盖/顺序校验 → EditionArchive 导出
```

实现位于 `translation/ingestion/`，对外提供 Python API 和 `novel-translate import` CLI。

| 能力 | 当前行为 |
| --- | --- |
| 输入契约 | 显式声明版本、语言、有序来源、编码覆盖、卷提示及 TXT 段落策略；路径相对 manifest 解析 |
| 来源快照 | 保存源文件 SHA-256 和字节副本，原文件只读；来源改变、快照损坏或提取数据被修改时阻断导出 |
| EPUB | 读取 package/spine、EPUB 3 nav/EPUB 2 NCX；支持 fragment、空锚点及 URL 编码路径；提取 p/div/br、直接文本、列表、表格和 ruby |
| TXT | 严格解码；支持 BOM、显式编码；有正文差异的编码歧义阻断；保留物理行号和解码字符范围 |
| 段落策略 | `line` 每个非空行一段；`blank_line` 按空行合段且保留段内换行；`wrapped` 显式合并折行，保留对白/列表/分隔符边界；`auto` 首版保留物理行并报告歧义 |
| 章界 | 支持单资源多章、跨资源连续范围、正文标题和无标题正文；弱编号候选不直接切章或从正文移除 |
| 结构补丁 | `set_boundary / remove_boundary / select_title / classify_range / set_paragraph_policy / set_volume`；绑定旧结构修订，拒绝无效锚点、过期补丁和正文替换 |
| 导出 | 输出 entries/paragraphs/provenance/issues 与完成 manifest，保留来源范围、注音、资产引用、转换标记和标题去向 |
| 下游接入 | `EditionArchive.from_folder()` 校验新导出的数量、指纹和段落归属；新输入按显式逻辑卷切索引父节点，旧输入继续沿用旧规则 |

重复执行同一输入得到相同 ID 与内容。已有完成目录不可被不同输入覆盖；补丁保留旧结构快照，并支持已应用补丁的幂等重放。正文非空白字符在段落规范化前后必须相同。

导出完成仅表示结构校验通过，仍为 `semantic_status=unreviewed`、`evaluation_eligible=false`。格式统一不会自动建立日中配对关系。

## 2. 测试与真实样本

新增 `tests/translation/test_input_normalization.py`，覆盖多章合并、一章跨资源、无标题、零宽字符、全半角标题、父子导航、空锚点、ruby/表格/直接文本、TXT 三种段落策略、BOM/显式编码、歧义阻断、来源快照和导出完整性、结构补丁与过期拒绝、逻辑卷索引和 CLI。

最终离线回归：**532 passed**，其中标准化定向测试 31 项；唯一 warning 来自既有 pykakasi 对 `pkg_resources` 的弃用提示。`compileall` 和 `git diff --check` 通过。本次模型调用为 0。

### 2.1 现有语料的变形验证

验证脚本：`scripts/verify_input_normalization.py`。最终窗口结果保存在：

```text
artifacts/translation/import-validation/20261019-final-windows/report.json
```

每个窗口构造 6 种包装：每章一资源的 EPUB、带 fragment 目录的合并 EPUB、无有效元信息而只有正文标题的合并 EPUB、一章拆成多个资源的 EPUB、UTF-8 TXT、UTF-16 TXT。合并输入的条目数、标题、正文段落及顺序均与冻结基准逐字比较。

| 窗口 | 逻辑条目 | 基准正文段数 | 6 种包装 |
| --- | ---: | ---: | --- |
| 剑日文主线 400–402 | 3 | 180（68/54/58） | 全部一致 |
| 蜘蛛日文主线 11–13 | 3 | 279（81/101/97） | 全部一致 |
| 蜘蛛中文第 1 卷「1 开场就是高潮」「S1 日常生活终结的一刻」「2 房租零圆的家」 | 3 | 526（196/76/254） | 全部一致 |

共 **18/18** 通过。跨资源连续范围仍保留 `resource_continuation_ambiguous`，不把仅凭资源相邻的合并视为已确认出版章界。

这些 fixtures 从既有站点读取器选择非空正文段，显式将段内换行投影为空格，并在保留原标题的同时添加一致的章界测试前缀。选取位置、原题、源文件哈希和投影规则保存在各窗口的 `selection.json`。它们验证已知结构的包装变化，不证明网上任意 EPUB/TXT 都能恢复，也不验证译文语义。

### 2.2 整本导入检查

整本产物与报告：

```text
artifacts/translation/import-validation/20261019-run3/report.json
artifacts/translation/import-validation/20261019-run3/<sample-id>/full/
```

| 输入 | 导出条目 | 导出正文段 | 提取层非空白字符 | 导出正文非空白字符 |
| --- | ---: | ---: | ---: | ---: |
| `転生したら剣でした.epub` | 1359 | 107232 | 3869316 | 3869316 |
| `蜘蛛ですが、なにか？.epub` | 600 | 60924 | 1527348 | 1527348 |
| `转生成蜘蛛又怎样！ 1.epub` | 28 | 4992 | 118235 | 118225 |

以上条目数是当前文件实际恢复的阅读条目，包含支线或资料，不能等同于先前实验中主线章数。三份文件均通过来源哈希、资源文字覆盖、块归属、阅读顺序、导出完成性和 `EditionArchive` 加载检查，原文件哈希未变。

两份日文文件没有可见正文标题块，因此全部提取文字仍进入正文；其标题来自导航。中文文件的 10 个字符归属标题块，由 provenance 独立保存，并非遗漏。日文保留弱标题候选警告；中文还保留 `image_only_resource` 和 `navigation_target_missing`，因此均为 `ready_with_warnings`。图片中的文字没有做 OCR，不属于本次文字覆盖证明。

## 3. 实测发现并修复的问题

1. **正文数字被误当章名。** 初始规则将「2、3分」「第一部隊」「第1話は各所で無料公開中」等正文移作标题。初始剑样本得到 1366 条，蜘蛛得到 613 条；收紧后恢复为目录对应的 1359 和 600 条，弱候选继续留在正文，不丢句子。
2. **目录标签规范化污染原题。** 初始目录解析压缩连续空格；现将原题和匹配规范化键分开，测试验证全角数字、全角空格、连续 ASCII 空格均保留。
3. **TXT 策略只被记录、未参与导出。** 已将段落合并落实到导出，每个合并段保留所有组成物理行的来源定位，且禁止非空白正文变化。
4. **导出只检查结构，未检查提取。** 已改为使用联合校验，编码歧义、资源遗漏、快照缺失或提取篡改会阻断导出。
5. **导航指向第一段时可能误删正文。** 只有实际标题块或与目录标签一致的块才移入标题角色；空 `<a id>` 也能定位到其后的正文块。
6. **导出缺少完成性校验。** 新导出最后写入完成 manifest；加载时校验数据指纹、数量、段落清单和 provenance，避免把不完整输出交给下游。
7. **分步 CLI 的目录参数与旧 run 名校验冲突。** 旧参数检查将新的 `Path` 类型误当成实验名称；已隔离导入子命令，并增加 extract → plan → read → validate → export 的真实 CLI 调用回归。

## 4. 使用方法

输入 manifest 示例（将路径换成实际文件）：

```json
{
  "schema": "novel-import-input-v1",
  "edition_id": "my-source-ja",
  "language": "ja",
  "edition_label": "待翻译原文",
  "paragraph_policy": "auto",
  "sources": [
    {"source_id": "book", "path": "inbox/book.epub", "input_order": 0}
  ]
}
```

按步骤执行：

```powershell
.\.venv-test\Scripts\python.exe -m translation.cli import inspect --manifest inputs.json
.\.venv-test\Scripts\python.exe -m translation.cli import extract --manifest inputs.json --output artifacts/translation/imports/example
.\.venv-test\Scripts\python.exe -m translation.cli import plan --run artifacts/translation/imports/example
.\.venv-test\Scripts\python.exe -m translation.cli import validate --run artifacts/translation/imports/example
.\.venv-test\Scripts\python.exe -m translation.cli import export --run artifacts/translation/imports/example --output artifacts/translation/editions/example
```

也可一次执行 `import run --manifest inputs.json --output artifacts/translation/imports/example-run`，输出的 `extraction/` 保存提取与结构，`corpus/` 保存可加载档案；有阻断错误时不导出 corpus。

疑点处理使用 `import read --run <extraction-dir> --block-id <B...> --context 2` 和 `import apply --run <extraction-dir> --patch patch.json`。首版补丁的 `at` 和 `title_ref` 只接受完整块的 `{"block_id":"B..."}`，不接受设计目标中的字符偏移。读取有块数和字符数上限，不触发模型或重建。补丁后用新的空目录导出，以保留旧语料视图。

复现样本验证：

```powershell
.\.venv-test\Scripts\python.exe -m scripts.verify_input_normalization --output artifacts/translation/import-validation/new-run --full
```

## 5. 下一批边界

尚未进行本模块专属的真实连续翻译；当前验证止于标准语料加载和离线索引接入，没有新增翻译费用。旧翻译测试作为回归继续通过。

接下来可按设计推进：自动恢复逻辑卷和目录区、超长章节预算视图与重组、旧 parallel-v1 适配及通用连续翻译配置，再做两个模式的限定窗口实测。当前未分章大文件可保存和导出，但若单条目超过索引预算，现有索引仍会明确报错；不能据此声称已经支持任意长度的整本 TXT。agent 辅助与 OCR 仍未实现。
