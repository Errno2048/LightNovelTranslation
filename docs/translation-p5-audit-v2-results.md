# P5：prompt、证据协议与模型能力诊断

日期：2026-10-16。已实现独立版本的 `audit-v2`／`audit-v3` 审校与裁决 prompt，并使用同一模型、同一初稿、同一丰富上下文完成真实对照。第1233章的3处 White 错译和6处 `_rank_` 残片已全部修复；第1200章仍有语义漏检和重复位置漏修。**P5质量验收仍未通过，真实译文接受数为0。**

结果说明了三种不同问题：协议差异可以由工程处理；知识权威表述不清会改变裁决；即使证据齐全、格式有效，模型的语义判断和逐项执行仍不稳定。本轮没有更换模型，不能把这些结果直接归结为模型家族的能力上限。

## 1. 本轮改动

默认 `legacy` 行为和历史请求保持；新实验显式选用新 profile，各版 prompt、原响应、派生对象及失败结果分别保留。

| 改动 | 具体约束 |
| --- | --- |
| 稳定段落引用 | 新协议可只返回S／T／R／L标签，程序绑定提供过的完整段落；模型若主动附带引文，仍须逐字匹配，不替它补引号或改字。 |
| 依据分类 | `source_meaning` 表示本章含义或内部一致性；`observed_usage` 必须有实际历史R／L正文；`style_preference` 不强制修复。摘要和实体卡不自动晋升为规范。 |
| 局部命名一致性 | v3明确：未审定的历史译文仍可证明同一人物的实际译名；满足身份与稳定用法证据时，可修复本章命名断裂，无须先批准全书规范。有冲突或不同称呼语境则保留未决。 |
| 出现位置完整性 | 要求每个目标段单独列出finding；同段多个问题可合并一个补丁，不允许只改一段却声称修好其他段的问题。 |
| 通用残片检查 | 程序枚举每处机器占位符候选，交给模型核验；修订后的规则检查仍会阻断残片。此模式明确记为 `rule_assisted`，不算纯独立发现。 |
| 裁决隔离 | 没有实际历史段落的历史译法意见，即使模型确认，程序仍保留为未决；风格偏好的错误确认单独保存后按策略驳回。 |
| 有限格式恢复 | 增强版裁决仅恢复最后一条 `reason` 句尾的独立 ` @@END`；增强版映射仅把精确枚举 `已映射` 归一为 `mapped`。原响应不变、恢复另有记录；未知值、缺ID、重复映射、额外正文仍拒绝。 |

审校、裁决和修订继续共享冻结的近章完整双语、分层前情、候选元信息与目录，以及本章原文／当前草稿。新一版请求不注入上一版模型答案，也不注入已知错译清单或目标中文参考。格式恢复不另调用LLM，本轮 `repair_requests=0`。

代码入口：[prompt](../translation/review/prompts.py)、[审校与裁决](../translation/review/service.py)、[补丁约束](../translation/review/revision.py)、[协议解析](../translation/review/protocol.py)、[覆盖校验](../translation/review/coverage.py)、[残片信号](../translation/review/signals.py)。

## 2. 同资料下，prompt确实改变了结果

v2先执行两章审校与裁决。发现知识权威措辞存在歧义后，冻结单独的v3补充计划，在相同输入和模型设置下重新运行。两版都保留，不把v3结果倒写成v2成功。

| 开发案例 | audit-v2 | audit-v3及实际修订 |
| --- | --- | --- |
| 第1200章 | 仅提到师傅／师匠混用；错误标为历史用法且只引用本章T段，未通过历史证据准入。 | 发现体验者反转、T055称谓混写，以及一条有争议的“要不要放弃村子”意见；三条均被模型确认并进入补丁。原先五类已知问题中明确涉及2类，吸收对象、寝息增义、叛乱表述仍漏检。 |
| 第1233章 | 列出4条rank意见，覆盖6处残片；列出3处White，但裁决以“历史用法未审定”为由全部驳回White。 | 9条意见全部获模型确认：4条rank、3条White、国名及龙名各1条；实际改动8段，全部6处残片及3处White修复，另统一雷多斯、比斯德拉。 |

第1233章的White证据在v2已存在，例如 `R1D2P006` 的“那个穿着文官服装的怀特”。v3没有再添加这条资料，只澄清“本章沿用有正文支持的历史命名”与“批准全书规范”的区别，裁决就从驳回转为确认。因此，这次White问题不能解释为单纯缺少上下文或模型不认识该人物。

六份整章审校报告共22条finding，20条通过当前证据准入；这是工程统计，**不是20条意见均正确的语义评分**。例如v3第1233章f7引用S027，而对应名字实际位于S028；可定位的标签仍可能选错邻段。第1200章体验者意见的解释也夹有一处自相矛盾的说法，虽然主要判断和修复方向正确。

