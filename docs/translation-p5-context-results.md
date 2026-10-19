# P5 补充上下文后的审校、裁决与修订验证

日期：2026-10-16。

**结论：上下文传递、依赖隔离和接受门禁通过工程验证；真实章节的质量验收未通过。** 邻近双语正文解决了第1233章 `ホワイト→白色` 的历史译名证据不足，分层资料进一步帮助发现 `_rank_` 残留。但修订没有覆盖全部出现位置；第1200章仍有语义漏检，审校模型也持续生成无法逐字匹配的引文。本轮没有接受任何真实译文。

这是对 [P5首次实施结果](translation-p5-results.md) 的补充，保留原试验、原草稿及原裁决。新试验位于 `artifacts/translation/tenken-cache-clean/verification/p5-context-v2/`，不覆盖历史版本。章节编号使用 `match_key`：`main:1200` 和 `main:1233`，分别对应站点文档 `ja:syosetu:n6006cw:1202`、`ja:syosetu:n6006cw:1235`。

## 1. 本轮增加了什么上下文

新增 [context.py](../translation/review/context.py)，在原翻译上下文之外建立不可变的 `review-evidence-context`，绑定当前草稿版本和可见性策略。审校、裁决、修订三个角色在本章原文和草稿之前接收**完全相同的冻结资料前缀**。各角色后续的任务指令、意见、裁决及补丁hash仍各不相同；回归审校则绑定修订后的新版本。

前缀按以下顺序排列：

1. 资料使用规则：日文判断事实，历史中文核对译法，摘要和实体描述尚未语义审定。
2. 分层前情、实体描述与已观察译法：沿用已有一级/二级摘要，按当前原文及近章原文重新选择借入快照的译名元信息。
3. 含章节范围和部分标题提示的历史目录。
4. 邻近三章完整日文与完整中文，每段提供可引用的 `R` 标签。
5. 本章完整日文 `S` 段与当前完整草稿 `T` 段，随后才是角色任务。

`near` 配置只提供规则和完整近章双语；`hierarchical` 还提供分层资料和目录。`legacy` 保留旧审校提示词，历史验证脚本已显式选择该配置。

| 试验臂 | 近章范围 | 近章文档 / R段数 | 摘要及元信息块数 | 上下文估算tokens | 依赖正文版本数 |
| --- | --- | ---: | ---: | ---: | ---: |
| `1233-near` | 1230–1232 | 6 / 424 | 0 | 16,636 | 6 |
| `1233-hierarchical` | 1230–1232 | 6 / 424 | 15 | 49,925 | 2,315 |
| `1200-hierarchical` | 1197–1199 | 6 / 392 | 15 | 54,687 | 2,308 |

表中tokens只计算新增资料前缀，不包含本章原文、草稿和角色任务；实际API用量见第6节。15块包括12个一级概括、2个近二级概括和译名元信息。这里的“二级概括”不能理解成已提供所有块内细节：本轮复用的14份摘要 `content.details` 均为空，实际没有可注入的详细区间摘要。代码支持非空详情并有夹具验证，但该能力未在这批真实资料上发挥作用。

第1233章借入的是第1200章之前的历史快照，只概括到1199；1200–1229未新建摘要，1230–1232以完整双语补入。目录是导航资料，借入节点并未自动注册到当前版本的查询索引。当前原翻译上下文没有 `index_id` 时，节点查询会明确返回 `not_found / no_index_in_frozen_translation_context`；不能宣称目录中的每个借入节点都能通过工具取回。允许的章节查询仍可使用已有双语准入记录。

第1230–1232章使用整章正文的结构配对准入，**并非人工语义审定**。历史译名和摘要候选均未晋升为全书权威知识。本轮未新建付费摘要，也未把目标章已有中文参考或已知错译清单放进模型请求。

## 2. 对照方法与总体结果

两个第1233章试验臂使用完全相同的P3-B旧草稿；第1200章使用原P4.2草稿，没有先行修正已知问题。模型均为配置中的 `glm-5.3-flash`，审校和裁决分别发出HTTP请求，但同一模型的两次回答不构成相互独立的质量证据。

已知开发问题用于离线核对检出结果，不提供给本轮模型。这里区分“语义上提到问题”“形成有效定位/引文”“实际全部修复”，不能相互替代。这些章已经用于开发，不是盲测，也不足以估计稳定召回率。

