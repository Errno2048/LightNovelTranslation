# 01 配置与容量预算

[返回目录](index.md) · 下一步：[输入标准化](02-ingestion.md)

> 新工作流（main）使用 `[workflow]` 配置批次并发、上下文窗口和输出预算；本页的 `[roles.*]` / `[sequence]` 说明属于 legacy 引擎，仅供参考或在 `legacy-sequence` 分支使用。

## 配置如何加载

`TranslationProject(workspace)` 使用默认私有配置；默认文件不存在时允许本地导入、检查与规划，模型操作才要求配置就绪。`TranslationProject(workspace, config="path.toml")` 使用指定 TOML/JSON；显式 `config=TranslationConfig()` 跳过用户文件，适合离线操作。公共入口只加载指定格式的配置，不执行 Python 配置文件。

默认目录按“显式 directory 参数 → `LIGHTNOVEL_TRANSLATION_HOME` → `~/.lightnovel_translation`”解析。配置文件名为 `config.toml`。执行 `python -m lightnovel_translation config init` 创建[空模板](../../assets/config.example.toml)，由用户本地填写；agent 用 `config check` 检查就绪状态，不读取含凭据的文件。检查不会访问远端，也不验证模型实际容量。

顶层支持 `schema_version`、`tokenizer_path` 和下面各配置节。当前 schema 为 `1`；未知配置键报错，不会静默忽略。TOML 中不需要的可选字段应省略，不填写 `null`。指定 `tokenizer_path` 时相对配置文件目录解析，需安装 `narrative` 可选依赖；未指定时使用估算计数器。

## `[model]`：连接与模型身份

| 字段 | 作用与默认行为 |
|---|---|
| `model` | 网关模型名；不会自动探测其能力 |
| `base_url` | 兼容接口前缀，Provider 在其后接 `/chat/completions`；禁止 URL 用户密码、query、fragment |
| `api_key` | 只在进程内用于认证；非空时优先使用 |
| `api_key_env` | key 为空时读取该环境变量，默认 `LIGHTNOVEL_TRANSLATION_API_KEY` |
| `timeout` | HTTP 超时秒数；类默认 `240`，模板 `600` |
| `retries` | 公共入口必须为 `0`；持久化引擎负责有账本的重试 |
| `thinking` | 默认 `"omit"` 不发送专用思考字段；布尔值发送 enabled/disabled |
| `reasoning_effort` | 可选，支持 `low/medium/high/max/xhigh/minimal/none`；端点是否接受由上游决定 |
| `trust_env` | 默认 `true`，使用环境代理；设 `false` 可直连，仍验证 TLS |
| `dimension` | 可选 embedding 维数；公共翻译主线不要求配置 embedding |

角色共用该生成模型；`[roles.*]` 配置的是预算，不是分别指定不同模型。更换模型或 Provider 身份后创建新任务；原任务的缓存与冻结上下文不能自动换用新身份。

## `[capacity]` 与 `[roles.*]`：单次请求预算

| 字段 | 默认值 | 含义 |
|---|---:|---|
| `context_window_tokens` | 131072 | 输入、输出预留和安全空间的总窗口 |
| `max_output_tokens` | 16384 | 单次允许的最大输出预留 |
| `max_input_tokens` | 省略 | 端点另有输入上限时设置 |
| `token_inflation_factor` | 1.05 | token 估算的先验膨胀系数，至少为 1 |
| `safety_tokens` | 2048 | 协议差异等安全空间，可为 0 |

每个角色支持 `requested_output_tokens` 和 `reasoning_reserve_tokens`。思考预留已经包含在 requested 内，不额外叠加，也不是对上游思考长度的硬控制。

| 角色 | 用途 | 模板输出预留 |
|---|---|---:|
| `translator` | 生成初译 | 8192 |
| `reviewer` | 语义审校 | 8192 |
| `adjudicator` | 裁决候选问题 | 8192 |
| `indexer` | 摘要、实体等抽取 | 16384 |
| `compressor` | 压缩导航摘要 | 8192 |
| `mapping` | 旧式输出的段落映射 | 未指定时继承 reviewer |
| `revision` | 根据裁决修订译文 | 未指定时继承 translator |
| `repair` | 有界格式修复 | 未指定时继承 reviewer |

