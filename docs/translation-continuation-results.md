# 续译定位 0.4.0：蜘蛛两组真实验证

日期：2026-10-19。结论：两组均完成排序、独立作用域摘要、目录召回、正文查证并返回 `suggested`；两组 `auto_eligible` 都是 false。已验证工程可运行及缺前缀处理，尚不能声称两组都已准确识别唯一、可无缝承接的续译起点。

## 1. 输入、结果与判断

源文为用户本地 Web EPUB，共 600 个标准化叙事章。中文只使用相应的三个卷文件，没有将其余中文卷或其摘要加入定位输入。测试故意打乱文件顺序。

| 场景 | 输入 → 规范顺序 | 中文终点 | 模型建议 Web 整章 | 0 基叙事位置 | 自动采用 |
| --- | --- | --- | --- | --- | --- |
| prefix-03 | 3/1/2 → 1/2/3 | 第 3 卷「终章 离开迷宫喽！」 | １５２　ぶらり蜘蛛一人旅 | 198 | 否 |
| window-09 | 9/7/8 → 7/8/9 | 第 9 卷「妖精笑了」 | ２２８　ぬぁぜだ！？ | 356 | 否 |

稳定 ID 分别是 `spider-ja:entry:ebf57778301298c35b38` 与 `spider-ja:entry:dff5976f652320244322`。152/228 是原始标题号，198/356 是标准化语料位置，调用时使用稳定 ID。7–9 卷保持真实卷号；1–6 被记录为本次未提供的前缀，未变成从头补译任务。后记与卷首书名页未被当成剧情终点，主线终章、幕间保留。末卷完整性仍为 unknown。

**前 1–3 卷。** 中文终章停在告别迷宫、考虑进入人类城镇。Web 151 末段处在首次来到阳光下、准备散步的状态；152 随后展开外界观察、检问所与砦、选择山与海的行动。从故事阶段看，152 是合理的有限建议，开头可能重复离开迷宫的状态。但当前仅有一个 partial、一个 uncertain 锚点，没有核验所有并行线及 Web 独有前置事件；不能证明它是唯一边界或保证没有补读需求。

**仅 7–9 卷。** 系统先召回「働かざる者食うべからず」「腹黒」「嫉妬の鬼と憤怒の鬼」。局部核验否定直接推荐首候选227：该章是主角交付文书换取餐食，中文卷末则是波狄玛斯介入反叛、准备兵器。模型主动 REQUEST 228 全文，随后推荐228：它从魔王视角展开获知瓦基斯政变证据、请求第四军先制镇压及批准。这个建议有反叛支线的局部依据，但只有一个 partial 锚点。中文末端摘要还记录了拉斯获救、与 D 会面等其他进展，不能用反叛线一条边界代替它们；228 可能造成较长重复阅读，尚未量化，也未排除更合适的跨线承接位置。

因此，目前验收是“两个输入场景均可运行并产生可审查建议”，不是“续译定位准确率 2/2”。本次没有自动启动翻译，也没有使用新模型互相投票充当人工真值。

## 2. 可复核证据与完整 I/O

prefix 的主要锚点为 `J79af50bb3ae6P0099` ↔ `Z68f456dd041cP0008`（离开迷宫后的外出状态，partial）。第二锚点被模型自己标为 uncertain，不计入可靠共同事件数量。最终证据落在完整提供的152章。

window 的锚点为 `Ja994a4b85a55P0093` ↔ `Z168a05fd6568P0007`：前者报告瓦基斯政变，后者是波狄玛斯谈反叛与暗中牵线；两者是相同背景下的不同具体场景，保留 partial，不能升级为逐章平行映射。最终使用完整228章和中文末章进行核验。

| 场景 | 完整输入 | 原始输出 | 锚点邻段与候选正文 | 审计 |
| --- | --- | --- | --- | --- |
| 1–3 | [messages](../artifacts/translation/continuation-validation/run1/prefix-03/final-input.md) | [response](../artifacts/translation/continuation-validation/run1/prefix-03/final-output.txt) | [evidence](../artifacts/translation/continuation-validation/run1/prefix-03/evidence-context.json) | [audit](../artifacts/translation/continuation-validation/run1/prefix-03/audit.json) |
| 7–9 | [messages](../artifacts/translation/continuation-validation/run1/window-09/final-input.md) | [response](../artifacts/translation/continuation-validation/run1/window-09/final-output.txt) | [evidence](../artifacts/translation/continuation-validation/run1/window-09/evidence-context.json) | [audit](../artifacts/translation/continuation-validation/run1/window-09/audit.json) |

