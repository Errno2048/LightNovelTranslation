# P4 实施结果：证据准入、冻结知识与 B/C 开发试验

日期：2026-10-15。P4 的工程闭环和三章开发试验已完成，尚未证明 C 稳定提高翻译质量；C 保持显式选择，不默认启用。P5–P7 不在本次实现范围内。

依据：[P4 执行计划](translation-p4-plan.md)、[知识库设计](translation-knowledge-design.md)。

## 1. 实现范围

| 任务 | 本次结果 | 实现位置 |
| --- | --- | --- |
| T0 输入与契约 | 固定语料内容、声明指纹、切分、donor allowlist、审查范围及辅助文件哈希；故事范围、揭示位置与修订独立保存 | `translation/knowledge/{schemas,evidence}.py` |
| T1 证据准入 | 默认 donor 是 candidate；只有明确的版本、段落和用途审查才能授权抽取或 few-shot；保留原始对齐候选 | `translation/knowledge/bootstrap.py`、`alignment/paragraphs/reviews/` |
| T2 存储与修订 | SQLite 提交链、证据校验、依赖闭包、折内提交政策、幂等键、head 检查、冻结、fork、修订、撤销、影响定位及确定性导出 | `translation/knowledge/service.py` |
| T3 导航 | 无模型的日文标题/正文索引；短名和子串查询；先按政策过滤，再确定性排序、top-k；命中段落与邻章定位 | `KnowledgeView.search/read_evidence` |
| T4 有限抽取 | 显式计划、逐请求及累计预算、候选本地校验、审查决定、来源覆盖记录、模型配置缓存、失败保留和恢复 | `translation/knowledge/extraction.py` |
| T5 上下文 | 独立折内 K0；相关约定、简单名称卡、B 的同一组 few-shot；完整 prompt 预算、证据依赖和指纹 | `translation/context/compiler.py` |
| T6 集成 | 知识库 CLI、C、dry-run、只读查询无模型调用、mock 闭环、成功结果恢复 | `translation/cli.py`、`translation/workflow/` |
| T7 验证 | 离线回归、真实抽取、三章各 B/C 一次、证据复验、用量、匿名审查页及逐项质量记录 | `knowledge/p4/verification.json`、各运行的 `reports/` |

实现集中在 `service.py` 等少数文件，未照规划中的建议文件名机械拆分模块。没有实现通用 GraphRAG、全书摘要、事件状态重建、人物知情推理、自动接受生成译文或连续翻译压缩。

## 2. 证据资格与局部对齐复核

正式语料仍为 `artifacts/translation/tenken-cache-clean/corpus/`。

```text
文件内容指纹：488979538255b4eb91b52ce0005938738e431bb4e5e03980411de1362af345e9
corpus 声明：6d1131ada474eb42ffb9d9a426b66ff40d2e66734a77e0a51e7670f8d7c24713
```

前者由三个 corpus 文件的实际哈希生成，后者是已有构建流程声明的语料标识。它们不是同一种计算方法；本轮分别保留并校验。P4 没有修改 corpus 或 EPUB。

`input_manifest.json` 固定核心文件和切分身份，`input_dependencies.json` 补充质量阻断、结构审查、donor 和局部审查等文件的哈希。

修复了一个危险的初版行为：原 bootstrap 会直接把邻章前两个正文段落标记为 `manual_prefix_review/accepted`。现在未提供显式审查时只输出 candidate；接受必须绑定确切原文/译文 revision、paragraph IDs、方法、理由和用途。候选不能进入已接受知识或 B 示例。

审查用途分别保存 `correspondence/convention/few_shot/evaluation`，不以一个 reviewed 标记批准全部用途。accepted 子范围可以单独提交；partial、rejected、unresolved 不授权使用。

三个目标的 donor 如下，均为非保留集、无质量阻断的局部范围。

| 目标 | donor 章节 | 范围与限制 |
| --- | --- | --- |
| main:3 | main:2、main:4 | 第 2 章念动相关三段；第 4 章搬运哥布林尸体的两段 |
| main:775 | main:774、main:776 | 矮人行军两段；两国军队和抗魔出现的两段 |
| main:1233 | main:1232、main:1234 | 战斗结束三段；村口追问两段。简化的魔术/毒名不作为规范 |

