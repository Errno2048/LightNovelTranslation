# P4.1 实施与验证：分块索引和分层前情

日期：2026-10-16。H0–H5 已完成工程实现、离线验收和一个目标章的真实对照。**索引与上下文闭环可用，尚未证明分层摘要带来稳定翻译收益；摘要构建仍需人工介入，H-tiered 不默认启用。**

依据：[分层设计与计划](translation-hierarchical-context-design.md)、[总体规划](translation-pipeline-plan.md)。原 P4 冻结产物及语料保持不变。

完整真实 prompt、原始/修复后摘要、实际注入内容及原因分析见 [输入输出审计](translation-p4.1-prompt-audit.md)与 [浏览页](../artifacts/translation/tenken-cache-clean/verification/p4.1-main-45/io-audit/index.html)。该轮只读导出，新增模型调用 0。

## 1. 已交付功能

| 功能 | 实现与行为 |
| --- | --- |
| 来源与窗口契约 | `translation/narrative/schemas.py`；区分目标日文、前文日文、允许中文、隐藏译文与保留资料 |
| 本地分块索引 | `translation/narrative/service.py`；标题候选、容量边界、显式覆盖配置、精确块/区间/章节成员及依赖 |
| 完整近章读取 | 完整日文和经范围核对的中文；缺译或受阻时保留日文，不回填更远章节 |
| 不可变对象与账本 | `translation/narrative/store.py`；内容寻址对象、请求前预留、失败留存、显式重试及成功复用 |
| 摘要闭环 | `translation/narrative/summaries.py`；plan/run/review，一次读取完整允许范围，输出总览与全部区间详情；逐字引文和区间校验 |
| 三层上下文编译 | `translation/context/hierarchical.py`；完整近章、近块总览/详情、全部远块总览；确定性 prompt、预算和实际注入清单 |
| 翻译与 CLI | `translation/narrative/{translation,cli}.py`；dry-run、C/H-near/H-tiered、缓存恢复、独立叙事命令 |
| 模型配置 | 可选 `reasoning_effort`；未配置时不改变历史 public 身份。截断错误保存 request ID、finish reason 和 usage |

缺必需摘要报 `coverage_gap`，过期或越权分别拒绝；装不下报 `budget_exceeded`，不会截断完整正文或静默减少 N。发送翻译请求前重新编译，检查来源和审查变化。跨模型配置复用摘要须显式固定 summary ID，保留原生成身份。

`narrative-translate` 从 bundle 固定的 ID 恢复不可变整章范围审查，也可通过 `--chapter-reviews` 指定修订；不自动选取最新审查。恢复操作不得改变实际 prompt。

完整中文范围资格只证明纳入了所列中文章节的全部正文段落，不证明没有漏译、错译或主体误解。P4 局部 donor 审查不升级为整章语义资格。摘要引文校验也只证明定位和引用有效，不证明陈述正确。

## 2. 索引范围与资料隔离

全书无模型导航索引包含 **1357 个日文叙事条目、35 个候选块**，配置为每块 20–40 话、区间 4 话并受 token 上限约束。标题只提供候选线索，容量也会触发切分；这些不是 35 个已确认的剧情篇章，未生成全书摘要。

真实开发试验目标为 `main:45`，原文 ID 为 `ja:syosetu:n6006cw:47`。完整近章为 `main:42–44`，`N_chapter=3`、`N_block=2`。生产目录与目标折目录隔离，生产目录及其后文边界不进入实验 prompt。

- 从冻结的 `evaluations/splits.json` 继承全部 validation/blind-test 保留集合；目标折 CLI 拒绝遗漏该集合的政策。
- 第 45 章之前的 `main:5/23/28` 因保留政策排除，其日文、中文和派生摘要均不可用。
- 中文 allowlist 仅为 `main:2/4/42/43/44`；前两章提供同一组 P4 donor 和允许的早期冻结约定，后三章提供完整近章参考。
- 目标中文仅在全部生成后读取作诊断比较，不进入 prompt、摘要或约定。
- K0 复用 P4 main-3 的允许早期证据，不从后部折补词；匹配非空日文词形或已审查风格项，并校验来源和父修订。

允许的 41 个前文章节分为 6 块：`1–4`、`6–20`、`21–22`、`24–27`、`29–32`、`33–44`。最后两块为近块，其余四块只注入总览。详情每 4 话分组；与完整近章部分重叠时保留整个详情，不切割摘要句子。保留缺口在 prompt 中明示，不补造缺失剧情。

