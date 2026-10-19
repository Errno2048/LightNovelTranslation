# P5 实施与开发验证

日期：2026-10-16。P5 的检查、取证、裁决、两轮补丁、回归和显式接受接口已实现；完成第1200章闭环及第3／1233章旧缺陷补测。当前模型仍有漏检、错误裁决和修订新增漏译，三章均未作为有效已译上下文接受。没有目标中文参考进入这些模型请求。

最新一轮见 [prompt、证据协议与模型能力诊断](translation-p5-audit-v2-results.md)：已增加显式v2／v3协议、完整段落绑定、历史用法分级、逐出现位置补丁约束与本地格式恢复。第1233章3处White、6处rank均修复；第1200章仍有漏检。短上下文六案例由普通任务1/6变为逐项分解6/6，提示明显的任务执行敏感性，不能解释成整章可靠。另计18次请求、842736 tokens，272项测试通过；真实接受数仍为0。下述首次试验和后续上下文试验均保留历史结果。

后续已完成 [补充上下文后的审校与修订验证](translation-p5-context-results.md)：近章完整双语与分层资料在审校、裁决、修订间共享；第1233章White的3处错译已修复，但rank仍有残留，第1200章引用失败和语义漏检未解决。本轮另计13次请求、631172 tokens，254项回归通过；P5质量验收仍未通过，真实译文接受数仍为0。下文为首次P5试验的历史记录。

## 1. 已实现的模块

新增 `translation/review/`，与既有根命令 `translation review` 的章节对齐审查分开，通过 `python -m translation.review` 使用。

| 模块 | 行为与约束 |
| --- | --- |
| `versions.py` | 冻结原文、完整上下文、策略、文本hash及稳定分段／偏移；修订创建新版本。空白行可豁免，省略号和短应答保留。 |
| `coverage.py` | 模型建立多对多段落映射；检查遗漏、重复、未知ID、乱序。单列 `mapping_complete`，不把结构覆盖当作语义覆盖。 |
| `checks.py` | 空输出、截断、不完整映射为阻断；数值、长度、日文残留和括号为需裁决信号。三十／30及全角数值可归一。 |
| `service.py` | 独立审校、候选提示后的核验、逐字源／译／外部引文检查、单独裁决、一次模型请求取证往返。引用存在与意见正确分开。 |
| `protocol.py`、`repair.py` | 平面记录解析，允许完整输出中的精确中间 `@@END` 分隔符，保留原响应及解析版本。缺终止符且记录内容完整时，最多一次模型格式转录；不得修改字段、意见、引文或补写内容。 |
| `lookup.py` | 整条修订链最多两个不同查询；可读允许的前文章节或已建节点摘要，明确返回禁止／未建／未找到。额外付费创建一个二级节点须显式调用，读取不会暗建摘要。 |
| `revision.py` | 仅修复已确认且有有效引文的问题；补丁绑定旧段hash、连续范围及finding；拒绝重叠、改段数和回滚式振荡。最多两轮语义修订，每个新版本重新检查。 |
| `requests.py` | 共用持久账本；调用前预留；原响应先存储再解析；缓存命中仍验证依赖。禁止HTTP自动重试，结果未知保留预留并阻止静默重发。 |
| `acceptance.py` | 显式接受计划及阻断清单；同一SQLite事务写接受对象、权威head、幂等键和outbox；CAS防并发覆盖。撤销保留历史。未核实的历史问题不会因新报告零发现而消失。 |
| `accepted_view.py` | 显式选择 `generated_accepted` 近章；保留接受ID及来源，消费者重新验证整个依赖闭包。撤销／换head／隐藏或预留依赖会阻断旧上下文。 |

额外检索证据会进入修订版本的 `additional_dependencies`，由后代继承。接受后的读取同时核验生成时策略与当前消费者策略，避免通过已接受译文绕过折内隐藏。历史摘要使用原始双语视图验证，接受的生成译文只替换显式选用的近章，不会倒改全部历史摘要。