| 试验臂 | 已知问题被提到 | 已知问题形成有效证据 | 其他结果 / 修订状态 |
| --- | --- | --- | --- |
| 第1233章上轮无前文补测 | 0/2 | 0/2 | 历史对照，未修订 |
| `1233-near` | 1/2 | 1/2 | 找到White，漏掉rank；全部4条finding可定位，但报告有未知字段错误；仅比较，不修订 |
| `1233-hierarchical` | 2/2 | 2/2 | 初审5条finding均可定位；应用一次修订，White全部修复，rank只部分修复 |
| 第1200章上轮独立审校 | 3/5 | 见历史报告 | 历史对照，不能与候选辅助结果混算 |
| `1200-hierarchical` 首次审校 | 1/5 | 0/5 | 2条finding引文均有问题，未修订 |
| `1200-hierarchical` 自身报告复核 | 2/5 | 0/5 | 3条finding引文仍有问题，未修订；不是第二次独立检出 |

第1233章的两类预登记问题是White人名误译和rank标记残留。第1200章的五类是体验主体反转、吸收对象、`寝息→打鼾`、师傅/师匠混用和叛乱条件措辞歧义。后者被模型提到但没有定位为需修复的语义问题，不计作有效检出。

## 3. 第1233章：历史译名问题得到缓解，修复仍未闭环

### 3.1 White 的证据和实际修改

近章正文提供了有效历史证据：

```text
R1D2P006 | 那个穿着文官服装的怀特从外表上看也不像是很强的角色。
```

两个新试验臂都据此判断 `ホワイト` 是人物名，提出将“白色”改为“怀特”。分层臂初审指出T016、T024、T028，裁决请求也得到相同资料，随后三个位置均实际修改：

```text
原文：最後、男たちはホワイトが手配してくれた衛兵に引っ立てられていった。
修订前：最后，男人们被白色安排的卫兵带走了。
修订后：最后，男人们被怀特安排的卫兵带走了。
```

这直接支持用户提出的解释：上轮White漏检确实受到前文和历史译法证据不足的影响。本轮是从完整近章取到证据，并非模型仅凭摘要猜出译名。

近章臂另外发现 `ビスドラ` 的译名差异和 `楽し気` 的语气问题，推测性的技能译名意见被裁决驳回。但模型在 `@@UNRESOLVED` 下输出协议不允许的 `confidence`，产生 `unknown_field:confidence`。因此“四条意见引文有效”不等于“整份报告完整有效”。

### 3.2 分层臂的修订和剩余问题

分层臂初审还发现 `レイドス` 译名差异、`虚言の理` 的已观察译法、rank标记和“转播/中继”建议。裁决确认了全部五项；其中把建议级用词调整也确认为修复事项，说明裁决仍有过度编辑倾向。

第一次补丁请求试图以T010的单段hash覆盖T010–T011，严格hash校验拒绝应用，正文未变。随后仅对增强上下文的修订提示增加明确要求：每条PATCH必须 `start=end`，跨段问题拆成多个单段补丁，原样复制所给hash。第二次请求成功应用10个单段补丁：

```text
T006 T010 T011 T013 T016 T020 T024 T028 T030 T062
```

这是**一个成功应用的修订版本**，不是两轮成功修订。该版本的段落数保持不变，其余段落逐字不变，重新映射完整，确定性检查没有信号。

但T012仍有 `_rank_A` 和“比斯多拉”；rank只修了T010、T011、T030。根因之一是补丁程序确认每个finding至少被某个补丁处理，却没有证明同一问题在全文的每次出现都已处理。模型初审未枚举T012，修订也没有自行补齐。

回归审校重新发现T012的rank和龙名问题，但三条意见的引文均不满足逐字匹配。裁决模型确认前两项、驳回一项可选风格建议；程序把缺少有效定位的两项确认隔离为未决。接受计划保留：

```text
unresolved_finding:f1
unresolved_finding:f2
```

之后尝试进入第二轮修订时，因为没有可用于修复的有效确认意见而直接跳过，没有新增HTTP请求。没有第二个修订版本，也没有接受该章。

改动仍不能视作全章质量保证。例如T013修订后的“他们只被看出了充当技能中继媒介的价值”仍显生硬。T030将领队的 `ランクB級` 写成“是B级”可以作为紧凑表达，原句并没有显式的“实力相当于”需要补回。完整改动及对应日文见审计结果，不能仅凭映射完整和段数不变宣称没有语义损失。

## 4. 第1200章：更多上下文没有解决语义漏检与引用失败