覆盖完整仅指当前政策允许的全部范围，不能声称拥有未经遮蔽的完整前情或已验证全书长程回忆。

## 3. 工程验收

完整回归 **103 passed、10 warnings**，警告均来自既有 `pkg_resources`。证据为 [pytest XML](../artifacts/translation/tenken-cache-clean/verification/p4.1-pytest.xml)。

新增场景涵盖三层选择、精确前缀、缺摘要与超预算、目标/未来/保留资料隔离、不回填缺章、中文隐藏仍保留日文、局部范围不升级整章、传递来源与父修订、依赖失效、确定性、截断和无隐式重试、成功恢复、跨配置显式摘要复用、拒绝审查阻断，以及 CLI 恢复整章范围审查。缓存索引验证改为批量载入正文，避免逐章重扫整份 JSONL。

真实产物离线复验确认：

- 六份摘要覆盖全部 41 个允许前文章节，三份 bundle 可逐字段重新编译。
- 三份译文在 Python API 和 CLI 中均可缓存恢复；验证提供方禁止发请求，**新增模型调用 0**。
- **190 个受保护的 P4/语料文件哈希不变**；未新增 embedding。
- 最新业务代码另以实际语料完成 mock 全链路，由 `latest-offline.json` 指向；mock 与真实产物隔离，不作语义证据。

真实生成之后修复了一处 CLI 审查记录恢复遗漏，未改变生成 prompt。`verification.json` 分别记录生成时和最终复验时的实现指纹，确认最终代码重建的 bundle 与生成时完全一致，无需重新付费生成。

## 4. 摘要构建实际表现

main-45 实际进行了 **7 次摘要请求，得到 6 份结构有效摘要**：2 份直接通过，4 份保留 raw 和失败账本后经人工检查、本地恢复，另有 1 次截断无产物。失败账本没有改成 completed，也没有通用自动修复器。

| 范围 | 实际结果 | 本地处理 |
| --- | --- | --- |
| 1–4 | 直接通过 | 无 |
| 6–20 | 两条引文不逐字相符 | 从同一 ref 复制准确原文替换 quote；摘要正文不变 |
| 21–22 | 直接通过 | 无 |
| 24–27，首次 | 16000 输出上限截断 | 保留失败；更改显式配置后另发一次有界请求 |
| 24–27，第二次 | JSON 包装错位及两条空重复区间 | 修复包装并移除空重复；摘要句子不变 |
| 29–32 | 两份互补详情共用 range ID，一处 ref 偏一行 | 合并为计划要求的四章区间，修正定位 |
| 33–44 | 详情内容跨区间，一处 ref 偏一行 | 根据原文把 39–40 章内容及证据移回第二详情，第三详情从 41 章开始；不增删摘要句子 |

四次修复由绑定具体差异的 `p4_1_repair_{citations,structure,ranges,partition}.py` 完成，有独立 repair 元数据、原 raw 和失败结果备份。它们说明错误可追溯、可恢复，**不代表摘要构建已经全自动可靠**。本地恢复无额外模型调用，语义仍未获完整审查批准。

抽查问题另存于 `summary_quality_review.json`：

| 问题 | 是否注入 H-tiered | 观察 |
| --- | --- | --- |
| 6–20 总览把 `フラン` 自译为“法兰” | 是 | 违反保留日文原名指令；三份输出仍全部用“芙兰”，本例未见误名传染 |
| 将 `不都合のない範囲で` 摘作“在不便范围内” | 否，属于远块详情 | 条件失真，不能归因为本轮翻译错误来源 |
| unresolved 仍问西侧 Boss 是否存在 | 否，该字段未独立注入 | 第 11 章已说明最后 Boss 被击败，未决状态未正确消解 |
| 29–32 总览及详情整份使用日文 | 是 | 未遵循中文摘要要求；结构和证据校验不检查输出语言 |

目录覆盖、结构有效、证据有效、语义正确和模型采用需要分别统计，不能合并为一个摘要成功率。

## 5. 三组真实翻译结果

三组共享目标日文、短 donor、冻结约定、模型配置和 9600 输出预留。模型为本地配置的 `glm-5.3-flash`，显式 `thinking=true`、`reasoning_effort=low`、`retries=0`、timeout 480 秒。C 是本目标重跑基线，不能与 P4 第 3 章旧 C 直接比较。

