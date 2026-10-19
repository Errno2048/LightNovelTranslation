# 第701章：聚焦 P6 抽取信息后的审校对照

日期：2026-10-17。

本次把串行二级索引得到的 P6 信息加入第701章 D 译文的审校、裁决、修订和回归审校上下文，检查它能否在保留已知正确修复的基础上促成其他可靠修订。

## 结论

**本次没有观察到 P6 信息带来的新增、已裁定修复，不能据此认定 P6 达到知识质量或翻译收益验收标准。** 已知的“米格尔打赢的对手”施受关系错误再次被定位、确认并修正，修订后回归审校保留了该修复；最终译文与此前审校链的修订稿逐字相同。

回归审校另外提出一项关于“等级、人气和地位一高”的条件关系疑虑，裁决认为它是可接受的中文意译并驳回，没有修改译文。当前译稿仍为 `review_required`，数字检查信号尚未显式处置，未接受或发布。

## 试验设置

上一轮审校把 `main:601—700` 的完整 P6 汇总作为一个巨大的上下文块。本次将该块替换为最邻近的 `main:691—700` 二级节点摘要和该节点的58条抽取记录，并将其放在上下文前部。保留了冻结的旧摘要、近三章完整双语、原审校约束和来源依赖闭包。58条记录均保留 `candidate_unreviewed` 状态，没有升级成已确认知识。

记录包括23个实体、16个事件、8个人物视角、3个状态、3条时序记录、4条待查项和1条摘要。该上下文共108,733字符；上一版完整汇总上下文为1,319,908字符。首轮审校输入由579,180 tokens降至56,202 tokens。这个变化证明聚焦选择能显著缩小本地审校输入；它本身不证明翻译质量提升。

本次依次完成初始映射、审校、裁决、定点修订、回归映射、回归审校和回归裁决，共7个模型请求。总用量为293,074输入tokens、5,083输出tokens；其中213,888缓存输入tokens已包含在输入总量中。目标参考译文只在模型请求全部结束后供离线比较，没有进入任何模型输入。

## 发现与修订结果

首轮审校再次发现 `S014` 的「ミゲールに勝った相手」被译成“米格尔打赢的对手”，把胜负方向颠倒。审校使用当前章节中的预选赛结果及米格尔的后续回答作为证据；裁决确认，修订为“打赢米格尔的那个人叫什么名字？”。该证据来自目标章节本身，不能据此说是 P6 的历史抽取促成了发现。

回归审校关注 `S006`：原文「ランクと人気と地位次第で」表示“取决于等级、人气和地位”，现译“等级、人气和地位一高”稍偏向程度条件。裁决认为上下文中的实际含义仍成立，属于可接受意译，驳回该建议。没有产生第二处确认修订。

P6候选没有包含目标附近两个人名的实体卡：`ミゲール` 和 `リディック` 均未出现在该节点抽取记录中。离线参考译文使用“米凯尔／利迪克”，D译文保留“米格尔／里迪克”。这仍是可观察到的译名差异；参考译文不是已批准的规范译名，因此本次只将其记录为一致性待查项。抽取层没有提供本次审校可据以裁定的译名知识。

比较最终译文与此前修订稿，文本完全相同。由此，本次结果支持“缩小并突出候选上下文能减少输入规模，并保留已知修复”，但没有显示它能进一步减少经裁决确认的错译。单个章节窗口也不满足 P6 设计中至少三个冻结验证窗口的验收条件。

## 核验与复现材料

离线审计重放了7个已捕获请求，确认请求与数据库中的prompt一致、响应与保存的输出一致，P6工件哈希有效，修订补丁可精确重建最终译文。1,349项来源依赖按当前语料和策略重新验证通过；新增 HTTP 请求为0。全量测试为 **301 passed，10条既有依赖弃用警告**，`git diff --check` 通过。

- [最终修订译文](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/revised.zh.txt)
- [58条聚焦 P6 候选记录](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/selected-p6-extraction.json)
- [实际审校上下文](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/review-context.prompt.md)
- [初次审校prompt与输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/02-review.prompt.md) · [输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/02-review.output.txt)
- [初次裁决prompt与输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/03-adjudication.prompt.md) · [输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/03-adjudication.output.txt)
- [回归审校输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/06-review.output.txt) · [回归裁决输出](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/io/07-adjudication.output.txt)
- [对照结果](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/evaluation/p6-context-review.json) · [离线完整性审计](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-p6-context-D/evaluation/audit.json)
- [可复现审校脚本](../scripts/review_sequential_701_p6_context.py)

原始请求、响应与 `review.sqlite` 保存在同一实验目录中。本报告记录的是单窗口开发试验，不是 P6 阶段验收通过声明。
