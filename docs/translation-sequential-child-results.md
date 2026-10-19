# 串行二级节点抽取试验结果

日期：2026-10-17。实验目标是验证“同一一级节点内的二级节点串行抽取、不同一级节点并发”的方案，并使用 `main:601—700` 的汇总翻译 `main:701`。

后续P5审校、裁决、修订与回归结果，以及P6阶段验收判断，见 [第701章审校结果](translation-p6-701-review-results.md)。

进一步将 `main:691—700` 的聚焦 P6 候选上下文加入 D 译文审校与裁决后的对照结果，见 [聚焦 P6 抽取信息后的审校结果](translation-p6-701-focused-review-results.md)。

## 执行方式

本轮选取 `main:501—600` 和 `main:601—700` 两个一级节点，每个节点拆成十个十章二级节点。两个一级节点由线程池并发执行；同一父节点严格按 501、511、521……或 601、611、621……的顺序执行。当前二级节点只接收：更早一级节点的冻结摘要和译名元信息、同一父节点之前二级节点的摘要、当前二级节点的完整日中正文及段落标签。当前父节点后续内容不会泄漏给当前子节点。

抽取结果先保留为未审候选，再由程序按子节点命名空间合并事件 ID；实体不自动去重，时间边保持子节点内部端点。一级汇总仍保留每条记录的子节点来源和证据。

执行器位于 [`run_sequential_child_extraction.py`](../scripts/run_sequential_child_extraction.py)。结束标记的无损处理和正常停止但缺失 `@@END` 的摘要续接位于 [`settle_sequential_child_extraction.py`](../scripts/settle_sequential_child_extraction.py)；最终离线准入修订位于 [`quarantine_incomplete_sequential_child.py`](../scripts/quarantine_incomplete_sequential_child.py)。后者只允许摘要继续作为导航，`complete` 仍为 false，并将该响应的其他记录隔离；没有把缺少结束标记的响应伪装成完整响应。

## 执行结果

- 两条父节点链均完成十个子节点；父节点之间的 HTTP 请求最大并发数为 2，同一父节点内没有重叠请求。
- 501—510 的响应复用先前实验缓存，因此本轮输出目录包含 20 个子节点结果，但新发出的抽取请求为 19 个；再加 1 个 701 翻译请求。本轮新发 HTTP 共 20 个。把早期保存的 seed 请求也计入整个连续实验，则物理请求数为 22。
- 571—580 的模型响应 `finish_reason=stop`，但没有 `@@END`。第二次离线处理将其摘要保留为导航，并隔离其余 62 条实体、事件、状态等记录；这些记录没有进入一级汇总。631—640 的 `@@END` 粘在上一行末尾，经过可逆拆分后解析为完整响应。
- 601—700 一级汇总保留 10 条摘要、225 个实体、172 个事件、50 个状态、103 个知情/观点记录、45 条时间约束和 33 条待查项。它们全部标记为未审候选。

逻辑报告把复用的 501—510 用量也列入统计：输入 1,790,144、输出 149,290、缓存输入 30,144、reasoning 1,211。扣除复用 seed 后，本轮新生成输入为 1,710,777、输出 142,891；其中 19 次抽取输入 1,275,687、输出 141,512，701 翻译供应商计数输入 435,090（本地估算 435,514）、输出 1,379。翻译上下文低于 1,048,576 tokens，因此通过一次有记录的预算修订将原先误设的 262,144 输入上限改为 900,000；没有因为修订重复发送抽取请求。修订记录在 [`translation-budget-amendment.json`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/translation-budget-amendment.json)。

## 抽取质量观察

串行前文摘要对跨章关系有明显帮助，但不能代替语义审校。当前结果中可以看到几类正确的结构化信息：

- main:610 将泽罗斯里德记录为杰莱瑟人体实验的受验体，并把前世界线事件标为回忆；杰莱瑟与泽罗斯里德没有被混成同一施害者。
- main:616 的状态主体是师匠这把剑，描述了神属性使用后的耐久下降和自我修复停止，避免了把性能下降挂到芙兰身上。
- main:645 把“大魔兽优先挣脱封印、恢复力量”记录为师匠的 `believes` 观点，而不是已确认事实。
- main:651 的事件和状态都指向维纳莲以神属性水丝消灭大魔兽。
- main:688 的观点明确写成“不像间谍，更像雷多斯出身的大小姐密旅”。
- main:610 的历史事件使用 `story_time=回忆/前世界线`，`disclosed_at=610`，并单独保存叙述发生位置；这比把回忆事件的发生时间直接当作当前章节时间更安全。

同时仍有覆盖缺口。main:697 的 `ミゲール／リディック` 没有进入 601—700 的实体卡，因此 701 翻译仍然写成“米格尔／里迪克”，而已有参考译文采用“米凯尔／利迪克”。这说明串行策略能改善看到前文后的关系抽取，但不能保证每个局部专名都被抽取；目标章仍需做表面专名检索和邻近双语回查。

## 701 翻译对照

新方案 D 的完整输出和 A/B/C、参考译文并列在 [`comparison.html`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/comparison.html)。D 的若干预先检查项表现如下：

- “我国的王，说要册封你为骑士，任用你”保留了国王任用芙兰的施受方向；A 曾写成“为你所用”。
- 使用“全国”，没有把范围改成“各国”。
- 保留“子爵”，没有加入 C 中出现的“伯爵”。
- 保留“裤子和地板”，没有加入“王室地板”等无依据词语。
- 师匠的通道台词保留 `『……』` 标记。
- 配角姓名仍为“米格尔／里迪克”，没有解决 697 章译名覆盖缺口。

这些结果只能说明本次单样本的局部变化，不能证明串行抽取普遍优于 A/B/C。一级知识仍未经过 P5 语义审校，也没有把模型候选提交到持久知识库。

## 可复核产物

- 完整输入输出、原文、D、A/B/C 和参考译文：[`comparison.html`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/comparison.html)
- 运行报告：[`evaluation/report.json`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/evaluation/report.json)
- 离线完整性审计：[`evaluation/audit.json`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/evaluation/audit.json)
- 601—700 一级汇总：[`rollups/parent-601.json`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/rollups/parent-601.json)
- 代表性子节点完整输入：[`child-601/prompt.md`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/io/parent-601/child-601/prompt.md)
- 代表性子节点原始输出：[`child-601/output.txt`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/io/parent-601/child-601/output.txt)
- 701 翻译完整输入：[`translation-701/prompt.md`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/io/translation-701/prompt.md)
- 701 翻译原始输出：[`translation-701/output.txt`](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/io/translation-701/output.txt)

本轮翻译测试结果为 **298 passed**；离线报告和审计阶段没有新增 HTTP 请求。