注意模板与空 `TranslationConfig()` 的默认值不同：未配置角色时，引擎主角色默认 16384、indexer 32768，公共配置先按 capacity 的输出上限裁剪。因此默认容量下均不超过 16384；显式角色预算超出容量会报错。模板还开启 `translation_tools=true`，空配置默认关闭；公共新任务默认使用 `source-records`。

最终请求预算按实际消息计算：

```text
safe_input = ceil(estimated_final_messages × max(先验膨胀系数, 已校准系数))
input_limit = min(context_window - requested_output - safety, max_input 或 context_window)
满足 safe_input <= input_limit 且 requested_output <= max_output 才能发送
```

估算还包含消息元信息及存在时的工具 schema。usage 可用时以实际输入/估算值并留余量更新系数，只增不减。服务端缓存命中的 tokens 仍占上下文，不能从容量中扣除。分块规划是预估，真正发送前仍检查完整消息。

## `[index]`：已有版本的独立分块

| 字段 | 默认值 | 作用 |
|---|---:|---|
| `parent_max_chapters` | 100 | L1 最多包含的章节数 |
| `parent_min_chapters` | 30 | 完整 L1 的硬下限；不足下限的短书、卷尾或增量尾部可显式标记例外 |
| `expected_l2_count` | 10 | 每个 L1 预期划分的 L2 数量 |
| `l2_prepartition_min_chapters` | 3 | 预划分 L1 时估算的每个 L2 最低章数；正式 L2 允许更短 |
| `parent_build_mode` | `direct` | 默认严格一次摘要；可显式选择 `child_summary_rollup` 或 `direct_then_rollup` |
| `child_max_chapters` | 10 | L2 最多包含的章节数 |
| `child_target_tokens` | 64000 | L2 内容打包目标，受实际可用容量约束 |
| `child_min_content_tokens` | 4000 | 尽量避免过小 L2；不能突破章界、卷界与硬容量 |
| `entity_catalog_threshold` / `core_entity_limit` | 50 / 50 | 实体目录超过阈值时生成有上限的核心实体视图 |
| `predecessor_reserve_tokens` | 4096 | 为前驱导航预留输入 |
| `protocol_reserve_tokens` | 4096 | 为提示协议预留输入 |

这些字段属于 `EditionIndexSpec`，也为新连续任务提供统一 token 分块与实体目录默认值。旧任务读取冻结的 planner 版本与参数，不因升级而重排。

## `[sequence]`：连续运行与增量索引

下表列出可配置项；更改它们不会改写已冻结任务。

| 字段 | 公共新任务默认 | 作用 |
|---|---|---|
| `strategy` | `"H"` | R 滚动、U 保留增长、H 分层压缩；见 [13](13-compaction.md) |
| `mode` | `"working"` | 工作稿提交；也可 `accepted_only` |
| `n_chapter` / `n_child` | 3 / 2 | 检查点保留的邻近完整双语章/详细 L2 数 |
| `child_max_chapters` / `parent_max_chapters` | 继承 index | 任务内增量节点上限，可显式覆盖 |
| `child_min_content_tokens` | 继承 index | 触发避免过小子块的 rollup 策略 |
| `parent_build_mode` | 继承 index，默认 `direct` | L1 容量失败策略；严格默认会阻断并报告诊断，rollup 必须显式选择 |
| `entity_catalog_threshold` / `core_entity_limit` | 继承 index，默认 50 / 50 | 完整合并目录阈值与常驻核心实体上限 |
| `planning_utilization` | 0.80 | 增量分块使用的规划容量比例 |
| `parent_summary_input_tokens_per_child` | 8192 | L1 用子摘要 rollup 时的保守输入估算 |
| `compression_trigger` / `compression_target` | 0.85 / 0.65 | H 压缩触发比例与目标比例 |
| `soft_input_tokens` | 省略 | 可选输入软预算，最终仍受 capacity 限制 |
| `max_revisions` | 1 | 初始修订上限，允许 0–2；追加恢复另有接口 |
| `review_profile` | `"chapter-review"` | 可选 v5/v6 审校协议 |
| `reference_profile` | `"stable"` | 稳定章/段别名；保留 legacy 兼容 |
| `mapping_profile` | `"coarse"` | plain 输出映射协议，也支持 coarse |
| `translation_tools` | false，模板 true | 翻译阶段是否允许只读查证 |
| `targeted_evidence` / `targeted_evidence_tokens` | true / 12288 | 定向术语及局部证据开关与上限 |
| `workset_max_tokens` / `workset_recent_chapters` | 128000 / 25 | 压缩 workset 预算与近期窗口 |
| `thread_reconcile` | false，模板 true | 封口 L1 时批量判定未决线程 |
| `window_l2_blocks` / `translation_concurrency` | 3 / 3 | 窗口翻译的 L2 块数与并行翻译数 |
| `review_concurrency` | 4 | 并行审校数；建议低于翻译并发 |
| `provisional_lookahead` | 1 | 允许未审校窗口领先已审校窗口的数量；0 禁用推测 |
| `output_profile` | `"source-records"` | 逐源段输出；plain 为兼容路径 |

