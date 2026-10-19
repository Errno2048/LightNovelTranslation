# 可安装翻译库：公共接口与实现边界

版本：0.4.0；日期：2026-10-19。

## 1. 目标与分层

将此前依赖仓库路径的实验调用收敛为可安装的 Python 库。常见操作通过少量方法完成；高级能力通过类型化配置或 TOML/JSON 配置；后续 skill 和 agent 只需适配这些方法及其 JSON 结果。

- 安装名：`lightnovel-translation`；公共导入名：`lightnovel_translation`；Python >= 3.11。
- `lightnovel_translation` 提供项目、版本、索引、任务、配置和结果契约。首版公共 API 以本文件和使用说明为准。
- `translation` 保留既有引擎、CLI、实验协议与历史 import；它是兼容实现层，不能把它所有内部字典都视为已稳定的公共数据模型。
- 爬虫、站点凭据、下载书籍、大型实验缓存和脚本不作为运行库依赖。既有 `novel-translate` CLI 继续可用。
- 同步 API 首先形成完整工程闭环，安装包附带可导出的 agent skill。异步服务、任务队列和 GUI 不属于本次交付。

## 2. 公共对象和接口

| 对象 | 接口 | 功能 / 副作用 |
|---|---|---|
| `TranslationConfig` | `from_file(path)` / `from_dict(data)` | 读取配置、验证预算；不执行 Python 凭据文件 |
| TranslationConfig | `from_user_config(directory=None)` / `load_default()` | 复用用户私有配置；默认文件不存在时允许本地操作 |
| 模块函数 | `init_user_config()` / `user_config_status()` / `user_config_path()` | 创建空模板、脱敏检查及定位；不发请求、不覆盖已有文件 |
| 模块函数 | `export_skill(destination)` | 导出随包指南与参考文件；冲突时保留原文件 |
| `TranslationProject` | 构造 / 上下文管理器 | 打开工作区；持有模型客户端与任务资源 |
| Project | `import_book(paths, edition_id=..., language=...)` | EPUB/TXT 标准化，保留来源，不调用模型 |
| Project | `import_edition(spec, base=...)` | 使用完整 InputSpec 的高级导入 |
| Project | `attach_corpus(folder, edition_id=...)` | 关联已有标准化语料，验证并冻结该版本指纹 |
| Project | `edition(id)` / `editions()` | 获取已注册版本 |
| `Edition` | `chapters()` / `read(id)` / `info` | 发现稳定 ID、阅读顺序和正文 |
| Project | `index(editions)` | 本地规划索引，创建本地计划文件 |
| `Index` | `plan()` / `nodes(edition=..., level=...)` | 检查 L1/L2 计划；不调用模型 |
| Index | `read(node_id)` / `entity_catalog(parent)` | 读取已有摘要、实体覆盖；不自动补建 |
| Index | `build(node_ids=None, workers=1, include_parents=False)` | 显式构建 L2，可汇总为 L1；可能调用模型 |
| Index | `build_parent(parent, allow_partial=False)` | 只汇总已有子摘要，不隐式补建缺失 L2 |
| Index | `associate(source_nodes, reference_nodes)` | 显式候选 L2 间的 LLM 事件关联 |
| 本地导入工具 | `plan_reference_set(paths, edition_id=..., overrides=...)` | 按证据整理参考卷、记录缺口，输出可导入清单 |
| Index | `plan_continuation(source=..., references=..., reference_set=...)` | 本地验证参考作用域与末端，冻结定位计划 |
| Index | `locate_continuation(plan, max_new_nodes=..., max_requests=...)` | 有界目录召回、事件抽取、正文核验，保留完整 I/O 与未决状态 |
| Index | `match_entities(source_parent, reference_parents)` | L1 范围内实体候选匹配 |
| Project | `create_job(name, source=..., chapters=..., ...)` | 冻结连续窗口、参考资料、配置和身份；默认不调用模型 |
| Project | `open_job(name)` | 重开冻结任务，不重新构建索引或资料包 |
| Project | `translate(source, name=..., **options)` | create + run + export 的便利调用，返回 RunResult |
| `TranslationJob` | `plan_next()` / `status()` | 查看完整上下文 / 预算 / 执行状态；不调用模型 |
| Job | `run(max_chapters=..., materialize=False, on_event=...)` | 翻译、审校、裁决、修订、提交；逐章前进 |
| Job | `resume(...)` | 同一 durable run 的续跑，复用已完成响应 |
| Job | `checkpoint()` | 对已提交前文做上下文压缩；可能调用模型 |
| Job | `translations()` | 读取已提交译文与质量标签 |
| Job | `explain(chapter, output=...)` | 导出该章完整 prompt、响应、审校与提交记录；无模型调用 |
| Job | `export(output=..., include_io=False)` | 导出 UTF-8 TXT、JSONL、状态，可附完整 I/O |