`NarrativeStore.reserve()` 已改用 `BEGIN IMMEDIATE`，防止并发请求同时检查通过而超预算。接受候选知识只进本地outbox，不自动晋升知识库；跨库消费者仍须按event_id幂等处理，不宣称跨数据库原子提交。

## 2. 第1200章：分别验证检出、证据、建议和修订

沿用P4.2的原始草稿与完全相同的冻结上下文；历史索引只读导入独立P5数据库。五项线索来自目标参考导出之前的原文优先审阅，不能当作新盲测。独立臂不接收这些线索，辅助臂明确接收候选并重新核验。

| 预登记问题 | 独立审校 | 候选提示后 | 模型裁决 | 实际修复 |
| --- | --- | --- | --- | --- |
| S1 女神说“你不再吃惊”变成“不再让我惊讶” | 检出 | 引文和主要方向正确 | 确认 | 改为“你已经不再吃惊了呀” |
| S2 吸收魔石表达为吸收个体 | 漏检 | 引文和方向正确 | 确认 | 改为吸收残留魔石，保留尸体收纳 |
| S3 `寝息`增加为“打鼾” | 漏检 | 正确提出无依据增义 | **错误驳回** | 助手依据原文推翻驳回，最终写睡梦中的呼吸声 |
| S4 同章师傅／师匠混用 | 检出 | 正确指出混用 | 确认 | 本章统一师傅；不确立全书规范 |
| S5 面对叛乱变成“接受叛乱” | 检出 | 指出歧义，主要方向正确 | 确认 | 明确保留国家保护或勾结叛军的未知分支 |

独立臂提到了3/5项，辅助臂定位了5/5项；不能把辅助结果算成独立召回率。独立报告对S1的方向判断正确，但对惊讶者使用“芙兰方”的泛称仍不够精确；S5给出“国家受理”措辞也不是最好的解释。S2的语法说明过于简化，正确修复主要依赖尸体与魔石的处理上下文。

五条辅助意见的日中引文均有效，**并未证明模型可靠取得外部历史证据**。本轮显式读取1199和1198章及其允许中文并提供近章／候选元信息，但模型没有借此解决 `ジャン→约翰` 的历史译名核对；它随后请求1190章，因两个查询已用完而未执行。S4建议“师匠符合中后期约定”的范围判断也缺少充分依据，未采用。

模型裁决把S3解释成可接受的打鼾，暴露出同一模型多角色复核仍会互相错误支持。修订使用的是另存的助手原文裁决，保留原模型裁决，没有把人工干预冒充自动成功。

### 首轮修订制造的新问题

六个补丁修复五项原问题，但T024整段被缩为睡眠描述，丢掉两层信息：芙兰恢复行动之前无法护卫，以及不知道要花多少时间。**段落数量没有改变，映射仍完整。**

独立回归审校明确发现了恢复时间遗漏，但没有完整指出另一句护卫条件；其修复建议本身也不够完整。助手逐句核对S024后，要求同时补回两句，再进行第二轮修复。最终为：

> 总之，在芙兰能动起来之前，我们也无法担任护卫。问题在于，不知道会花费多少时间。芙兰至今仍满脸疲惫，发出睡梦中的呼吸声。

最终独立模型返回零发现。助手抽查五项原问题、两项新增缺句，以及撤离方向、国家立场未知、亡灵风险、系统切换、20级／99999和神明的未回答问题，未发现所检查含义的新增变化。只修改T021／024／039／046／055／058六段，其余正文逐字保持。该结论是有范围的开发审阅，不是整章人工金标准。

最终接受计划仍为 `review_required`：数值规则对“一”的计数有差异；历史译名核对和上一轮的前文解释请求也未被显式关闭。最终模型零发现不能覆盖这些记录。没有调用真实章的接受操作。

## 3. 第3／1233章：补测更早发现的问题

