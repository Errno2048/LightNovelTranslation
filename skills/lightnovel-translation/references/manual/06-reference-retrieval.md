# 06 跨版本关联与参考检索

[返回目录](index.md) · 上一步：[独立索引](05-edition-index.md) · 下一步：[续译定位](07-continuation.md)

## 四件不同的工作

跨版本模式把“这段剧情是否相关”“这两个名称是否同一实体”“中文正文里有没有可用译法”“最终 prompt 放得下哪些材料”分别处理。关联成功不代表所有名字已匹配；中文文件已导入也不代表正文实际进入翻译请求。

| 入口 | 读取什么 | 产出 | 是否可能调用模型 |
|---|---|---|---|
| `index.associate` | 显式选择的日中 L2 摘要、标题与阅读范围 | 区块关联及证据 | 是 |
| `index.match_entities` | 源 L1 与参考 L1 的已建子块实体目录 | 译名候选、冲突和覆盖 | 是 |
| `index.resolve_terminology` | 当前日文、已有目录、中文原始正文局部 | candidate/ambiguous/no_match | 是 |
| 参考包及文风检索 | 已冻结候选、中文正文与 token 预算 | 被选正文、术语、omissions | 通常本地；启用术语补查时另有请求 |

## L2 关联与 L1 实体匹配

`associate(source_nodes, reference_nodes)` 只搜索调用方提供的候选集合，允许多对多、非单调、related_context 或无对应。LLM 利用摘要中的事件与标题/阅读顺序判断关系；标题是辅助导航，不是严格匹配证明。`search_complete=false` 保留未穷尽搜索的边界。

`match_entities(source_parent, reference_parents, batch_size=...)` 把源目录分批，每批对照参考父节点下**已建子节点**的实体目录。它可以跨 L2 修补局部匹配缺口，但不能发现从未抽取的实体；`matching_coverage` 中未报告、未判定和缺目录不能当成 unmatched。

`prepare_references(source_chapters, reference_chapters)` 封装“选各自 L2 → 构建这些 L2 → 关联 → 相关父目录实体匹配”。它不自动搜索全书，也不自动补齐全部 L1 子节点。选错中文窗口时，应扩大有依据的候选范围，而不是反复对同一批摘要重试。

## 术语补查为什么不依赖完整索引

补查可以直接读取所提供中文译本的原始正文。候选来自当前原文、已有实体、过去源摘要、显式 `source_forms`，以及片假名、Latin、【技能名】等启发式发现。人物名优先，并按频次等因素限定 term/doc/token 数，避免把全部技能列表塞入每个请求。

每个 candidate 必须有日中两侧逐字出现的名称及引用；description 使用当前源局部证据。没有结果可能因为未抽取、候选未选中、中文正文未召回、预算裁剪或模型判断无对应，不能一律归因于 embedding。`no_match` 也不证明全书不存在译法。

可用 `index.resolve_terminology(chapter, source_forms=[...])` 显式补查，或在 `create_job(resolve_terminology=True)` 创建时补查并冻结。恢复旧任务不会重新补查；需要新的术语或正文证据时创建新任务。

## 中文正文的召回与裁剪

文风样本依次考虑：显式 `style_reference_chapters`、关联 L2 下的正文、表记线索召回、所提供中文译本的后备样本。后备路径允许没有剧情关联时仍有中文文风参考，不需要伪造严格映射。

优先尝试完整正文；放不下时改用局部段落窗口，再不足则删除并写 omissions。参考包内还含过去 Web 原文、过去已建导航、候选译名及其用途标签。`references.style_*` 和 terminology 参数控制上限，详见 [配置](01-configuration.md)。

中文材料标记为 `reference_assisted` 等参考用途，可能含另一版本的未来剧情；模型被要求仅用于文风/有证据的译法。桥接到连续任务时还会剔除目标未来 L2 解释及 L1 范围过宽的未来描述，只保留当前/过去源用例。不能把文库实名解释覆盖 Web 尚未揭晓的昵称。

## 两次容量选择，两个报告层级

1. 创建任务时，ReferencePacket 根据参考预算选入候选正文和术语，并保存省略原因。
2. 翻译前，将参考包和主上下文合并，按实际预算及校准系数再检查；必要时先移除文风样本，再移除候选术语。当前目标原文不能被静默截断。

用 `job.reference_report()` 查看冻结计划，`job.plan_next()["context"]` 中的 `edition_reference_selection` 查看最终规划。`actual_style_document_ids` 来自已经保存的翻译上下文；尚未保存上下文时为 null。上下文可能先于 HTTP 请求保存，因此该字段非空本身不证明已经发送；分析中文 few-shot 是否有效，应继续用 explain 中的实际请求确认，不能只看导入记录或初始包。

排错时按“名称是否在源文本 → 是否进入候选 → 是否命中中文证据 → 是否被裁剪 → 模型是否采用”逐层查。局部原文证据和主动查询机制见 [09](09-context-evidence.md)。

## 代码位置

- `src/lightnovel_translation/index.py`：`prepare_references`、`associate`、`match_entities`、`resolve_terminology`。
- `src/translation/editions/service.py`：`EditionPipeline.match_blocks`、`match_entities`，关联与目录匹配。
- `src/translation/editions/terminology.py`：`resolve_target_terminology`，定向正文补查。
- `src/translation/editions/retrieval.py`：候选检索、独立文风样本选择。
- `src/translation/editions/context.py`：ReferencePacket、用途标签与预算裁剪。
- `src/translation/editions/sequence.py`：`build_sequence_inputs`，去除未来解释并桥接到连续任务。
- `src/lightnovel_translation/job.py`：`TranslationJob.reference_report`，planned/actual 对照。
- 回归入口：`tests/translation/test_reference_workflows.py`。
