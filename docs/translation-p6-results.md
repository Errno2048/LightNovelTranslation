# P6：第1200章局部抽取与翻译对照

日期：2026-10-16。

后续针对串行索引 `main:601—700` 的 `main:701` 审校及P6验收判断，见 [第701章审校结果](translation-p6-701-review-results.md)。该窗口成功修复一处严重施受关系反转，但不满足P6阶段验收条件。

**本轮没有观察到整体翻译改善。补齐输出契约后，实际注入的是9条对白和4条待查问题，事件、人物状态、知情状态与实际译法均为0条。因此，本轮无法验证时序和知情信息本身的收益，也不能据此否定把这些信息挂在索引节点上的方案。** 未审对白候选中存在错误，P6译文出现与错误候选一致的退化；本轮没有运行P5审校接受链，没有接受真实译文。

这是一章开发样本、每个实验臂一次生成的观察，不是统计显著性检验，也不是P6阶段验收。判断由开发代理对照日文作出，没有独立人工金标准。

## 实验设置与实际输入

目标为 `main:1200`，站点章节为 `ja:syosetu:n6006cw:1202`。使用当前配置的 `glm-5.3-flash`，翻译temperature=0.2、输出预留8192、timeout=600秒、retries=0。配置请求关闭thinking，但供应商仍报告了少量reasoning tokens，不能宣称实际推理用量为零。

保留当前架构：按目标章节独立分析，不重建一级或二级索引。两轮均按顺序执行场景分析、对白分析、新鲜P5翻译、新鲜P6翻译，各4次请求。P5臂使用历史冻结的完整翻译messages，P6臂在同一前缀与目标原文之间加入候选工作集。使用新鲜P5臂是为了避免把模型相对旧译稿的随机变化误算为P6收益。

| 角色 | 输入 |
| --- | --- |
| 两个抽取任务 | 目标章完整日文、之前两章完整日文及可用中文对照、一次精确检索返回的两条历史日文段落。历史查询固定为 `魔石`；未加入目标中文、旧目标草稿或开发错误答案。 |
| 新鲜P5翻译 | 原冻结P5前缀：100章一级整体摘要、近处10章二级摘要、之前三章完整双语、翻译元信息、目标章完整日文。供应商计数47461输入tokens。 |
| 第二轮P6翻译 | 完全相同的P5输入，加13条未审候选及使用规则。新增块本地计数3072 tokens；完整输入供应商计数50557。 |

抽取资料仍不自动包含一级／二级摘要，scene和dialogue仍是两个独立任务，后者不读取前者模型输出。新增知识仍只存入本次分析overlay，未写回索引节点，也未提交成历史知识。中文参考在两臂生成完成后，才由离线评估脚本读取和导出。

## 原版试验与必要修正

原版保存在 [p6-pilot-1200-current](../artifacts/translation/tenken-cache-clean/verification/p6-pilot-1200-current/run.json)，完整请求、响应及当时的关键代码快照均保留。

原版场景输出把标记、JSON和END写在同一行，且多数记录只有引文，没有事件或状态字段；对白输出自行合成 `@@UTTERANCE_NEED`，另造channel和候选字段结构。两次分析的有效记录均为0。执行器当时仍继续生成两份译文，P6只增加了67个供应商计数的规则tokens，不能把该轮当作有效知识注入的比较。

为得到一次有实际候选的试验，在同一架构下作了以下修正，并冻结到独立的 [p6-run2计划](../artifacts/translation/tenken-cache-clean/verification/p6-run2/plan.json)：

- 准备和执行共享同一600秒公开配置，修复之前240／600秒不一致；新增独立输出目录选项和抽取I/O导出。
- 修复取证服务错误访问StateView上的policy／archive，改为读取底层NarrativeView；补充目标章原文必须显式选择的测试。
- 抽取prompt列出每种记录的字段、枚举和三行格式，分别说明通用引文字段，并在资料末尾重申契约。未提供任何目标章答案。
- 上下文中对白和NEED只注入一次，排除已拒绝／撤销的对白；零有效记录或工作集超预算时在翻译请求前停止。