每次摘要、召回、查证、重试与失败的精确输入输出保存在结果 `artifact_dir/calls`。`semantic_status=model_suggested_unreviewed`，引文能绑定只代表确实存在，不代表模型推理必然正确。模型自报 `threads_checked=yes` 或 `state_compatible=yes` 也不是工程确认；本次不足两条独立 shared/current 锚点，故不能自动采用。

作用域审计：两组均无 pending 请求；保存的请求中未出现范围外中文 chapter ID，中文摘要也未引用其他中文 edition。这是输入隔离检查，不是逐字语义泄漏检测或模型预训练记忆检测。`search_complete=false`，未穷尽全书语义。

## 3. 真实运行暴露并修复的问题

1. **终点误识别为后记。** 增加终点正文过滤；仍保留叙事性的终章、尾声、幕间。规则仅覆盖明确的非正文标题，不声称通用理解全部附录。
2. **预算估计超限。** 原始核验逐段重复携带来源字段导致输入膨胀，改为按章组织 title/chapter_id，段落只放 ref/text；必要时减少事件邻段或一次只全文核对一个候选。
3. **输出被推理预算耗尽。** 在当前131072输入上下文、16384输出上限、低推理配置下，完整核验仍多次耗尽输出，window 的六次失败都没有可见正文；这不是 JSON 括号或宽字符解析问题。
4. **截断后的局部恢复。** 有可见残文时保留候选与引用，用真实正文补全；完全没有可见输出时缩为一个候选、中文末章、相关导航概要和少量引文，允许否定候选并 REQUEST 邻章。每次仍受原持久预算与最多三次尝试约束。局部恢复不能提升为自动采用。
5. **缓存与恢复。** prompt JSON 使用稳定键序；响应、失败、pending 都进入同一持久账本；相同请求恢复复用，未知远端状态不重发。读取完整中文末章后会保留已读范围，后续 prompt 因证据扩展发生变化时产生新的请求身份。
6. **Windows 长路径。** 原子写入临时文件改用短前缀，避免再次复制完整 hash 文件名而超长。
7. **预算耗尽丢失前轮结论。** 保留上一轮已核验锚点与待查请求，最终状态仍为 insufficient_evidence，不把候选冒充推荐。

一次局部恢复的模型输出错误声称“中文末章仅见片段”，但当次实际已提供全部64段。此类对上下文的错误自述未被当作事实；最终扩大正文核验后仍推荐228，但这并未解决多线覆盖与版本重排问题。

## 4. 成本与复现

使用用户本地聚合网关的 deepseek-flash，容量由私有配置声明为131072/16384。实验仅在内存设 `trust_env=False`、`reasoning_effort=low`，未修改用户凭据文件。表中是最终定位目录内所有已付费响应和失败的累计用量，包含调试与证据扩展，并非理想首次调用成本。未把更早“误选后记”诊断目录混入表中。

| 场景 | 成功响应 | 截断失败 | prompt tokens | completion tokens | 其中 reasoning | total |
| --- | --- | --- | --- | --- | --- | --- |
| 1–3 | 6 | 2 | 192407 | 81050 | 70287 | 273457 |
| 7–9 | 9 | 6 | 361493 | 185481 | 170791 | 546974 |

请求数分别为8与15。缓存输入仍计入服务端 prompt 用量；上表未按价格折算。回退成功说明工程可以恢复，不说明当前模型的定位成本已优化。

```console
python -m scripts.verify_continuation --locate
python -m scripts.report_continuation
python -m pytest tests/translation -q
```

不带 `--locate` 只做本地整理和定位规划。脚本只输出模型名、容量、任务名与 usage，不显示凭据。

最终对7–9场景重开同一计划，返回相同228候选，`new_model_requests=0`；未再次付费发送已完成的查证。

## 5. 已交付与下一步

公共 API：`plan_reference_set`、`ReferenceSetPlan.to_input_spec`、`Index.plan_continuation`、`Index.locate_continuation`，见[库用法](translation-library-usage.md)。wheel 可独立安装，隔离解释器 smoke 已验证7–9排序、终点为9和定位 API 可调用，本地规划零模型请求。

最终全量 `tests/translation`：583 passed（一个既有 pkg_resources 弃用警告）；新增续译测试17项，覆盖排序排列不变、局部卷、冲突与重复、引用绑定、缓存恢复、截断局部补全、虚构引文拒绝、预算中断保留已有证据。`compileall` 与 `git diff --check` 通过。安装后的验证使用 `python -I`，确认导入路径来自wheel安装目录。

后续语义改进应优先针对少量末端叙事线分别查证：每条保留最后可靠共同事件、Web 后续未覆盖事件、版本差异和可能重复范围，再汇总一个连续起点或返回歧义。目录无法召回某条线时显式报告缺口，不能因反叛线已定位就宣称主角线、鬼线也已确认。无需先构建全书实体图，也不应硬编码本书章号补足模型判断。