| 配置 | 输入 | API 输入 tokens | API 输出 tokens（含思考） | 时延 | char-F2 |
| --- | --- | ---: | ---: | ---: | ---: |
| C | 短 donor + 冻结约定 | 5626 | 3318 | 65.296 秒 | 32.138 |
| H-near | C + 3 个完整近章日中正文 | 22354 | 3388 | 69.156 秒 | 32.598 |
| H-tiered | H-near + 2 近块总览/详情 + 4 远块总览 | 27031 | 3460 | 66.531 秒 | 32.494 |

三次均正常结束，未触及输出上限。H-tiered 相对 H-near 增加 4677 输入 tokens，约 20.9%；相对 C 约 4.8 倍。单次时延波动不用于推断速度收益。

char-F2 只是与现有参考的字符重叠，参考自身存在语义问题，不是金标准；这些差值不能证明翻译质量改善，也不能用于配置排名。

### 5.1 按原文检查的观察

完整定位和片段见 `translation_quality_review.json`。下列段落号均属于 `ja:syosetu:n6006cw:47@ea7b01cff504616a`：

| 原文位置与要点 | C | H-near | H-tiered |
| --- | --- | --- | --- |
| p0071 等：`ギルマス` 是公会长简称 | 公会会长 | 公会会长 | 20 处译作“吉玛斯”，职位当成人名 |
| p0126：多年无法从 E 级晋升 | 保留方向 | “连 E 级都升不上去”，方向错 | 保留方向 |
| p0004：C 级委托耗时长 | 保留耗时含义 | 加入“长时间待在原地”，无依据地点限制 | 保留耗时含义 |
| p0092：“女儿”而非“妹妹”的玩笑 | 应该是女儿 | 是女儿才对 | “是女儿的错”，误解 `間違い` |
| p0186：契约只能一个，誓约可有多个 | 保留 | 保留 | 保留 |
| p0210：怀疑会长是否察觉魔石需求 | 保留疑问 | 保留疑问，但加入“把柄被抓住”含义 | 保留疑问 |
| p0008：`ガルス` | 加尔 | 加尔留斯 | 加尔斯 |

第 42 章允许中文使用“加留斯”；三种输出均未准确沿用，说明完整近章不能代替明确译名约定。三层组还把 C/H-near 的“涅尔、阿曼达”改成“奈尔、阿曼妲”；这是配置间漂移，不能仅凭本例判定哪个新译名本身错误。

“吉玛斯”没有出现在输入中文中。相应日文简称同时出现在目标、近章和新增摘要，因而只能确认 H-tiered 发生误译，不能确定归因于某一摘要。总览的“法兰”虽实际注入，但三组输出各 16 次使用“芙兰”，未见采用该误名。

三组状态/技能/装备表的阿拉伯数字序列与原文一致；这不认证技能名称正确或全章无漏译。“契约/誓约”区别在目标本身已明确，不能作为利用远程知识的证据。H-near/H-tiered 还保留若干“魔獣”字形，书写一致性仍需检查。

### 5.2 效果判断

本次证明分块索引、三层选择、来源隔离、有界调用和可复现缓存能够协同工作。**未证明 H-tiered 稳定优于 H-near 或 C，且出现职位误作人名、局部误译及既有译名未沿用等负例。**

只有一个开发目标、每配置生成一次；没有远程事实专项题、独立人工盲评、全章语义覆盖标注或跨模型比较。不能验证数百章回忆、人物知情推理、局部注意力适配或连续翻译质量。H5 是有界试验完成，不是生产质量认证。

## 6. 用量、失败与缓存

累计硬限制：摘要 12 次、翻译 12 次、全部 24 次；输入 2M、输出 150K tokens。所有失败和显式重试计入保守预留。

早期 `p4.1-main-15` 的 3 个请求各预留 7000 输出 tokens，全部截断；旧提供方未保存实际 usage，只能报告预留。该探索脚本最初未继承保留集合，第一块含保留的 `main:5` 日文；无有效摘要或后续译文，**不能作为严格隔离试验的质量证据**。main-45 使用新政策和数据库，仅导入旧失败的预算记录，不导入知识。

