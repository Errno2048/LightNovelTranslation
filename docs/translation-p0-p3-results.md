# P0–P3 最小方案：实现、运行与初步验证

日期：2026-10-15。

P0–P3 的最小功能已实现，可以从本地原始缓存/EPUB 导入语料、建立章级对应、构造隔离参考答案的实验输入、调用真实或模拟模型、恢复任务并生成 A/B 对照页面。已完成第 3、775、1233 章的六次真实翻译。当前输出均为待审查草稿，尚未经过 P5 的完整语义审校。

核心发现是：邻近双语示例有助于部分译名延续，但无法保证忠实度；现有中文语料存在需要先处理的段落顺序问题，不能直接作为全书可靠金标准。

后续更新：已定位为网站脚本还原步骤遗漏，并完成独立语料分支的全量顺序修复及五章结构分区。原 P3 实验和本报告历史结果保留不变；最新证据、边界及后续使用方式见 [参考译文顺序与结构修复](translation-reference-repair.md)。

## 1. 已实现的模块

| 阶段 | 功能与验证 | 当前边界 |
| --- | --- | --- |
| P0 | 独立生成/向量配置、HTTP 与 mock 提供方、JSON 本地校验、错误分类、超时和有界重试、请求上限、原子写入、指纹与恢复 | 上下文上限、原生 schema、流式能力未宣称已验证 |
| P1 | 三份 EPUB 全量导入、日文原始缓存优先、正文/前后记/译注分类、作者注音保留、稳定编号、版本与哈希、异常报告 | 零宽字符保留并标记；没有擅自修复疑似乱序 |
| P2 | 章号/番外序列锚点、候选映射、审查补丁、重跑保留审查、局部多对多 DP、向量检索探测、显式语义窗口候选、向量缓存 | 全书 1320 对中只抽查确认了 9 对的章节身份；候选分数尚未校准 |
| P3 | 固定开发/验证/盲测划分、三种可见性策略、已审查前缀示例、A/B 基线、生成与参考文件隔离、恢复、对照/盲评页面、错误标注模板 | 六次真实生成属于小样本；尚未完成开发集全面评测或独立人工盲评 |

代码位于 `translation/`，当前用版本化 JSON/JSONL 和文件事务保存最小方案的数据。没有引入数据库服务；P4 以后的证据和状态复杂度增加时再决定是否增加 SQLite。

## 2. 环境与配置

本机验证环境为 Python 3.11.4，入口命令使用 `py -3.11`。依赖已在本机该环境中存在；项目声明在 `pyproject.toml`，新环境可安装：

```powershell
py -3.11 -m pip install -e ".[dev]"
py -3.11 -m pytest -q
py -3.11 -m translation --help
```

默认从当前项目根目录读取 `config/apikey.py`，配置优先级为：显式 TOML > `NOVELFETCH_*` 环境变量 > 本地配置。生成和向量模型分别读取 `LLM_*`、`EMBEDDING_*`。

实际接口探测：

- `glm-5.3-flash`：短文本生成与指令约束 JSON 均通过，本地校验结果为“你好”，正常返回 `stop` 和用量。
- `embedding-3`：两个输入正常返回两个有限、非零的 **1536 维**向量，配置维度无需修正。
- 真实翻译中观察到 `cached_tokens` 字段，六次基线合计 128 个缓存输入 token；当前尚未进行 P7 的缓存优化。
- 生成与向量请求使用各自配置的端点。接口成功仅记录为技术能力，不扩展为对账号其他模型、参数或服务使用约定的判断。

能力报告：`artifacts/translation/tenken/provider/provider_capabilities.json`。

更换同协议的生成模型，可在新的 TOML 中指定模型与参数。若供应商不接受智谱的 `thinking` 字段，可配置 `thinking = "omit"`。更换端点时必须显式指定新的凭据环境变量，避免把本地旧凭据发往新端点：

```toml
[generation]
model = "replacement-model"
base_url = "https://provider.example/v1"
api_key_env = "REPLACEMENT_API_KEY"
thinking = "omit"
timeout = 240
retries = 2
```

```powershell
py -3.11 -m translation --config config/replacement.toml probe
```

向量模型、端点、维度或文本变化会使向量缓存身份变化。更换生成配置或实验输入须使用新的运行目录；旧结果和原始运行指纹保留。

## 3. 全量语料与章节映射

| 类别 | 数量 |
| --- | ---: |
| 日文正文 | 1357 |
| 日文资料 | 2 |
| 中文正文 | 1320 |
| 中文封面/卷介绍/作品相关 | 5 |
| 所有阅读条目 | 2684 |
| 保留的段落记录（含空段、前后记和译注） | 282126 |
| 一对一章节候选 | 1320 |
| 原文单侧条目 | 37 |

