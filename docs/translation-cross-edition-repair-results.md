# 跨版本连续翻译修复与端到端复验

日期：2026-10-18。对应[修复计划](translation-cross-edition-repair-plan.md)。模型保持本地网关配置的 `deepseek-flash`，未更换模型，也未修改旧分支的译文、冻结输入或审校意见。

## 已实现的工程修复

| 项目 | 实现与边界 |
| --- | --- |
| 源段绑定 | 新的跨版本连续运行默认使用 `source-records-v1`，整章生成 `@@TRANSLATION C章节.J段落` 记录。验证 ID 恰好一次、顺序、正文非空及结束标记，组装时去掉 ID。全角标点、包装和记录内换行可无损解析；不补空段、不猜缺失 ID。 |
| 有界恢复 | 首次生成、格式反馈、传输重试和翻译工具调用共享每章最多 3 次物理请求。重启复用已保存响应和同一任务预算。失败到上限后阻断并导出材料。 |
| 连续协议一致性 | 新运行冻结 `history_output_profile=source-records-v1`，将历史 assistant 译文也表示为源段记录，避免用纯正文历史示范与当前输出协议竞争。已有冻结运行不改变。 |
| 结构与语义分开 | 有源段声明时直接建立 `declared_by_translation_output` 结构覆盖，不再付费让映射模型猜一次。仍然审校全文，绑定明确标为 `semantic_status=not_verified`。 |
| 名称形式 | 实体匹配增加 `reference_rendering`、`rendering_kind`、`rendering_evidence_ref`；实际称呼须逐字存在于中文用例。旧记录的实体主名不再被解释为已确认的当前爱称。 |
| 定向称谓补查 | 根据当前章已抽取的源侧名称，对未匹配及缺少具体爱称形式的候选，批量查看相关文库原始正文。默认每章最多 8 名、约 16k 输入预算；不要求中文实体目录先抽到对应名称。没有证据则保留缺口。 |
| 材料选择 | 局部参考正文采用“事件窗口 ∪ 术语用例窗口”；共享最终预算。补查、候选、引文、可见性与省略原因进入冻结参考包，翻译、审校和裁决共用。 |
| 失败收尾 | 暂停、阻断、检查点异常及正常结束均导出已有草稿、完整请求响应、状态和索引快照，关闭仓库。报告导出不调用模型；尾部索引是显式、可恢复的付费步骤。 |
| 旧流程兼容 | 原 `plain-v1` 运行保持旧 prompt 和缓存身份。旧映射增加非法引用原值、合法范围和附近正文诊断；已有历史失败对话仍使用原反馈策略。 |

实现入口是 [output.py](../translation/sequence/output.py)、[terminology.py](../translation/editions/terminology.py)、[连续运行脚本](../scripts/run_cross_edition_sequence.py)和[受控实验脚本](../scripts/verify_cross_edition_repairs.py)。

对规划做了两项简化：布局恢复统一重发整章记录，没有实现局部补丁拼接；失败响应先作为不可变 attempt 保存，成功后才建正式版本，避免消耗语义修订轮次。旧 68 段稿作为冻结的外部恢复种子，保存原 run/version/hash，不伪造为新分支已提交历史。

## 实验一：恢复旧 68 段草稿

保留 `continuous-v1`，用 `fork_from_commit` 继承原来已完成的 4 个条目，把第五个条目 `page14.html` 的真实失败稿作为种子。没有加入手写的作品答案。

结果：一次模型生成得到 69 个合法源段记录，随后完成审校、裁决、提交和尾部索引，未使用 LLM 映射。新增 1 个提交，继承 4 个提交，5 次物理请求；pending outbox 为 0。完成后把 provider 请求上限设为 0 再执行，提交、head、对象数和请求账本完全不变。

人工核对尾部发现，J068/J069 不再整体错位，含义保留；但 J065 仍提前包含“也只会回一句”，J067 又输出“就只是这样回答我”，存在重复叙述。这说明结构恢复成功，逐段语义归属仍未完全正确，审校也没有检出。

材料：[完整输入输出](../artifacts/translation/spider-independent-index/layout-recovery-v2/report.md)、[逐段核对](../artifacts/translation/spider-independent-index/layout-recovery-v2/repair-audit.json)、[无调用重放](../artifacts/translation/spider-independent-index/layout-recovery-v2/idempotency-audit.json)。

## 实验二：固定旧 S1 首稿，只改变审校材料

