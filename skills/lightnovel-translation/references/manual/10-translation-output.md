# 10 译文协议与段落覆盖

[返回目录](index.md) · 上一步：[上下文](09-context-evidence.md) · 下一步：[审校修订](11-review-revision.md)

## 为什么使用逐源段记录

自然语言翻译会合并跨段句子。原文 69 段变成译文 68 段，可能是模型把引语后的叙述并入前段，也可能是真漏译；只比段数不能区分。再让模型从无标记正文恢复映射，也可能遗漏某个目标段 ID。

公共新任务默认 `output_profile="source-records"`：仍要求整章连贯翻译，但每个非空源段必须输出恰好一条 TRANSLATION 记录，复制完整源段别名；程序组装中文正文及声明覆盖。该协议需 `reference_profile="stable"`。

## 输入和输出形状

prompt 含逐段日文、合法 ID 的有序清单及虚构 few-shot。以下仅说明形状，运行时必须使用本章真实 ID：

```text
@@TRANSLATION Cdemo.J001
text: 第一段完整译文。
@@TRANSLATION Cdemo.J002
text: 第二段完整译文。
@@END
```

模型不能输出自造标题、分析、围栏或另起编号。记录内换行只是排版，程序会组装成一段；不能靠换行替代下一条记录。原文一句跨三段时仍分别保留全部叙述，避免将后一段内容搬入前段导致空记录。

## 解析器检查什么

1. 解析平面记录，允许受控的全角符号等语法变体。
2. 比较输出 ID 与本章 expected 源段列表：必须同集合、同顺序、各一次。
3. 每条 text 非空，协议结束标记有效，无不允许的额外记录。
4. 保存原始响应、解析诊断和组装译文，建立 source-bound 覆盖声明。

解析器不会猜测缺失 ID、复制邻段补洞，或把 68 条自动凑成 69 条。格式和覆盖失败会在尝试限额内附明确反馈，要求重输出完整记录；原始付费响应保留。截断与格式错误分别记录，可能在单次容量允许时扩大输出预留。

## 兼容 plain 输出的路径

`plain` 为旧任务/兼容调用保留。它先分割自由中文段落，再调用模型建立覆盖组。`coarse` 可把漏映射的目标段并入相邻组，保留 `mapping_gap` 诊断；这只避免结构映射把流程无谓阻断，不证明该处语义没有缺失。

源段声明可省去这一步模型映射，但不代替语义审校。即使恰好输出 69 条，模型仍可能在错误 ID 下写相邻段内容、漏掉限定词或误译说话人，需要 [11](11-review-revision.md)继续判断。

## 哪一层失败该怎样处理

| 现象 | 优先检查 |
|---|---|
| END 缺失、finish_reason 为 length | 原始响应是否截断、角色输出与 capacity 上限 |
| 全角标记、字段变体 | 保存解析诊断与容错结果，不立即归因语义能力 |
| missing/duplicate/out-of-order ID | actual-request 中合法 ID，原始记录和恢复反馈 |
| 结构通过但仍漏叙述 | 同 ID 下日中内容，进入语义审校 |
| 旧 plain 输出出现 mapping_gap | 覆盖组与诊断，不能按目标段序号硬推对应 |

配置变化只对新冻结任务生效；恢复旧 plain 任务不会自动改成 source-records。已有请求不能在同一身份下换协议重解释。

## 代码位置

- `src/lightnovel_translation/config.py`：`TranslationConfig.sequence_spec`，公共默认输出协议。
- `src/translation/sequence/output.py`：`protocol_messages`、`parse_output`、`render_output`、`recovery_feedback`。
- `src/translation/sequence/service.py`：翻译生成、格式反馈、译文版本编排。
- `src/translation/review/protocol.py`：`parse_records`，共享平面协议容错。
- `src/translation/review/coverage.py`：`declared_coverage`、`map_coverage`、`recover_local_gaps`，声明覆盖与旧映射恢复。
- `src/translation/review/versions.py`：译文分段与不可变版本。
- 回归入口：`tests/translation/test_library_api.py`、`test_reference_workflows.py`。