这些改动没有将模型候选提升为已审事实，没有放宽逐字引文验证，也没有执行LLM格式修复或语义补写。

## 第二轮究竟抽取出了什么

| 类型 | 原始输出条数 | 通过格式及证据绑定 |
| --- | ---: | ---: |
| SCENE | 1 | 0 |
| EVENT | 6 | 0 |
| STATE | 3 | 0 |
| PERSPECTIVE | 5 | 0 |
| SUMMARY | 1 | 0 |
| UTTERANCE | 11 | 9 |
| NEED | 5 | 4 |
| USAGE | 0 | 0 |
| 合计 | 32 | 13 |

第二轮的JSON和记录标记已经可以解析。scene中的16条记录仍缺少 `evidence_ref` 或 `quote`，因而被拒绝，只有2条NEED通过。对白另有3条因改写或虚构引文被拒绝，留下9条UTTERANCE和2条NEED。两次分析状态均为 `partial_candidate`，不是成功的完整抽取。

原始scene文本确实概括了村庄避难、芙兰休息、师匠收纳尸体、系统切换和女神出现，也列出了师匠对国家局势的怀疑等5条视角记录；但是这些描述没有绑定证据，不能当作已抽取的可用事实。即便查看原始输出，也没有before等明确锚点间约束，不能说已经建立了时序图。

对白原响应还把原文 `護衛しながら` 改成 `護衛しないがら`，再输出一条NEED，询问这个“原文错字”如何处理。逐字校验同时挡住了伪引文及其派生问题，没有自动修复成看似合法的证据。这类错误发生在约8000 tokens的抽取输入中，不能简单归咎于百万token长上下文。

通过绑定也不代表含义正确。下面这条实际进入翻译上下文的候选，把 `（オン）` 归给了芙兰，并把通道定为telepathy：

```json
{"quote":"（オン）","speaker_candidates":[{"entity":"フラン","status":"inferred"}],"channel":"telepathy","review_status":"candidate"}
```

原章明确说芙兰仍在睡觉，师匠与乌鲁希商量；第二处回应更直接紧接“乌鲁希，芙兰就拜托你保护了”。错误候选没有利用这些反证。对一些其他台词，模型也仅凭引号形式或邻接推断就标成explicit，置信等级不够可靠。

另一个工程缺口是：目标章证据标签目前指向整章，`（オン）` 在章内出现两次，仅靠相同引文无法唯一定位发言。这比是否挂索引更细一层，后续需要保留段落或字符范围。当前overlay的candidate标签只能提示风险，不能阻止翻译模型采纳错误候选。

实际入选列表见 [scene简表](../artifacts/translation/tenken-cache-clean/verification/p6-run2/analysis/scene.compact.json) 和 [dialogue简表](../artifacts/translation/tenken-cache-clean/verification/p6-run2/analysis/dialogue.compact.json)，其中所有记录仍为candidate，reviewed为0。

## 翻译差异

下表以同轮新鲜P5和P6比较。原始日文、完整摘录和开发判断另存于 [development-assessment.json](../artifacts/translation/tenken-cache-clean/verification/p6-run2/development-assessment.json)。

| 检查项 | 新鲜P5 | P6候选工作集 | 判断 |
| --- | --- | --- | --- |
| 女神所说“不再吃惊”的体验者 | “都不再吃惊了呀” | “已经不会让你惊讶了呀” | 两边均未见旧稿的体验者反转，不能算P6独有修复。 |
| 吸收对象 | “吸收着还残留魔石的魔石” | “吸收掉残留魔石的尸体，把其余的……” | P5有重复病句；P6明确写成吸收尸体，又增加“其余”分流，未解决并加重语义问题。 |
| 寝息 | “打着鼾” | “沉沉地睡着” | 去掉打鼾增义，但也丢失呼吸声并增写深睡，只有部分改善。 |
| 乌鲁希两处回应 | 两处均为“（汪）” | 两处均为“（嗯）” | 与错误芙兰候选一致的退化；一次生成不能建立严格因果。 |
| 叛乱后的庇护 | “叛乱后给予庇护” | “接受东征公的反乱、提供保护” | P6出现国家接受叛乱的错误倾向。 |
| 信息宝库 | “信息的宝库” | “情报的宝库” | 两边都保留信息含义。 |
| 恢复时间未知 | 保留“不知道需要多长时间” | 同样保留 | 两边均正常，未重现旧修订的整句丢失。 |
| 仮称及称谓 | “暂称·师匠” | “仮称・师匠” | P6出现日文残留；两边都没有恢复历史的“师傅”用法。 |