全部章节 ID 与段落 ID 唯一，段落引用完整。导入后重新核对了三份原始 EPUB 和 1359 个日文原始缓存的哈希，均未变化。

当前映射中 9 组已确认章节身份，其余 1348 组保持候选；这里包括尚未正式确认缺失原因的 37 个原文单侧条目，不会自动将它们判为可发布的补译任务。`confirmed` 不代表参考译文逐段无误，质量状态独立保存。

向量探测选择了 2/3/4、774/775/776、1232/1233/1234 章，各原文在九个译文候选中均以对应章作为第一名（9/9）。这只是基于序号标签的小型检索检查；部分第一、二名分数差很小，不能推导出全书准确率。

774–776 章的真实局部语义 DP 返回了三组顺序一对一候选。局部窗口还允许跳过、拆合章，结果需要显式审查补丁才能生效。

产物目录：

```text
artifacts/translation/tenken/corpus/
  corpus_manifest.json
  chapters.jsonl
  paragraphs.jsonl
  corpus_issues.jsonl
  ingest_report.md
artifacts/translation/tenken/alignment/
  groups.jsonl
  alignment_report.md
  alignment_manifest.json
  manual_patch.json
  prefix_review.json
  example_catalog.jsonl
  semantic_probe.json
  window_candidates.json
```

## 4. 参考译文的实际问题

1186 个中文条目被标记含零宽字符。这是保留并复核的信号，不等于这 1186 章都已证明乱序。

抽查第 3、775、1233 章时，发现部分译文段落相对原文明显移位，例如第 775 章靠近原文末尾的救援与忠告内容，在中文中出现在更早的位置；第 1233 章末尾附近也有更早事件的段落。检查对应 `temp/linovelib_*.html` 后，原始缓存中已经存在这种排列，并非本次导入器重新排序造成。

因此：

- 这三章参考译文标记为 `order_suspect`，只作为对照资料。
- B 基线仅使用邻近章节中逐一查看过的**前五个非空段落**，没有把整章疑似乱序内容放进示例。
- 原有文字和零宽字符完整保留，可以回查；当前阶段不进行 LLM 猜测排序，也不自动修正参考答案。
- 在修复或整理出可靠的评测参考子集之前，未扩展到十二章开发集或最终盲测。

这项问题应优先进入后续语料质量工作，否则术语抽取仍可能可用，而段落对齐、局部风格和内容评分会受到污染。

## 5. A/B 实验与初步效果

A：仅原文与统一翻译指令。  
B：A 加两个邻近章节的短双语前缀示例，各五个非空段落。

当前示例选择上限为两个，阅读顺序距离不超过八个条目；不足两个时使用已有的合格示例，没有合格示例时拒绝运行 B，避免悄悄退化为 A。补缺协议允许两侧已译示例；严格顺序协议只允许截止点前示例。清单中的 `allow_future_source` 为严格顺序实验的额外原文前瞻预留字段，当前关闭，该实验能力尚未实现；双侧示例权限由协议模式决定。

目标为第 3、775、1233 章，使用相同生成模型、temperature 0.2、8192 输出 token 上限。生成侧仅读取 `generation/tasks.jsonl`，参考译文单独放入 `sealed/references.jsonl`；目标译文及中文标题没有进入 prompt。为避免同批目标互相成为示例，本次将三个目标一并屏蔽；这是保守的批量单章补缺实验，未启用连续翻译状态。

| 章节 | A 输出字符 | B 输出字符 | A 字符 n-gram F2 | B 字符 n-gram F2 |
| --- | ---: | ---: | ---: | ---: |
| 3 | 3376 | 3418 | 31.472 | 32.895 |
| 775 | 1962 | 1972 | 31.736 | 38.306 |
| 1233 | 1991 | 1880 | 30.288 | 30.929 |

这里的 F2 是本项目实现的描述性字符重合指标，不是 SacreBLEU 的标准化 chrF 实现，不衡量人物理解或文学质量，也不能将疑似乱序参考当成可靠答案。B 的平均值高于 A，不足以证明 B 总体翻译更好。

抽样查看原文和输出得到以下具体观察，已记录带原文/输出定位的标注；这属于开发者审查，并非独立人工盲评，也不是穷尽错误计数：

| 位置 | 观察 | 含义 |
| --- | --- | --- |
| 1233：フラン、エイワース | A 使用“弗兰、艾沃斯”，B 延续示例中的“芙兰、艾瓦斯” | 邻近示例能提供部分命名约定 |
| 1233：ホワイト | A 使用“怀特”，B 写成“白色” | 示例之外的实体仍会被误当普通词，需要术语约束 |
| 3：ゲギャ！ | B 译成“去死吧！” | 不明语言被添加明确语义，需要忠实度检查 |
| 3：気配 | B 使用“杀气” | 普通气息被增加敌意解释 |
| 775：スパルタ | A 留下“Spartan得……” | 流畅度和未翻译表达仍需审校 |