直接使用冻结的P3-B旧草稿，绑定当前清洗日文，未重写旧P3产物。模型只收到完整日文和旧草稿，没有错误清单、目标参考或前文知识。这是两个已知开发案例，不是盲测；本补测不做整章映射和接受。

| 章节 | 原有问题 | 结果 |
| --- | --- | --- |
| 3 | 不明怪叫被译成“去死吧”；普通气息变成杀气 | 独立发现2/2，引文与修复方向正确；修订均修复，并改正“最先把倒的”病句。仅改T006／009／106，其余逐字保持。 |
| 1233 | `ホワイト`人物名译成白色；`_rank_`标记 | **独立漏检2/2**。另报一项有合理方向的时态问题，但源／译引文都提前补了闭引号，严格匹配失败，不能据此修订。 |

第1233章的模型确认被隔离成未决；助手曾尝试确认，同样被准入层拒绝。该失败决定另存为 `main-1233-decisions.rejected.json`，之后明确记未决，没有修订此章，也没有偷偷改正模型引文。

第3章回归仍提出拟声词、单复数、重复感叹词和文风建议，部分引用包含合成省略号及原文不存在的字形。“日语名词语法本身标记单数”“重复感叹必然增加事实”等判断不可直接接受；本轮没有把所有模型意见都计作正确缺陷。已修的两项语义错误未重现，改动段的空间位置和技能推断保留；没有因此接受整章。

## 4. 调用、预算、失败与恢复

| 独立账本 | 真实请求 | API输入 | API输出 | 合计tokens |
| --- | ---: | ---: | ---: | ---: |
| 第1200章 | 12 | 152318 | 11482 | 163800 |
| 旧草稿补测 | 6 | 46267 | 5505 | 51772 |
| 本次P5合计 | **18** | **198585** | **16987** | **215572** |

不含P4.2此前已知8772552 tokens和其未知超时用量。输出包含供应商记入的思考tokens；请求了关闭thinking仍有部分响应报告思考用量，不能宣称完全关闭。全部请求使用现有配置的 `glm-5.3-flash`、temperature=0、retries=0、600秒超时。没有新增embedding或二级摘要构建，不根据token数估计货币费用。

第1200章冻结上限为13次、输入700000、输出262144、上下文1048576，类型分别上限为映射3／审校4／裁决3／修订2／格式修复1／摘要1，总上限共同约束。原设计的8次估算漏掉了每个修订版本的重新映射，本次另有候选提示对照和一次解析器变动后的新裁决，因此按完整开发方案冻结13次。补测独立上限9次、输入200000、输出147456；没有借用主试验剩余额度。

12次主试验请求包括3次映射、4次审校、3次裁决、2次修订。最初解析器只允许外部L引文，错误拒绝了实际有效的S／T跨段证据；保留原始输出及失败流程，更新带版本的解析规则后，发生一次新的裁决请求。辅助审校和补丁还出现中间 `@@END`，在保留完整最后END、无孤立文字或未知字段时可本地恢复；没有调用模型格式修复，没有通过解析器修改内容。

两份离线审计都以 `MockProvider([])` 重放全部成功路径，任何缓存缺失直接失败，不允许HTTP；新增请求0、账本与用量不变，正文和修订身份保持，冻结输入hash通过。主试验保护原语料、P4.2整轮产物和旧知识库；文档更新不会覆盖历史结果。当前解析器下初始报告有新的解析对象身份，旧对象仍保留；这不等于重新生成审校文本。

## 5. API与操作入口