旧稿 hash 固定为 `b87dcf47aebb190342c962443db16bebcd8e7e0dc655049936988b8916cf5cf0`。没有先换成新译文再比较审校效果。

定向补查从文库 S1 原始正文找到了：

| 源称呼 | 补查形式 | 实际中文证据 |
| --- | --- | --- |
| 岡ちゃん | 冈姊 | `Z5422c21958d3P0051`：外号冈姊的冈崎香奈美老师 |
| リホ子 | 真贞子 | `Z5422c21958d3P0063`：她叫作真贞子，那不是她的本名 |
| べスベルウィッチ | 贝斯贝尔魔女 | `Z5422c21958d3P0019`：躲避该头目的魔法 |

这些用例进入了实际审校请求，相关段落也通过引用绑定验证。结果分两组：

| 变体 | 结果 | 判断 |
| --- | --- | --- |
| 只补充名称候选及原始用例，保持现有检查要求 | 3 次调用，仍为 0 findings，无修订 | 补齐证据本身没有解决漏检，不能再归因为本次证据未注入。 |
| 再明确要求逐项检查称呼形式，独立 `name-forms-v1` 实验 | 初审 5 条，裁决确认 4 条、驳回 1 条；执行一轮修订，复审还有 2 条确认意见；共 7 次调用 | 能定位“冈酱”“理保子”等并引用正确原段，但未覆盖全部重复出现位置，且出现错误修复建议。 |

第二组把 Web 的 `リアルホラー子` 解释直接改成了文库的“真人版贞子”。虽然引用确实存在，却不能据此判定改写忠实于 Web。它还把一个普通措辞差异列为问题，裁决正确驳回了这一条。

因此，`name-forms-v1` **仅保留为显式实验开关，没有默认接入新运行**。固定草稿实验完成不等于质量通过，也没有为这些稿件生成正式提交。不能把“findings 增多”简单解释为能力改善，更不能认为正确引用自动证明修复方向正确。

材料：[仅增补证据](../artifacts/translation/spider-independent-index/fixed-review-v2/report.md)、[增加检查要求](../artifacts/translation/spider-independent-index/fixed-review-v3/report.md)、[每轮意见、裁决及最终版本](../artifacts/translation/spider-independent-index/fixed-review-v3/controlled-review.json)。

## 实验三：新协议从头生成并连续执行

实验使用冻结的 `embedding-3-v1/corpus` 和 `pilot-v3` 双侧索引。窗口为原生条目 `page10.html–page21.html`，即阅读位置 10–21；其中含 S 线条目，不能把它称为故事标题编号第 10–21 章。

输入容量规划保持 131072 context / 16384 max output / 1.05 先验膨胀 / 2048 safety；连续运行输入软上限 80000，翻译输出上限 8192。为观察新索引跨块行为，L2 每块上限设为 5 个条目。

先执行 5 个条目后主动暂停，再在同一冻结分支上显式建立检查点并恢复剩余条目。前 5 个条目全部成功，其中 `page13.html` 的第一次输出是纯正文，未遵守记录协议；程序反馈结构错误后第二次输出通过，原始失败响应仍保留。

显式检查点的保守输入估算从 62799 降为 49388 tokens，减少 13411（约 21%）；这次移除了 `page11.html` 的完整双语，保留 `page12–14` 三个邻近完整双语。`page10` 此前已由自动检查点摘要化。正文仍保存在仓库中，不因压缩删除。

新 S1 草稿采用“冈姊”“真贞子”，但同时把昵称解释写成“真人版贞子”。因此名称召回和采用有改善，跨版本语义污染仍未解决。J065–J067 仍可发生含义前移；源段 ID 只能证明结构声明。

完整窗口 **12/12 条目提交，1155/1155 个非空段落**；源文 revision、输出绑定、覆盖、审校报告和参考包均通过本地重验。46 次连续流程调用（14 次翻译、12 次审校、12 次裁决、8 次索引抽取），另有 8 次预编译称谓补查，合计 54 次。第二处结构恢复发生在 `page19.html`：缺少 `@@END`，反馈后第二次通过。没有绕过校验补齐结束标记。

最终有 3 个最新 L2（10–14、15–19、20–21），前两个为 `partial`，分别隔离 1、2 条记录；最后一个为 `extracted`。所有已提交条目的尾部导航均已物化，pending outbox 为 0；`partial` 仍保留缺口说明，不能解释为知识抽取完整。另保留了中途开放前缀的旧节点版本。