`source-records` 要求 `stable`。capacity、roles、累计请求/token 限额不能重复放进 sequence，分别在所属节配置。

## `[syosetu]`：原文下载

原文下载另用 `[syosetu]`：`concurrency=2`（1–8）和 `request_interval=1.0`（秒，所有下载线程共享）。两项不影响模型调用。`SyosetuConfig.load()` 只解释此节，不要求模型就绪；`download_work` 的显式参数优先。串行设置 `concurrency=1`，详细退避与恢复见[原作采集](00-source-acquisition.md)及 `syosetu-fetch` 技能的采集 API 说明。

## `[references]` 与 `[limits]`

| references 字段 | 默认值 | 作用 |
|---|---|---|
| `packet_token_budget` | 自动 | 按容量、膨胀系数及最大角色输出推导的参考包预算 |
| `style_max_documents` | 3 | 中文文风样本最多文档数 |
| `style_excerpt_paragraphs` | 48 | 完整样本放不下时的局部正文窗口参数 |
| `terminology_max_terms` | 16 | 每次补查的候选术语上限 |
| `terminology_input_tokens` | 自动 | 默认约可用输入的四分之一，并受容量限制 |
| `terminology_output_tokens` | 自动 | 默认 indexer 输出预留，并受单次输出上限限制 |
| `terminology_max_documents` | 12 | 补查最多读取的参考文档数 |
| `terminology_radius` | 2 | 参考命中周围的段落半径，可为 0 |
| `discover_terms` | true | 是否从原文启发式发现片假名、技能等候选 |

`limits` 是耐久的累计上限：`max_requests`、`max_input_tokens`、`max_output_tokens` 默认均为 `0`（不设累计上限）；`max_attempts=3`（允许 1–3）。每个索引和每个任务分别记账，不是整个项目共享一份总额度。`capacity.max_output_tokens` 是单次容量，`limits.max_output_tokens` 是累计预算；追加修订次数也不会重置累计账本。累计值设为 0 不会关闭统计。

## 查证与代码位置

预算不够先查 `job.plan_next()["context"]["budget"]`、参考 omissions 和分块计划。不要把所有数字一起调大；判断不足来自输入、输出、累计成本还是修订次数。`config.public()` 可用于脱敏诊断，不是恢复凭据的备份。

- `src/lightnovel_translation/config.py`：`TranslationConfig`、`ReferenceConfig`、`Limits`，字段校验、继承、脱敏与自动参考预算。
- `src/lightnovel_translation/user_config.py`：`user_config_path`、`init_user_config`、`user_config_status`，私有文件管理。
- `src/translation/config.py`：`ModelConfig`，传输字段与公开身份；其旧 `load_settings` 不属于公共配置路径。
- `src/translation/budget.py`：`ModelCapacity`、`RoleBudget`、`plan_request`、`observe_usage`。
- `src/translation/sequence/schemas.py`：`SequenceSpec`，连续任务字段校验与旧任务兼容。
- 回归入口：`tests/translation/test_library_user_config.py`、`test_library_api.py`、`test_reference_workflows.py`。
