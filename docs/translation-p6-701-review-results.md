# 第701章审校与 P6 阶段验收

日期：2026-10-17。

本报告记录串行二级索引实验后，对目标 `main:701`（站点原文 `ja:syosetu:n6006cw:703`）执行的 P5 审校、裁决、定点修订、回归审校，以及 P6 阶段验收判断。实际审校调用使用 `glm-5.3-flash`；目标参考译文没有进入模型请求。

后续将 `main:691—700` 的聚焦 P6 候选信息加入审校与裁决上下文后的对照试验，见 [聚焦 P6 信息的第701章审校结果](translation-p6-701-focused-review-results.md)。该试验没有带来已确认的额外修复。

## 结论

审校链成功找出并修复了一处严重施受关系反转。回归审校把一条时态建议正确裁定为可接受意译；修订补丁能精确重建修订稿。但这只是一个开发章节，不能证明 P6 的抽取和翻译效果稳定。P6 仍未达到验收标准：设计要求至少三个冻结验证窗口，目前没有这些配对验证结果；索引中的候选知识尚未完成语义审阅；译名差异仍在；数字检查信号仍未处置。因此，该译稿状态为 `review_required`，未接受或发布。

## 审校发现与修订

初次审校将 `S014` 的「ミゲールに勝った相手の名前は？」与译文 `T014`“米格尔打赢的对手叫什么名字？”判定为严重的施受关系反转。原文问的是“打败米格尔的对手叫什么名字”，而不是“米格尔打败了谁”。独立裁决确认该问题，建议改为“打赢米格尔的那个人叫什么名字？”。修订阶段只应用了这一处补丁，修改后的段落为：

> 「打赢米格尔的那个人叫什么名字？」

回归审校针对开头的“打得漂亮”提出一条低置信度时态建议；独立裁决认为这属于自然的中文回顾性表达，驳回了该建议，没有再改正文。

初次裁决引用 `S012`“俺に勝った女剣士”及其附近红发剑士描述作为上下文。需要谨慎理解说话人归属：`S012` 本身没有显式说话人标记；模型把这句话归给里迪克，离线参考译文也作相同归属，但单看当前原文上下文不足以把该归属视为明示事实。本次修复依据的是日语「に勝った」的施受结构及后续问答语境，不依赖把说话人身份当成确定事实。

译名问题仍未解决：本稿使用“米格尔／里迪克”，已有参考译文使用“米凯尔／利迪克”。这表明当前索引候选没有提供或确认这两个名字的固定译法；本次没有擅自把参考译文升级为权威术语规则。

## 审校输入与来源

审校和裁决请求使用了相同的冻结审校上下文；独立裁决请求只在该上下文后附加待裁决发现。上下文包括目标章分段后的日文原文与当前中文草稿、段落映射、既有分层摘要和双语历史资料、`601—700` 串行抽取汇总，以及它们对应的来源登记。完整序列化输入见本次运行工件中的 `review-context.prompt.md` 和各请求的 `io/*.messages.json`。

离线来源核验确认：上下文选用了6个冻结摘要对象，含一个绑定到实际汇总文件的 `601—700` 索引工件；登记了1,349项依赖，其中包含1,148份历史日中来源。依赖闭包通过当前语料与可见性策略校验。第701章目标参考译文未进入任何模型请求。

初次审校请求计579,180输入 tokens；裁决请求计579,061和579,084输入 tokens，后者复用的缓存输入为578,304。修订请求计582,946输入 tokens。回归审校与裁决分别计579,180和579,055输入 tokens。缓存输入是输入量的子集，不应重复相加。

## 回归检查与验收阻塞项

修订稿的程序检查没有硬失败，映射完整；但数字／单位信号仍需人工处置。当前启发式把中文“一点”“这一点”“一个”等普通用法计成数字，把“十分抱歉”的“十”计成数字，同时未把日文「30歳」中的年龄单位与中文“三十岁”可靠对应。检查器因此记录了信号差异；接受记录没有信号裁决，状态仍是 `review_required`。这属于检查器与待处置信号的缺口，不能据此断言正文有数字错译，也不能忽略它直接接受稿件。

本轮审校模型没有对全章做独立人工语义验收；无硬规则失败、映射完整和单项修订成功都不等于完整译文正确。P6 设计的阶段验收还要求至少三个冻结配对验证窗口、错误知识风险评估和目标错误净下降。本次只有一个开发窗口，且索引抽取结果仍标记为未审候选，所以结论为 **P6 未通过阶段验收**。这不否定串行抽取架构；它说明现有证据不足以宣称该方案达到稳定的知识质量或翻译收益。

## 离线重放与工程修复

发现审校上下文来源闭包登记不完整后，建立了隔离的 `p6-review-701-provenance-replay2` 快照，并修复了上下文校验：读取已选摘要对象时检查其节点身份；读取提示词来源工件时检查工件 ID、来源、哈希和实际注入文本是否一致。对应测试覆盖节点错配和提示文本漂移。

历史8份模型请求与响应均在离线环境重新解析：2次映射、2次审校、3次裁决和1次修订。映射仍完整，审校报告与保存结果一致，裁决记录可解析，修订补丁可精确重建修订译文。重放登记依赖后，prompt 上下文内容仍与历史请求一致，新增 HTTP 请求为0。该结果证明的是来源和解析可复现性，不是一次新的模型语义审校。

历史调用总用量为3,486,724输入 tokens、6,094输出 tokens；其中2,847,680缓存输入 tokens 已包含在输入总量中。离线重放没有新增模型用量。

全量测试：**301 passed，10条既有依赖弃用警告**。`git diff --check` 通过。

## 可复核材料

- [修订后的第701章译文](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701/revised.zh.txt)
- [初稿](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701/draft.zh.txt)
- [原文](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701/source.ja.txt)
- [完整审校上下文](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701/review-context.prompt.md)
- [历史调用审计](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701/evaluation/audit.json)
- [来源修复后的离线重放报告](../artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-provenance-replay2/evaluation/offline-replay.json)
- [P6 验收条件](translation-p6-design.md#10-验证集与验收条件)

离线重放可复现命令：

```powershell
& 'C:\ProgramData\anaconda3\python.exe' -X utf8 scripts/replay_sequential_review_701.py --old 'artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701' --new 'artifacts/translation/tenken-cache-clean/verification/sequential-child-extraction-501-700-refined/p6-review-701-provenance-replay2'
```