## 3. 修订效果与尚存缺陷

第1200章只修改 `T016／T046／T055`，总段数保持68，其余段落逐字不变。T046由“已经不再让我惊讶了呢”改为“已经不再惊讶了呢”，T055改用师傅。

然而T058仍为师匠，回归审校才找到它；吸收魔石被写成吸收个体、寝息被写成打鼾、叛乱分支的表述仍未被整章回归发现。严格补丁范围可以防止跨段冒领修复，却不能补全模型没有列出的出现位置。T016“商量要不要放弃村子”被改为“商量舍弃村子的事”，属于有争议的过度修订候选，不计作明确纠错成功。

第1233章只修改 `T010／T011／T012／T016／T020／T024／T028／T030`，总段数保持63，其余正文逐字不变。此前未修干净的T012本轮同时处理了rank和龙名。但是“B级（Rank B）级别”等重复表达仍然生硬，不能把残片消失解释成整章语言质量合格。

技能「虚言の理」仍未修订。模型先承认缺少实际历史正文，给出的 `chapter:main:涉及奥古斯特、技能「虚言の理」的章节` 也不是可执行的章号查询；本轮没有假装完成检索。回归时模型又仅凭候选实体卡确认“谎言的机理→谎言的道理”，程序将该确认隔离为未决。现阶段摘要中的导航线索尚不能可靠转化为一次有效的取证请求。

### 两次纯协议问题

第1200章的映射响应实际上完整列出68对S／T，失败原因是所有状态值都从 `mapped` 变成了 `已映射`，并非模型遗漏段落。新的显式映射profile从同一原响应创建独立派生对象，逐条记录68次枚举归一化，覆盖检查随即完整通过；旧失败对象和旧接受计划保留。该结果只证明段落结构覆盖，未证明句内含义覆盖。

第1233章回归裁决把最后的 `@@END` 写在reason末尾。限定条件的本地恢复保留了完整理由和原响应，没有发生新HTTP请求；恢复后的意见仍因证据不足被隔离。格式恢复没有给它补出事实依据。

| 章节 | 归一化后的覆盖／规则 | 当前接受阻断 |
| --- | --- | --- |
| 1200 | 覆盖完整；数值规则仍有“一”的宽泛计数差异 | `signal:numbers`、回归确认但尚未修复的T058问题 |
| 1233 | 覆盖完整；确定性检查没有信号 | 技能译名的历史未决记录及回归证据不足的finding |

两章仅各执行一次实际修订。没有执行第二轮语义补丁，没有通过人工驳回未决事项来制造自动通过。

## 4. 短上下文诊断：是否只是模型不会理解？

为区分整章搜索失败和局部语义判断失败，额外冻结两个短请求。每个请求均有相同的六个案例：体验者、吸收对象、寝息三个原始问题，以及依据日文作最小修改的三个正常对照；顺序固定。输入只有有关日文邻段和待判断中文，包含S052的魔石吸收说明，没有历史摘要、其他中文参考或期望答案。

第一个任务要求一般意义对照；第二个仅追加以下要求：

> 判断前请逐项核对动作执行者、体验者/受益者、动作对象，以及日文明确支持的细节和中文额外断言。在reason中简要体现这些对照，再给出结论；不要把表达自然或常见当作语义一致的证明。

| 案例 | 开发审阅期望 | 普通局部指令 | 加入逐项分解 |
| --- | --- | --- | --- |
| 原始体验者反转 | 存在错误 | 错误驳回 | 正确确认 |
| 修正后的吸收魔石 | 未见该错误 | 错误确认 | 正确驳回 |
| 原始寝息→打鼾 | 存在增义 | 错误驳回 | 正确确认 |
| 修正后的体验者 | 未见该错误 | 错误确认 | 正确驳回 |
| 原始吸收个体 | 存在对象问题 | 错误驳回 | 正确确认 |
| 修正后的睡眠呼吸声 | 未见该错误 | 正确驳回 | 正确驳回 |
| 本次分类结果 | 6个案例 | **1/6** | **6/6** |

普通任务甚至把“让我惊讶”解释为“不再对我吃惊”，并坚持“打鼾”未增加事实；这些错误在短上下文且材料齐全时仍出现，不能全部归因于长上下文注意力不足。逐项分解后，模型能够给出三个问题的正确判断和主要证据，说明它并非始终无法理解这些局部差异。

但是6/6仅是本次定向分类的结果：案例由已知问题选择，正常对照由开发审阅者构造，没有独立人工金标准；不是从整章自行发现错误的召回率。每种prompt只调用一次，也没有测量重复稳定性。分解版的寝息修复建议还加了日文未明确说明的“均匀”，所以分类正确也不等于修复文本逐词可靠。诊断输出没有被当作新裁决偷偷用于接受真实章节。