| 项目 | main-45 已知实际用量 | 说明 |
| --- | ---: | --- |
| 摘要 | 7 次；输入 196746、输出 55124 | 含截断及四次无效输出；输出内思考 40334 |
| 翻译 | 3 次；输入 55011、输出 10166 | 输出内思考 9 |
| 已知合计 | 输入 251757、输出 65290 | 不含旧 3 次未知用量，不是整轮精确账单 |
| 累计账本 | 摘要 10 次、翻译 3 次，共 13 次 | 含旧失败 |
| 累计保守预留 | 输入 314699、输出 149800 | 不因实际少用自动回收 |

输出预留：旧失败 `3×7000` + 前四次摘要 `4×16000` + 后三次摘要 `3×12000` + 三次翻译 `3×9600` = **149800**，仅剩 200，本轮不再追加调用。预留是请求前门禁，不等于已付费 token。

前四次摘要未显式设置 effort，出现大量思考输出；随后新增可选配置并切为 `low`，通过显式 ID 复用已有效的前三份摘要。模型配置和公开参数资料快照单独保留，不将本次结果解释成跨模型规律。

main-45 已知摘要累计约 858.498 秒、翻译 200.983 秒，是请求用时之和，不含人工检查和本地修复；截断摘要时延取自 provider HTTP 事件，不含本地解析开销。旧三次失败时延未计入。H-tiered 返回 cached tokens=576，其余为 0；单次命中不能证明稳定 KV 缓存收益，更未验证窗口滑动后的命中。

输入使用公开 tokenizer 本地计数并加估计聊天开销；API usage 另存。128K 是本轮编译约束，没有测试 1M 端点能力，没有新增 embedding。

## 7. 产物与复现

真实产物根目录：[p4.1-main-45](../artifacts/translation/tenken-cache-clean/verification/p4.1-main-45/)。

| 入口 | 用途 |
| --- | --- |
| [review.html](../artifacts/translation/tenken-cache-clean/verification/p4.1-main-45/review.html) | 原文、C、H-near、H-tiered、现有参考五栏对照；不声称段落逐一对齐 |
| `global-title-index.json` / `block_index.json` | 全书导航候选与隔离目标折索引 |
| `summaries.json` / `summary_ids.json` | 六份精确范围摘要与固定 ID |
| `bundle-{C,H-near,H-tiered}.json` | 实际 prompt、层级、来源、预算和指纹 |
| `runs/*/request.json` / `translation.json` | 请求及生成草稿 |
| `result.json` / `verification.json` | 原始结果、用量及最终离线复验 |
| `summary_quality_review.json` / `translation_quality_review.json` | 选取的质量问题、原文定位与注入/输出追踪 |
| `narrative.sqlite` / `provider_events/` / `local-*-repair.json` | 不可变对象、失败账本、事件和修复来源 |

离线复现：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3.11 -m pytest tests/translation -q
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_1_run.py --mock
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_1_validate.py
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_1_review.py
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_1_quality.py
```

真实入口 `p4_1_run.py` 已完成项复用、失败不自动重试；要求当前代码已通过 mock 预检，再次执行仍受账本上限约束。不要删账本后以同一试验名重复付费。

通用命令提供 `--help`：`narrative-index`、`narrative-plan`、`narrative-summarize`、`narrative-boundaries`、`narrative-review`、`narrative-context`、`narrative-explain`、`narrative-translate`、`narrative-export`。例如：

```powershell
py -3.11 -m translation --book tenken-cache-clean narrative-index --help
py -3.11 -m translation --book tenken-cache-clean narrative-context --help
py -3.11 -m translation --book tenken-cache-clean narrative-translate --help
```

## 8. 后续优先级

1. 扩大构建前，增强摘要结构生成可靠性、语言/译名指令检查，以及否定、未决状态和跨区间事实的语义复核；格式恢复不能替代这些检查。
2. 加入有角色描述、职位/简称和获准译名的核心实体卡，定义其与摘要和近章参考之间的优先级。
3. P5 加入术语、身份误解、漏译和增义检查与接受事务，避免直接把本轮草稿当后续可信译文。
4. 在可靠参考子集扩展跨块、长程回忆和对白样本，做多目标/重复生成及同预算对照，再决定 N 值和默认配置。
5. P7 实现已接受译文提交、增量前缀、连续压缩及滑动窗口缓存；首版前缀重建仍读取匹配范围原文。

本轮没有新增完整人物图、人物知情推理、全书描述卡、连续翻译自动提交、通用工具循环或全书付费摘要。