`chapters()` 返回 `ChapterInfo(id, edition_id, position, edition_order, title, paragraph_count)`。`position` 是叙事章列表的 **0 基位置**；`edition_order` 是保留前置页等条目后的原生阅读序位。两者都不是标题中的作品章号。调用链使用 `id`，不通过编号偏移推测引用。

## 3. 三种参考模式

### source_only

只按日文事实和本分支已生成译文翻译。可利用事先构建且位于目标之前的源侧 L2 摘要。未建的前文记录为显式 gap；不会为了运行而偷偷全书建索引。

### parallel

调用者提供 `parallel_pairs={source_id: [translation_id, ...]}` 与 `references=[edition]`。允许一个日文章对应多个中文文档；只允许目标窗口之前的明确映射作为完整双语样本。目标 / 未来章的参考答案不能借此进入上下文；重复占用同一中文文档也拒绝。

通用 EPUB/TXT 导入结果不会自动按阅读序号建立双语等价关系。本次没有给旧版 `unique_sequence_key` 规则套上一个会误认为泛用语义对齐的自动接口。已有确认映射可以转换为上面的字典；需要复杂章节映射时，旧 `translation.alignment` 工作流仍然保留。

### cross_edition

两侧独立索引，`associate` 只做指定 L2 候选的事件关联，`match_entities` 整理实体候选。创建任务时显式传入这些产物；资料包保留跨版本事实边界，文库只作为有出处的译名和风格参考。

指定 `references` 不会自动启动全书候选检索或强行选一块译文。没有关联 / 实体证据时，资料包可以没有中文参考，使用者应检查 `plan_next()`。与旧流程一样，跨版本参考可能透露文库未来信息；它不是严格的无泄漏留一评测模式。

`mode='auto'` 只根据调用者是否提供 pairs / references 选择模式，**不宣称自动判断版本是否相同**。显式 index 需刚好包含 source 与本次 references。

## 4. 配置契约

新配置独立于旧 `load_settings(root)`。模板为 `lightnovel_translation/resources/config.example.toml`，支持等价 JSON 和直接构造 `TranslationConfig`。`config init` 在 `~/.lightnovel_translation/config.toml` 创建空模板；新目录/文件限制访问权限，已有文件不会覆盖。用户自行在本机填写 API key，agent 仅通过 `config check` / `user_config_status()` 检查，不读取文件内容。可用 `LIGHTNOVEL_TRANSLATION_HOME` 替换默认目录。

Project 未传 config 时加载上述文件（存在时）；显式配置对象或文件优先，显式空 `TranslationConfig()` 跳过用户配置。没有默认文件仍允许本地导入和规划。模板占位字段会在生成前被拒绝；检查只说明本地配置完备，不探测远端。

- `[model]`：OpenAI 兼容 chat/completions 端点、模型、凭据环境变量、超时、thinking / reasoning、代理配置。无强制 embedding 模型。
- `[capacity]`：调用者声明上下文、输出上限、输入上限、1.05 默认 token 膨胀系数、2048 默认安全余量。默认 128K / 16K 只是保守配置，不是自动识别的模型能力。
- `[roles.*]`：翻译、审校、裁决、抽取、压缩等输出预算；没有显式设置时按 capacity 截断默认值。显式超限会在运行前报错，辅助 role 继承相应主 role。
- `[index]`：独立 L2 的目标 / 最小内容 token 数、最多章节数和 L1 上限。L1 用子摘要汇总，不强迫 L2 为容纳整个 L1 缩小到一章。
- `[sequence]`：最近章 / 块、工作模式、压缩阈值、版本化审校 / 输出协议、工具检索等。对应现有 SequenceSpec 的非预算字段。
- `[limits]`：持久的请求、输入、输出预算和最多 1–3 次任务尝试。每个 index / job 单独记账，不是整个 Project 的共享额度。HTTP 传输层 retries 固定为 0，避免未进入持久账本的额外物理调用。
- `tokenizer_path`：相对配置文件解析；安装 `narrative` extra 后可用本地 tokenizer。未配置时沿用保守 UTF-8 字节计数，再应用预算系数。

未知字段、非法预算、协议不匹配尽早拒绝。凭据可通过本地文件 / 内存里的 `api_key` 提供；为空时从 `api_key_env`（默认 `LIGHTNOVEL_TRANSLATION_API_KEY`）读取。配置快照仅记录脱敏身份。`public()` 是诊断结果，不是可直接重新载入的凭据配置。状态接口和 CLI 不返回 key、配置原文或解析器原始异常；不能打印配置对象的完整内部字段。