| Python接口 | 用途 |
| --- | --- |
| `create_version(view, context, text)` | 导入待审草稿，返回不可变版本ID |
| `map_coverage(session, version_id)`、`run_checks(...)` | 返回绑定版本的覆盖和规则记录 |
| `review_translation(session, version_id, include_context=...)` | 审校；候选线索用独立的 `candidate_observations` 参数显式标识 |
| `review_with_lookup(...)`、`resolve_review_queries(...)` | 一次模型请求取证往返／显式读取 |
| `model_adjudicate(...)`、`adjudicate_findings(...)` | 模型／具名原文裁决，原始记录分开保存 |
| `propose_revision(...)`、`apply_revision(...)` | 提议及应用补丁，返回新版本，需重新检查 |
| `plan_acceptance(...)`、`accept_translation(...)` | 查看阻断清单后，以expected_head和idempotency_key显式提交 |
| `read_accepted_pair(...)`、`AcceptedNarrativeView` | 显式消费有效已接受近章 |
| `invalidate_acceptance(...)`、`pending_outbox(...)`、`publish_outbox(...)` | 撤销和独立的候选知识投影 |

CLI通用入口：

```powershell
py -3.11 -m translation.review --help
py -3.11 -m translation.review --corpus artifacts/translation/tenken-cache-clean/corpus --db PATH prepare --context CONTEXT.json --draft DRAFT.txt
# 后续子命令：map、check、run、lookup、adjudicate、revise、plan-acceptance、accept、status、invalidate
```

`--config`可选择模型公开配置；真实审校要求生成配置的 `retries=0`。`run --source-only`关闭近章候选注入，`run --resolve-lookups`执行一次受限取证；没有无限工具循环。`plan-acceptance`通过 `--signal-decisions`／`--unresolved-decisions`读取具名决定，历史未决ID可在返回的计划中查看。默认不自动接受。

离线复验：

```powershell
py -3.11 -m pytest tests/translation -q
py -3.11 -X utf8 scripts/audit_p5_1200.py
py -3.11 -X utf8 scripts/audit_p5_prior_errors.py
```

原始调用／缓存恢复入口为 `scripts/verify_p5_1200.py --phase ...` 和 `scripts/verify_p5_prior_errors.py --phase ...`，已有成功项复用；更换prompt／模型会改变请求身份，必须新建显式计划，不删除旧账本重跑。

## 6. 验证范围与后续重点

P5专项49项通过，翻译完整回归239项通过，另有10个既有依赖警告。测试覆盖错误引文隔离、空报告不等于无错、多对多覆盖、源／策略变化、截断与未知结果、原响应恢复、补丁hash冲突、两轮限制、并发预留、事务回滚、幂等、撤销、真实近章编译消费、额外检索依赖和历史未决问题继承。事务故障和接受消费在合成夹具验证，未对本轮真实章调用接受。

工程闭环具备了可复现的定位、取证、修订与阻断能力；质量瓶颈仍是审校模型本身。下一步应优先比较不同审校模型、强化实体身份与正文格式检查、按子句验证修改段的含义覆盖，并对新开发章和保留集分别测误报／漏报／新增错误。多模型、长期人物知情与时序、连续自动调度和全书质量认证仍未实现。本次不把单章辅助成功或两章补测解释成稳定收益。

## 7. 产物

- [第1200章逐项评估](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/evaluation.md)、[机器核验](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/evaluation.json)。
- [日文／初稿／两轮修订完整对照](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/comparison.html)、[最终修订译文](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/revised2.zh.txt)。
- [独立审校完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/02-review.prompt.md)、[辅助审校完整prompt](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/05-review.prompt.md)、[辅助输出](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/05-review.output.txt)。
- [错误裁决原文](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/06-adjudication.output.txt)、[首轮补丁](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/07-revision.output.txt)、[修订回归](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/09-review.output.txt)、[第二轮补丁](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/io/10-revision.output.txt)。
- [阻断的接受计划](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/live/acceptance-plan.json)、[测试记录](../artifacts/translation/tenken-cache-clean/verification/p5-review-1200/tests.xml)。
- [第3／1233章补测评估](../artifacts/translation/tenken-cache-clean/verification/p5-prior-errors/evaluation.md)、[完整机器记录](../artifacts/translation/tenken-cache-clean/verification/p5-prior-errors/evaluation.json)、[第3章修订正文](../artifacts/translation/tenken-cache-clean/verification/p5-prior-errors/main-3-revised.zh.txt)。