## 5. 对模型能力的判断与后续设计依据

当前端点的 `glm-5.3-flash` 在这组整章任务中，尚不能稳定承担无人复核的审校和裁决。问题包括漏检、把正确对照误判为错误、对自己错误解释的确认、证据等级混淆和最小修复中的新增断言。

目前证据同时支持两点：prompt与协议工程确实能解决一部分失败；语义任务执行的稳定性仍不足，不能只增加更长的前情来解决。尚未与第二个模型进行同输入对照，因此无法确定更强模型的收益，也无法据此宣称当前模型已经到达不可改善的上限。

下一步设计应把“局部含义检查”和“历史命名一致性”拆成独立任务：前者使用短段窗口，明确列出主体、体验者、对象、条件及增义；后者查询实际双语证据并枚举全部出现位置。补丁后对改动段逐句核验信息保留，对未改段继续检查同类残留。再使用保留案例和至少另一个审校模型做固定输入比较，分别记录漏检、误报、错误裁决和修订新增错误；这些实验尚未执行。

## 6. 成本、工程验证与复现

本轮独立账本总计 **18次真实请求**：审校6、裁决6、修订2、映射2、局部诊断2。输入826291、输出16445，合计 **842736 tokens**，不含之前P4／P5各轮开销；没有新增embedding或付费摘要。

使用现有 `glm-5.3-flash` 配置、temperature=0、retries=0、600秒超时；请求 `thinking=False`，但供应商仍报告部分reasoning tokens，故不宣称实际思考用量为零。18份响应均以 `stop` 结束，未触及输出上限，本轮语义漏检不能解释为输出截断。没有按token数估算货币费用。

完整翻译测试 **272 passed**，另有10条既有依赖警告；本轮专项18项涵盖历史证据绑定、无效依据隔离、版本独立、风格策略、补丁位置、残片枚举、句尾END恢复，以及枚举归一化不掩盖缺失／重复／未知ID。

本轮 [离线审计已通过](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/audit.json)：原版／v3／修订回归／两份覆盖计划／局部诊断全部从缓存重放，对象身份一致，18份完整实际HTTP请求payload全部核验；新增HTTP为0，账本及用量不变，769个受保护历史文件哈希不变。缓存缺失会立即失败，不允许回退网络。报告的32个本地链接也已核验。旧实验结果不重写，真实章节未调用接受事务。

```powershell
py -3.11 -m pytest tests/translation -q
# 以下重放只读取已有模型响应，不发HTTP。
py -3.11 scripts/verify_p5_audit_v2.py --phase audit
py -3.11 scripts/summarize_p5_audit_v2.py --compact
```

Python调用使用 `review_translation(..., review_profile='audit-v3')`，裁决及修订继承该profile；覆盖枚举兼容和残片检查分别显式指定 `map_coverage(..., profile='audit-v2')`、`run_checks(..., profile='audit-v2')`。CLI对应 `run --review-profile audit-v3`、`map --profile audit-v2`、`check --profile audit-v2`。默认legacy仍可重放历史请求。

## 7. 完整输入、输出与正文

产物根目录：`artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/`。

- [冻结初始计划](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/plan.json)、[v3补充计划](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/refinement-plan.json)、[诊断计划与单独保存的期望结果](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/diagnostic-plan.json)、[枚举恢复计划](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/mapping-recovery-plan.json)。
- 第1200章v3：[完整审校prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/05-review.prompt.md)、[审校输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/05-review.output.txt)、[裁决prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/06-adjudication.prompt.md)、[裁决输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/06-adjudication.output.txt)、[修订正文](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/1200-hierarchical/revised1.zh.txt)。
- 第1233章v3：[完整审校prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/07-review.prompt.md)、[审校输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/07-review.output.txt)、[裁决prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/08-adjudication.prompt.md)、[裁决输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/08-adjudication.output.txt)、[修订正文](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/1233-hierarchical/revised1.zh.txt)。
- [v2错误驳回White的裁决](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/04-adjudication.output.txt)、[映射原始枚举输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/10-mapping.output.txt)、[有句尾END且证据不足的裁决原响应](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/16-adjudication.output.txt)。
- 普通局部诊断：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/17-diagnostic.prompt.md)、[输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/17-diagnostic.output.txt)；逐项分解诊断：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/18-diagnostic.prompt.md)、[输出](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/io/18-diagnostic.output.txt)。
- [全部机器评估结果](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/results.json)、[对象身份及未决链](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/state.json)、[受保护输入清单](../artifacts/translation/tenken-cache-clean/verification/p5-audit-v2/protected-inputs.json)、[执行脚本](../scripts/verify_p5_audit_v2.py)。