没有模型的 Project 仍可导入、规划和读产物；若创建了绑定 unconfigured 身份的任务，配置模型后应创建新任务。恢复已有任务使用冻结的 sequence 配置，新传入的 role / 压缩设置不会改写原任务；模型 / tokenizer 身份改变会拒绝恢复。

## 5. 项目和产物布局

```text
workspace/
  project.json
  imports/<edition-id>/extraction/   # 原文件快照、块、结构修订
  imports/<edition-id>/corpus/       # 标准化正文与来源
  indexes/<identity-prefix>/        # 计划、完整模型调用、摘要、实体 / 关联
  jobs/<name>/
    job.json
    config.redacted.json
    sequence.sqlite                # 冻结输入、付费请求、状态、提交、增量知识
    last-result.json
    export/translation.txt
    export/chapters.jsonl
    export/status.json
    io/                            # 显式 explain / include_io 时导出
```

新导入的路径在 workspace 内相对记录，可整体搬移工作区。`attach_corpus` 对外部已有语料只读挂接；这类绝对路径需要调用者在搬移时安排。文件名 ID 用 ASCII slug；小说标题和正文保留 Unicode。

同一版本 ID / job 名绑定同一输入。章节 ID、来源修订、模型、tokenizer 和冻结参考保持可追溯；不同输入或模型应新建任务。每个 Project / Job 使用一个写入者；`Index.build(workers=N)` 是当前支持的内部并发入口，自定义 provider 需能安全并发。

## 6. 结果、错误和 agent 基础设施

- `ChapterInfo.to_dict()` / `RunResult.to_dict()` 直接 JSON 序列化。
- RunResult 包含 `state`（`complete` / `paused_at_limit` / `blocked`）、任务名、run_id、累计提交数、本次提交 ID、产物路径、结构化错误。`ok` 同时允许按指定数量正常暂停；完成全窗口要判断 state。
- 运行内协议、预算和审校阻断返回 `blocked`，保留既有译文和已支付响应；默认导出已提交结果，草稿通过 explain 可读。成功导出文件不代表模型语义已达到质量验收。
- 前置参数错误为 ValueError / ConfigurationError，文件访问为 OSError；显式运行前配置缺失、冲突、可见性等引擎操作错误为 `TranslationError(code, details, retryable)`。错误结构不返回 provider 的完整原始文本字段。
- `on_event` 同步发出 `chapter_started` / `chapter_committed` / `chapter_blocked`。回调异常交给调用者，数据库中的已提交内容仍保留；当前不提供 token 流事件。
- `lightnovel_translation.providers.TextGenerator` 允许注入自定义 `generate(GenerationRequest)->GenerationResult`。必须提供稳定 `provider_id`，模型 / 实现修订改变时更改它；库不关闭调用者拥有的 provider。
- `lightnovel_translation.ingestion` 提供 inspect / extract / plan / read / patch / validate / export 等结构化工具。随包 skill 指导 agent 先读取相关来源块、提交合法 patch，再由确定性验证器处理；编码歧义、无法整块切分或不支持的格式使用新提取分支或带转换记录的派生文件，不改写冻结来源。

planning/read/status 不隐式调用模型。create_job 只有显式 `resolve_terminology=True` 会为本章术语做模型查证。该操作、build、associate、run、checkpoint 属于可收费操作，适合在 skill 层映射为带预算的明确工具。

## 7. 实施与验证顺序

1. **公共配置与安装**：独立命名空间、Python 元数据、类型标记、无仓库凭据耦合。验证未知参数、脱敏和相对路径。
2. **导入与索引**：复用已完成的输入标准化和独立索引，不改历史 prompt；验证只读无模型调用、选块构建、并发、缓存重放。
3. **任务接口**：打通三种模式、冻结输入、逐章运行、审校、增量知识、压缩、重开和导出。验证跨版本事实边界和目标参考答案隔离。
4. **安装后的验收**：构建 wheel，在独立虚拟环境安装；离开仓库运行公开离线示例和 CLI，检查包文件、依赖和资源。
5. **0.3.0 更名与 skill**：已交付公共包、用户私有配置、脱敏 CLI 与随包技能。
6. **0.4.0 续译起点定位**：已增加部分卷排序、隔离参考作用域、Web 候选召回与局部正文核验；模型建议仍需语义验收，不自动建翻译任务。
7. **后续单独设计**：更通用的自动对齐入口、卷内缺页检测、跨进程调度、共享费用额度、异步 API、复杂分卷优化、EPUB 译文导出、MCP 工具描述。

当前首版不改变模型和翻译 prompt，也不把既往质量问题当作已经解决。离线端到端检查证明的是接口、安装和执行语义；真实语义质量仍使用此前多窗口实验的方法验证。