**已有中文参考本身也把寝息写成“打鼾”。** 因此本轮以日文为语义判断依据，没有把参考译文当逐字金标准，更没有将表面相似度当作准确率。

本章不是专门设计的倒叙／插叙保留测试，时序和知情记录又没有实际入选，所以本轮不支持关于复杂叙事处理能力的结论。也没有更换模型或重复采样，不能把失败直接归结为模型家族的能力上限。

## 完整输入输出与核验

直接打开 [全文、上下文与原响应对照页](../artifacts/translation/tenken-cache-clean/verification/p6-run2/comparison.html)，可并排阅读日文、新鲜P5和P6，展开目标中文参考、实际P6工作集以及四个模型调用的完整prompt和输出。

- 场景：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/scene_analysis/prompt.md)、[原始输出](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/scene_analysis/output.txt)。
- 对白：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/dialogue_analysis/prompt.md)、[原始输出](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/dialogue_analysis/output.txt)。
- P5：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/p5_frozen_context/prompt.md)、[完整译文](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/p5_frozen_context/output.txt)。
- P6：[完整prompt](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/p6_candidate_workset/prompt.md)、[完整译文](../artifacts/translation/tenken-cache-clean/verification/p6-run2/io/p6_candidate_workset/output.txt)、[冻结上下文](../artifacts/translation/tenken-cache-clean/verification/p6-run2/p6-context.json)。
- [离线审计](../artifacts/translation/tenken-cache-clean/verification/p6-run2/audit.json)：重放两个抽取解析，重建P6 bundle，逐一核验四份实际HTTP payload及输出，账本不变、新HTTP为0；计划中6份受保护历史输入哈希不变。没有提交任何P6知识版本。

完整翻译测试为 **280 passed**，10条既有依赖警告；其中P6专项8项。测试通过只证明这些工程行为，不证明抽取内容或译文合格。

| 请求用量 | 输入tokens | 输出tokens | 合计 |
| --- | ---: | ---: | ---: |
| 原版4次 | 109507 | 8877 | 118384 |
| 补齐契约后4次 | 114313 | 6125 | 120438 |
| 本轮合计8次 | 223820 | 15002 | 238822 |

缓存命中是上述输入的子集：两轮合计112960 tokens，不另加到总数。没有embedding、新增全书摘要或额外模型审校请求，没有根据token数推算货币费用。

```powershell
py -3.11 -m pytest tests/translation -q
# 只重放本次缓存、生成核验与对照页，不发HTTP。
py -3.11 scripts/summarize_p6_pilot.py
# 查看账本，不重复发起试验。
py -3.11 scripts/run_p6_translation.py status --output artifacts/translation/tenken-cache-clean/verification/p6-run2
```

## 对后续改进的含义

本轮首先暴露了输出契约执行、证据绑定和语义消歧三层问题。增加候选数量或放宽引用校验不能替代解决这些问题。现有未审候选不适合直接成为翻译强约束，已知反证应进入语义审阅，并为重复台词保留唯一位置。

用户提出将事件、时序、知情信息随索引节点摘要一起抽取，本轮暂未实施。该方向可以扩大同一次抽取看到的叙事范围、复用已有阅读成本，便于建立跨章锚点和捕捉倒叙／插叙；但还需要分别保留故事时间、揭示位置和证据位置，不能仅通过汇总自然语言摘要自动产生确认关系。本次结果为这种后续设计提供失败样本，尚不是该架构的对照验证。