连续流程实际记录 input/output 为 2314260 / 87402 tokens；预编译为 162624 / 7050，合计 **2476884 / 94452**。其中 input 包含缓存命中，不能直接当作无缓存计费 tokens，也不包含前轮双侧索引的建设成本。

完成后将 provider 物理请求上限设为 0 重放：12 个提交、head、检查点、对象数及 46 条请求账本完全不变，未新增模型调用。见[无调用重放记录](../artifacts/translation/spider-independent-index/continuous-v2/idempotency-audit.json)。

材料：[连续运行完整 IO](../artifacts/translation/spider-independent-index/continuous-v2/report.md)、[结构恢复和成本统计](../artifacts/translation/spider-independent-index/continuous-v2/validation.json)、[上下文压缩](../artifacts/translation/spider-independent-index/continuous-v2/compression-audit.json)、[源文及引用复验](../artifacts/translation/spider-independent-index/continuous-v2/repair-audit.json)。

## 补充：历史 assistant 输出格式

核查格式失败时发现，`continuous-v2` 当前任务要求记录协议，但未压缩的历史 assistant 回答仍是纯正文。此前代码直接使用 `commit['text']` 重建历史，没有沿用记录格式。这是提示材料的一处工程不一致，不能把全部格式失败归因于上游模型指令遵循。

已为后续新运行增加冻结的历史输出配置；根据最终提交文本重建记录，包含语义修订后的正文。旧运行没有该字段，恢复时仍保持原始消息字节。`continuous-v3` 复用 v2 的 5 条目冻结材料和称谓补查，仅启用一致的历史格式，另做从头生成实测。

结果：**5/5 条目、437/437 段完成，5 次翻译全部首次通过记录解析**。同一个前五条目窗口，v2 用了 6 次翻译，v3 用了 5 次；尤其 `page13.html` 本次没有返回纯正文。两次单次采样不能证明格式失败率已经降低到 0，也不能将差异唯一归因于历史格式。

v3 共 18 次新增流程调用（5 翻译、5 审校、5 裁决、3 抽取），input/output 为 733136 / 31528 tokens；称谓补查复用 v2 已支付材料，不能把此处预编译 0 次解释为不需要建设成本。最终 L2 为 `partial`，隔离 1 条，pending 为 0；禁止新增请求的完成态重放也未改变持久状态。名称解释的版本污染及跨段语义搬移仍然存在。

材料：[一致历史格式的完整 IO](../artifacts/translation/spider-independent-index/continuous-v3/report.md)、[来源及引用复验](../artifacts/translation/spider-independent-index/continuous-v3/repair-audit.json)、[无调用重放](../artifacts/translation/spider-independent-index/continuous-v3/idempotency-audit.json)。

## 验收范围与剩余问题

全量回归 **501 passed**；`compileall` 与 `git diff --check` 通过。覆盖结构失败反馈、重复/缺失/非法 ID、全角字符、三次上限、保存响应后崩溃恢复、重启不重复付费、失败导出、检查点异常、旧反馈兼容和历史 assistant 协议一致性。

本次已满足**给定双侧索引和限定窗口的实验级端到端执行条件**：参考编译 → 翻译 → 输出校验与恢复 → 审校 → 裁决 → working 提交 → 增量索引 → 压缩与恢复 → 审计导出。12 条目完整窗口、最新历史格式的 5 条目窗口及旧失败稿恢复均实际执行完成。没有验证全书索引召回和 L1 边界，不将其扩大为全书无人值守质量验收。

仍需区分以下边界：

- 抽取目录不是穷尽的；补查仅针对当前已有源侧实体，默认范围外不遍历全书。部分条目没有有效的跨版关联范围，保留 `no_reference_scope`。
- 上游抽取还会包含“私”等普通词，补查也会为它们消耗预算；本轮没有重建通用实体筛选器。
- 增量抽取可产生 `partial` 节点和隔离记录。可用摘要允许继续导航，但不能宣称时序/知情状态完整。来源范围以程序成员为准，模型自报覆盖不作穷尽证明。
- 旧错误译法没有跨分支自动清理或升格。生成译法仍是 `provisional_rendering`；本轮未实现全库候选替代和历史污染回溯。
- 同一模型在证据充分时仍会漏检、漏修和错误裁决；本次结果不足以把原因唯一归为模型能力，也不能宣称语义高正确率或全书可无人值守使用。

没有自动执行 git commit。