六条正式范围见 `knowledge/p4/donor_reviews.json`；准确段落 ID、revision、原始双语证据和资格见 `evidence_catalog.jsonl`。字段名 `reviewed_prefix` 为兼容既有 few-shot 入口保留，资格实际绑定显式范围，并不要求一定从章节首段开始。

另外记录了三个目标共 19 个局部范围的审查，包括普通样本、拆并、低相似度和数字提示：

- main:3 的原文 p80 关于同族个体、武器和擅长事项差异，在对应中文 p46 中没有表达；该 2:1 候选不能认证为完整对应，不供知识或示例使用。
- main:775 的 1:2 范围结构可以对应，但中文把关于新兵意愿的推测写成“我想”，没有批准其为语义金标准。
- `2万/两万`、`2日目/第二天` 增加保守数字等价诊断。数字无警告仍不代表翻译准确：部分相关范围另有主语或语义问题。
- main:1233 末段即使向量相似度低，局部语义仍可对应；另一些范围存在主语误解，分别记录。
- 第 128 章质量阻断、已分离附录/重复正文和数值冲突范围继续排除。

局部审查由 Codex 根据双语文本完成，没有独立人工金标准。全部 242 个旧候选未被批量升级；其余范围仍待审查。详见 `alignment/paragraphs/reviews/p4-local-review.json`。

## 3. 存储、查询和 prompt 边界

原始 corpus 保持唯一事实来源。SQLite 保存版本化知识、提交历史、证据记录和可重建索引，JSON 是快照导出物。索引默认只有日文，不将中文隐藏标题或正文加入翻译检索。

FTS5 索引结构已落地，但 P4 的默认查询使用可见文档上的确定性子串匹配和单文档排序，以兼容未分词日文和短姓名，并避免全库 BM25 统计泄漏。没有生成全书 embedding。

主要 Python API：

| API | 行为 |
| --- | --- |
| `KnowledgeBase.build_index()` | 构建日文标题/正文索引，0 模型请求 |
| `open_view(snapshot_id=..., visibility_policy=..., target_position=...)` | 固定知识版本、可见范围和适用章节 |
| `view.search/read_evidence` | 返回命中理由、段落引用、邻近章节；实际取证校验 revision 和质量限制 |
| `view.get_conventions/resolve_mentions/explain` | 匹配约定和已见名称，保留冲突、条件不明和同名候选，回查证据 |
| `bind_policy/commit/review_candidates` | 限制折内提交、验证证据及依赖闭包；接受须有具体审查 |
| `freeze/export_snapshot/fork` | 不可变快照、确定性 JSON 和工作副本 |
| `revise/retract/impact` | 保留旧快照；定位依赖条目与编译上下文；依赖修订后的旧派生项不再用于新视图 |
| `plan_enrichment/run_enrichment` | 显式有界抽取及缓存，普通查询不触发它们 |
| `compile_context` | 选择相关知识，保留证据和未决项，生成 C 的可复现 bundle |

`story_scope` 只记录明确区间、相对约束或 unknown，不推断完整故事时间。说话人/对象条件不明时返回 uncertain；冲突约定不进入强制规则。人物卡只保存已见名称、别名和证据，不自动合并身份或为无标记对白指定人物。

每折使用独立数据库、冻结快照和 policy。目标日文可读；目标中文及其派生知识被遮蔽。知识提交校验完整证据和传递来源；读取时再次过滤。B/C few-shot 完全相同，C 只添加冻结约定与名称卡，没有 P5 修订和 P6 状态分析。

预算覆盖最终发送的整个 prompt。无供应商 tokenizer 时使用 UTF-8 字节数加消息开销的保守估算，并注明不是实际 token 数。先省略可选知识；若完整原文、同一组 few-shot 和必要约定仍装不下则显式失败，不截断原文，也不悄悄改变 B/C 对照条件。

