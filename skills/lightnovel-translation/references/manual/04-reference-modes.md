# 04 参考模式与历史对应

[返回目录](index.md) · 上一步：[语料身份](03-corpus.md) · 下一步：[独立索引](05-edition-index.md)

## 模式决定证据可以用于什么

`project.create_job(..., mode=...)` 把参考关系作为调用方声明冻结。`auto` 仅根据参数选择：提供非空 `parallel_pairs` 则 parallel；否则提供 references 则 cross_edition；都没有则 source_only。它不会自动判断两本书是不是同一版本，也不执行全文语义对齐。

| 模式 | 输入 | 在翻译中的作用 |
|---|---|---|
| `source_only` | 单一日文 Edition | 使用日文及本分支已生成材料 |
| `parallel` | 日文、中文及明确的历史对应表 | 完整历史双语 few-shot，约束译法和文风 |
| `cross_edition` | 日文、独立中文参考，可加索引关联和实体匹配 | 术语及文风候选；不决定 Web 版事实 |

三种模式都只翻译日文源 Edition。目标必须是源内连续阅读窗口，不允许跳过现有中间章；参考 Edition 必须为中文。source_only 不接受参考材料；parallel 不接受跨版关联/术语补查参数；cross_edition 不接受伪装成严格对应的 parallel_pairs。

## parallel 如何表示缺章、多对一问题

`parallel_pairs` 的形状是 `{source_chapter_id: [translation_chapter_id, ...]}`，键值从实际 Edition 读取。全部源历史必须严格早于第一个目标，中文 ID 来自声明的参考版本且不能复用。一个源章可对应多个中文条目；当前公共形状不能把同一中文条目重复分配给多个源章。

缺失历史对应就不填，不按两侧数组序号自动补齐。若多个源章在中文侧合章、缺页或部分对应，应先整理来源结构或使用跨版本模式；不能把相似章节强制声明为完整对应。`caller_asserted_parallel` 表示调用方已确认，不是库做过语义证明。

历史原译有用，是因为目标之前的完整段落可以成为可靠示例；目标译文本身和未来参考答案不能混入 parallel 历史，否则验证或翻译会发生答案泄漏。

## cross_edition 为什么不要求逐章匹配

Web 与文库可重排、增删、改名、改变人物关系与结局。独立索引先分别记录每一版事件和实体；LLM 再关联 L2 摘要，允许非单调、多对多或没有对应。匹配结果用于找参考材料，不证明正文相等，也不自动把文库时间线合并进 Web。

即使没有事件匹配，也可注入用户提供的中文正文作独立文风样本。术语候选需有当前/过去 Web 用例与中文证据，不能利用未来身份揭晓解释当前昵称。检索机制见 [06](06-reference-retrieval.md)。

## 历史对齐算法在哪里

内部 `translation.alignment` 保留规则候选、局部序列对齐和 embedding 探测等工具。规则主要利用标题/编号和顺序；`local_sequence_align` 在给定打分下允许局部分组与缺口，embedding 提供语义相似分数。这些结果仍需检查重排、缺页、同词异剧情等情况。

公共 `create_job` 不会自动调用这套历史对齐工具，不会从 embedding 得到权威一一映射。需要调用它们时属于底层开发流程，先查看保存结果与来源，再明确转换为历史对应或跨版参考；不要向用户报告“自动建立严格对齐”而实际只有摘要相似。

## 排错入口与代码位置

先查 `job.status()` 的参考模式，再查冻结参数和 `job.reference_report()`。模式选错需要新任务，不能靠恢复时改参数重解释旧请求。

- `src/lightnovel_translation/job.py`：`create_job`，模式选择、历史映射与连续目标校验。
- `src/translation/editions/sequence.py`：`build_sequence_inputs`，公共参考材料转为连续任务输入。
- `src/translation/editions/context.py`：跨版本材料的用途与可见范围。
- `src/translation/alignment/__init__.py`：`rules`、`local_sequence_align`、`semantic_window`，历史对齐工具。
- `src/translation/alignment/paragraphs.py`：历史段落对齐实现，与当前输出覆盖协议不同。
- 回归入口：`tests/translation/test_library_api.py`、`test_reference_workflows.py`。