B 也留下 `_rank_A` 一类不必要格式。初期长度和截断检查未发现这些语义问题；这正是 P4 的术语约定和 P5 的语义审校需要验证的错误类型。

调用与用量：

- 六次整章生成全部正常结束，没有重试或输出截断。
- 基线合计输入 **16227 token**，输出 **9269 token**，其中供应商记录缓存输入 128 token。
- 六次接口调用耗时合计 **221.109 秒**，不含导入、探测与人工审查。
- 向量接口另用于两条合成探测文本、18 条章节摘录和10条局部窗口文本，返回用量分别为 22、15027、8340 token。
- 另有一次 47 token 的合成生成探测；本轮上述真实请求共 10 次，记录用量合计 48932 token。不同接口 token 不代表相同价格，未据此估算人民币费用。

重复执行同一个运行后，结果为 `resumed: 6`，没有新增模型请求。

## 6. 操作方式

在项目根目录依次运行。以下语料和审查产物已生成，重新执行接口探测或向量实验可能产生调用；恢复已经完成的同一翻译运行不会重复生成。

```powershell
py -3.11 -m translation ingest
py -3.11 -m translation align
py -3.11 -m translation probe
py -3.11 -m translation splits
```

当前规则对齐重跑会保留同一文本版本上已有的审查；若人工拆合结构或文本版本变化，将要求使用新分支，避免覆盖审查。当前书的审查补丁和前缀批准文件绑定语料指纹：

```powershell
py -3.11 -m translation review --patch artifacts/translation/tenken/alignment/manual_patch.json
py -3.11 -m translation examples --review-file artifacts/translation/tenken/alignment/prefix_review.json
py -3.11 -m translation prepare --keys main:3 main:775 main:1233 --experiment smoke
py -3.11 -m translation run --experiment smoke --run p3-smoke-glm --dry-run --max-tasks 6
py -3.11 -m translation run --experiment smoke --run p3-smoke-glm --max-tasks 6 --max-requests 9
py -3.11 -m translation report --experiment smoke --run p3-smoke-glm
```

`--max-requests` 包含当前进程内重试；失败/远端状态未知任务默认不会自动重复，确认需要重试时显式传 `--retry-failed`。更换输入、prompt 或模型配置时选择新的 `--experiment`/`--run` 名称。报告重建会保留已有人工错误标注。

无接口的离线运行：

```powershell
py -3.11 -m translation run --experiment smoke --run p3-offline --mock --max-tasks 6
```

`prepare --mode block_gap` 隐藏整组目标，`prepare --mode sequential --cutoff-order N` 仅开放截止点前参考资料。`N` 是规范化原文阅读顺序，不是标题中的章号；该书前面有两篇资料，不能直接把两者混用。P3 的顺序协议仍是独立章节基线，跨章传递生成状态属于 P7。

小范围语义对齐复核示例：

```powershell
py -3.11 -m translation semantic-probe --keys main:2 main:3 main:4 main:774 main:775 main:776 main:1232 main:1233 main:1234
py -3.11 -m translation align-window --source-keys main:774 main:775 main:776 --target-keys main:774 main:775 main:776
```

两类命令产生待复核候选，不自动覆盖正式映射；向量请求按配置与文本指纹缓存。

## 7. 验证证据与下一步

自动测试覆盖接口失败/重试/截断、向量顺序和维度、配置替换、注音和前后记、UTF-8、重复导入、拆合/缺失/重排、审查保护、隐藏资料与派生依赖、冻结实验、恢复、盲评和标注保留。

另外对真实数据完成全量段落引用和输入哈希验证，并检查生成代码、文档和 JSON/JSONL 产物未包含配置中的密钥值。验证记录为 `artifacts/translation/tenken/p0-p3-data-verification.json`。

主要结果入口：

- 对照阅读：`artifacts/translation/tenken/runs/p3-smoke-glm/reports/comparison.html`。
- 匿名候选页面：同目录 `blind/`；解盲映射独立存于 `sealed_blind_key.json`。
- 用量和诊断指标：`results.md`、`metrics.json`、`summary.json`。
- 抽样错误证据：`error_annotations.jsonl`。

下一步优先整理一个顺序可靠的参考子集，再进入 P4 的段落对齐与术语约定。当前小实验已经提供了明确的目标错误：姓名普通词混淆、示例未覆盖的译名、不明对白增义和语气推断。应围绕这些错误比较 C 基线，并在可靠参考上判断新增知识是否带来净收益。