## 4. 实际构建与调用

| 折 | 接受约定记录 | 简单名称卡 | 拒绝记录 | C 入选知识记录 | 完整 prompt 保守估算 |
| --- | ---: | ---: | ---: | ---: | ---: |
| main:3 | 6 | 0 | 1 | 5 | 16083 |
| main:775 | 7 | 0 | 1 | 4 | 11199 |
| main:1233 | 5 | 3 | 5 | 8 | 12840 |

合计 18 条接受约定记录、3 张名称卡和 7 条拒绝记录；其中包含同一译法由不同证据支持的独立记录，不等于 18 个不同术语。三个 K0 均已冻结并 fork 出 `translation-copy`，生成译文未被写回知识。

真实抽取共 6 次请求，返回 25 条约定候选。两份响应被外层 Markdown JSON fence 包裹，先按严格 JSON 解析记为格式失败；保留原响应后，仅移除完整外层 fence 并重做本地校验，新增请求为 0。原始失败文件和修复标记均保留。伪造证据、非法引文、语义不确定项不能通过此修复变成接受知识。

使用本地配置的 `glm-5.3-flash`，独立实验配置设置 `retries=0`，防止实际请求数超过首轮计划。B/C 各整章生成一次，共 6 次，全部正常结束，无截断或网络重试。供应商实际用量如下：

| 工作 | 请求 | 输入 tokens | 输出 tokens | 总 tokens |
| --- | ---: | ---: | ---: | ---: |
| 抽取 | 6 | 2181 | 1265 | 3446 |
| 翻译 B | 3 | 8034 | 4645 | 12679 |
| 翻译 C | 3 | 10417 | 4732 | 15149 |
| 合计 | 12 | 20632 | 10642 | 31274 |

新增 embedding 请求为 0。六次翻译的事前保守输入加输出预留为 121845，硬上限 160000；实际翻译用量 27828。价格未核实，费用为 unknown，不以推测的单价换算金额。供应商返回的缓存命中 tokens 均为 0，本轮不声称实现或测得 KV cache 收益。

## 5. 质量观察与结论

| 目标 | B 总 tokens | C 总 tokens | B char-F2 | C char-F2 |
| --- | ---: | ---: | ---: | ---: |
| main:3 | 5896 | 6548 | 32.310 | 32.752 |
| main:775 | 3434 | 4070 | 37.870 | 36.631 |
| main:1233 | 3349 | 4531 | 30.370 | 31.972 |

char-F2 只是与现有参考的字符重叠，不是质量判定。三个参考仍标记为 `structurally_verified_semantically_suspect`，不能因生成没有复现参考错误而扣分。

按已接受且当前命中的 9 组折内术语形式，原文有 89 个字面出现机会（38/16/35）。两组未观察到已确认的对应译名误用，但这些词 B 原本已经表现良好，不能据此证明 C 改善。输出词数可能因正常改述而不同，程序没有把字面计数冒充段落级准确率。

已保存的原文依据和输出片段显示：

- main:3：B 把哥布林腹部误写成“我腹部”；C 出现“我 exploded 的撞击”英文残片。两组都保留了现有中文漏掉的同族差异段，不因这一点惩罚生成。
- main:775：B 混入 `apparently`；C 使用“弗兰”，而现有译文是“芙兰”。本折 donor 和 K0 未覆盖 `フラン`，因此这是覆盖不足，不是违背已注入词条。不能用另一个折从 main:1232 获得的译名偷补本折实验。
- main:1233：两组均保持“芙兰/艾瓦斯”；B 残留“魔術師”，C 把反复瞥看写成“贼眉鼠眼地盯着”，增加了原文未明确表达的贬义。`虚言の理` 等未覆盖术语和局部不能确定的性别另记 unresolved。
- 标点、留白和未覆盖人名仍有差异；少量术语知识不能代替语言形式检查、指代分析和后续审校。