首次审校正确识别了女神对白中体验主体反转，另提出一句叹词的风格意见。但两条意见都存在无效引文，五类已知问题中没有一类形成可修订的有效证据。裁决模型确认两条，准入程序仍将其隔离，正文不变。

引文失败不是缺少原段。实际S046是：

```text
「その通り。あーあ、慣れちゃって、もう驚いてくれなくなっちゃったわねぇ」
```

模型却引用：

```text
「あーあ、慣れちゃって、もう驚いてくれなくなっちゃったわねぇ」
```

它删掉句首 `その通り。`，却为剩余片段补上开引号，导致引用不是原段的连续子串。中文也用同样方式省略“没错。”并补引号。另一条S051本来是无括号的叙述 `あ、相変わらずマイペースな神様だぜ。`，模型引用时额外加了 `『…』`。这是**意见内容与定位协议两个问题并存**：体验主体的解释方向可以正确，但当前引用不能用于严格自动修订。

为核验能否自行改善引用，本轮另冻结一次有界复核：相同原文、草稿及资料，加上模型自身上次的报告，不提供外部答案。它再次提到体验主体，并增加师傅/师匠混用意见，但三条意见仍没有有效引文；称谓意见还拼接多个标签/片段。裁决确认同样被隔离。没有生成或应用第1200章修订。

该复核属于 `followup`。旧保存对象沿用当时实现的 `mode=independent`，本轮保留原对象，审计依据 `prior_report_id` 明确记录 `effective_mode=followup`；当前代码已为新报告区分独立、候选辅助、上轮复核和取证辅助。不能把复核多发现一项当作独立检出提升。

吸收对象、打鼾增义和叛乱措辞仍未得到可靠处理。相较上轮独立审校提到3/5，本轮首轮只提到1/5；少量固定开发案例不能证明上下文导致退化，但已经足以否定“只要加足上下文就能满足P5质量验收”。

## 5. 工程实现与尚未验证的协议改进

| 接口 / 实现 | 本轮行为 |
| --- | --- |
| `compile_review_context(view, version_id, ...)` | 离线建立近章/分层资料快照，可显式借入已有前文快照 |
| `read_review_context(...)` | 重查版本绑定、正文依赖、隐藏/预留/截止范围，并按原配方重建校验 |
| `review_translation(..., context_profile, context_id)` | 审校保存实际使用的资料ID，支持旧提示词兼容 |
| `model_adjudicate(...)` / `propose_revision(...)` | 读取报告冻结资料，并注入相同前缀；补丁继承额外资料依赖 |
| `read_report(...)` | 重新解析原输出，核验实际请求中的前缀及本章正文边界，禁止把较多资料的请求冒充为较少资料的请求 |
| `review_with_lookup(...)` | 两次审校保持同一资料ID；读取不会暗中付费创建摘要 |

另增加了**实验性、显式启用**的 `reference_mode='paragraph'` / CLI `--reference-mode paragraph`：本章的 `source_ref`、`target_ref` 可只给一个实际S/T标签，程序绑定完整原段；外部历史证据仍必须给逐字引文。若模型主动提供引文，仍严格校验，不自动删改标点或模糊匹配。跨段问题需拆成多条意见。

该模式及报告读回、模式与实际prompt绑定、取证复核转发已通过离线测试。**本轮13次真实请求全部使用原来的literal模式，paragraph模式尚未做真实效果验证。** 没有用新模式重新解释旧坏引文，也没有把旧未决意见提升为可修订事实。段落标签有效也只表示可定位，语义判断仍要裁决。

新CLI入口：

```text
py -3.11 -m translation.review --corpus CORPUS --db DB context --version VERSION --profile hierarchical
py -3.11 -m translation.review --corpus CORPUS --db DB run --version VERSION --evidence-context CONTEXT_ID
```

`context` 是离线构建；`run` 在未提供mock时会请求模型。默认仍要求逐字引文，实验性段落模式需额外显式指定选项。

## 6. 调用、失败与离线验证

冻结上限为22次请求、输入1,500,000 / 输出360,448 tokens、上下文1,048,576，HTTP自动重试为0。第1200章的自身报告复核另有冻结计划，但仍受原总上限约束。

| 实际调用组成 | 请求数 |
| --- | ---: |
| 第1233章近章臂：审校、裁决 | 2 |
| 第1233章分层臂：初审、裁决 | 2 |
| 第1200章分层臂：初审、裁决 | 2 |
| 第1233章补丁：hash拒绝一次、成功应用一次 | 2 |
| 第1233章修订后：映射、回归审校、裁决 | 3 |
| 第1200章自身报告复核、裁决 | 2 |
| **合计** | **13** |