审查为 Codex 按原文的开发复核，提供匿名页面供后续人工审查，但没有独立人工或统计显著性结论。逐项记录见 `quality_review.json` 和每个运行的 `reports/error_annotations.jsonl`。选定缺陷有原文 paragraph ID 和生成片段；没有声称测得全章漏译率或长程事实召回率。

因此本次结论是：工程闭环通过，小样本试验完成，**质量收益未证实，C 不默认启用**。C 的总 tokens 比 B 增加约 19.5%，当前覆盖和提示尚不足以支持这部分额外开销必然换来收益。

这也给出了懒创建的实际边界：只读邻近两个短范围，可能缺少本章关键人物译名。后续应先用目标原文列出未覆盖名称/术语，再在允许的日文索引中定点寻找既有译文证据，经过同样的范围审查后有限补充；不需要立即全书语义抽取，也不能用空的 unresolved 列表声称整章知识完整。

## 6. 验证与复现

回归命令：

```powershell
$env:PYTHONIOENCODING='utf-8'
py -3.11 -m pytest tests/translation -q --junitxml=artifacts/translation/tenken-cache-clean/verification/p4-pytest.xml
```

最终回归为 **90 passed、10 warnings**，XML 见 `verification/p4-pytest.xml`。已有 `pkg_resources` 弃用警告不属于本轮新增故障。

真实产物复验：

```powershell
py -3.11 artifacts/translation/tenken-cache-clean/verification/p4_validate.py
```

检查结果：21 条接受记录的 42 份引用证据有效；17 条注入记录的 34 份证据有效；隐藏/保留来源违规为 0；三折 bundle 完全重现；B/C few-shot 相同；源文完整；六份成功结果恢复新增请求为 0。根知识库索引含 1359 个日文章节；“已索引”不代表“已抽取”或“该折可以读取所有章节”。

辅助脚本：

- `verification/p4_setup.py`：首次固定 donor 和 mock 闭环；已完成产物再次执行只读核验，固定原抽取计划，不改写历史审查，0 真实请求。
- `verification/p4_range_review.py`：记录已逐项指定的局部审查，不改变旧候选。
- `verification/p4_prepare_live.py`：应用显式候选审查、冻结和 dry-run，0 真实请求。
- `verification/p4_quality_review.py`：重建已复核质量注释与辅助文件哈希，0 真实请求。

这些脚本绑定本次实际语料指纹，不能直接给新语料重贴旧审查结论。真实抽取与翻译仍通过显式 CLI 运行。

一般使用入口如下；路径按目标折替换。`prepare --knowledge-db` 的 single_gap 模式只允许一个目标，避免误用一个可变数据库做多折实验。

```powershell
py -3.11 -m translation --book tenken-cache-clean knowledge-bootstrap --help
py -3.11 -m translation --book tenken-cache-clean knowledge-plan --help
py -3.11 -m translation --book tenken-cache-clean knowledge-extract --help
py -3.11 -m translation --book tenken-cache-clean knowledge-review --help
py -3.11 -m translation --book tenken-cache-clean knowledge-freeze --help
py -3.11 -m translation --book tenken-cache-clean knowledge-search --query フラン
py -3.11 -m translation --book tenken-cache-clean context-build --help
```

复验真实翻译计划时，全局 `--config` 放在子命令之前：

```powershell
py -3.11 -m translation --book tenken-cache-clean --config artifacts/translation/tenken-cache-clean/knowledge/p4/pilot.toml run --experiment p4-live-main-3 --run p4-live-main-3 --variants B C --dry-run --max-tasks 2 --max-output-tokens 8192 --max-input-estimate 30000 --max-total-tokens 60000
```

产物总入口为 `knowledge/p4/live-plan.json`、`verification.json` 和 `quality_review.json`。各折在 `knowledge/p4/folds/main-{3,775,1233}/`，译文、请求、用量事件及匿名对照页在 `runs/p4-live-main-{3,775,1233}/`。

交给 P5 的是冻结约定、bundle 和本轮错误清单。后续重点是检查未覆盖关键译名、无依据增义、残留外语和指代问题，并在既定小样本协议上验证修订；P6/P7 的状态推理、连续提交和压缩仍保持原有阶段边界。