API报告输入 **618,210**、输出 **12,962**，合计 **631,172 tokens**。账本预留输入621,087、输出212,992。输出用量按供应商返回字段统计，不等于可见报告的字数；不据此估算货币费用。失败补丁的调用计入总量。无HTTP失败或计费未知的请求；一次恢复期间的本地内存错误发生在HTTP之前，后续优化已有摘要导入，未产生额外付费调用。

离线审计使用空 `MockProvider([])`：核对13份真实HTTP请求与保存的模型输入、12份审校/裁决/修订请求的共用前缀、原坏补丁仍被hash拒绝、唯一成功修订及回归阻断均可重放；映射请求不需要资料前缀。离线新增HTTP为0，账本用量不变，656个受保护历史文件hash不变，没有真实接受记录。

```text
py -3.11 -m pytest tests/translation -q
py -3.11 scripts/audit_p5_context.py
```

完整测试：**254 passed，10条现有依赖警告**。测试覆盖R引文、资料传递、依赖失效、可见性隔离、非空详情注入、上下文冒用拒绝、报告模式和实验性段落协议。这些工程测试通过不能替代模型质量验收。

## 7. 验收判断与下一步优先级

| 条件 | 判断 |
| --- | --- |
| 审校、裁决、修订获得同一份允许的丰富资料 | 通过 |
| 历史输入保留、预算有界、坏引用隔离、接受门禁有效 | 通过 |
| 找到White历史依据并在本章全部修复 | 本案例通过，3处均修复 |
| 已确认问题的全部出现位置均修复 | 未通过，T012仍残留rank |
| 第1200章五类已知问题可靠定位并提出可执行修复 | 未通过 |
| 修订后没有已知未决、可作为有效已译上下文接受 | 未通过，实际接受0章 |
| 稳定质量收益或自动连续翻译可用 | 尚未证明 |

后续优先处理五件具体工作：

1. 在有界真实调用中验证段落标签协议，区分“定位失败”和“意见本身错误”；外部证据仍严格核验。
2. 为重复译名、占位标记等记录所有出现位置和修复后的残留，而非仅确认某个finding被一个补丁覆盖。
3. 将“观察到的历史译法”“建议采用的本章写法”“已审定规范”明确分开。此次模型反复称未审译法为“固定/已审/既定”，裁决还沿用了这种说法，现有提示词约束不足。
4. 收紧风格建议的裁决，核验修订保留完整句意；不能把“引文存在”或“更喜欢某种表达”当作错译证据。
5. 对主体、对象、条件和省略补全分别验证语义召回，评估缩小到相关证据的审校方式，再扩大开发章范围。更多摘要不能代替这些检查。

当前可以继续P5的可靠性改进；尚不满足将真实草稿自动接受、进入无人工复核的连续翻译的条件。

## 8. 完整输入、输出及产物

所有路径均位于本轮独立目录，报告展示未删改模型原始意见：

- [冻结计划](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/plan.json)、[自身报告复核计划](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/reground-plan.json)、[离线审计](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/audit.json)。
- 第1233章近章臂：[完整审校prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/01-review.prompt.md)、[审校输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/01-review.output.txt)。
- 第1233章分层臂：[完整审校prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/03-review.prompt.md)、[审校输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/03-review.output.txt)、[裁决prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/04-adjudication.prompt.md)、[裁决输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/04-adjudication.output.txt)。
- 第1233章修订：[被拒绝的补丁输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/07-revision.output.txt)、[成功补丁的完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/08-revision.prompt.md)、[成功补丁输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/08-revision.output.txt)、[唯一实际修订正文](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/1233-hierarchical/revised1.zh.txt)、[回归审校输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/10-review.output.txt)。
- 第1200章：[初审完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/05-review.prompt.md)、[初审输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/05-review.output.txt)、[自身报告复核prompt](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/12-review.prompt.md)、[复核输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/12-review.output.txt)、[复核裁决输出](../artifacts/translation/tenken-cache-clean/verification/p5-context-v2/io/13-adjudication.output.txt)。
- 实验及审计入口：[verify_p5_context.py](../scripts/verify_p5_context.py)、[audit_p5_context.py](../scripts/audit_p5_context.py)。复现审计不调用真实模型；验证脚本的live阶段会请求模型，应沿用冻结预算和状态，不清空历史重跑。
