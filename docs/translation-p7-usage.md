# P7 连续翻译使用说明

日期：2026-10-17。设计见 [P7 设计](translation-p7-design.md)，实测与限制见 [实施报告](translation-p7-results.md)。

2026-10-18 更新：新建运行默认 audit-v6，增加计数容错和摘要软长度；旧 audit-v5 清单仍按原规则恢复。验证见 [适应性处理报告](translation-p7-adaptive-results.md)，下一阶段见 [P8 长程实验设计](translation-p8-design.md)。

## 1. 配置与输入

使用 `config/translation-p7.example.toml`。密钥仍从本地忽略的 `config/apikey.py` 读取，示例不包含密钥。模型、端点、tokenizer、容量或运行规则发生变化时创建新分支，不直接修改已有运行清单。

容量的三种数字分开填写：模型总上下文、模型输出硬上限、各角色本次请求的输出预留。预算为 `ceil(估计输入 × alpha) + 本次输出预留 + 固定余量 <= 总上下文`；alpha 初始 1.05，只根据可比较的实际 usage 上调，缓存命中不减少窗口占用。示例中的 1M / 131072 是本项目配置，换模型时自行调整。

通过 TOML 新建时，覆盖映射／格式转录默认继承 reviewer 的输出预算，补丁修订继承 translator；也可通过 `[roles.mapping]`、`[roles.repair]`、`[roles.revision]` 分别设置。旧 spec 原样读取，不追溯改变其实际请求上限。reasoning 预留包含在总输出预留之内，用于规划及记录，不额外占一次输出预算。

`[sequence].review_profile = "audit-v6"` 使用新增样例和记录计数校准。模型的原始 `zero_findings` 保存在 `declared_summary`，有效值按 FINDING 实际数量计算，冲突写入 `protocol_warnings`；每条实际问题继续裁决。字数上限是软目标，摘要携带 `length_info`，超长不会单独触发拒绝或付费重试。完整请求仍必须满足 token 硬预算，语义覆盖不足及严重错误继续阻断。

`[index]` 的 100/10 是两级章数上限；规划器还检查输入、输出预留和固定开销，因此允许更短且不等长的节点。不能容纳单章时明确失败。分卷作为一级边界尚未实现。

对于较小窗口，可在 `[index]` 设置 `child_min_content_tokens = 32768` 和 `parent_summary_input_tokens_per_child = 8192`，启用“先 L2，再由摘要汇总 L1”的回退。前者为日中正文 token 总数的软下限，0 表示保留旧布局；后者为每份 L2 摘要及其元信息的规划输入配额。输出仍受 indexer 本次预算约束，估计输入仍乘 1.05；发送前以完整消息再次检查。不要把最低正文大小当作必定能满足的硬条件：章数上限、可见性缺口和预算都优先。

`plan` 中可检查 `selected_layout/fallback_reason`、节点 `minimum_exception` 及 L1 的 `budget.basis`。新阈值属于运行身份，修改后需要新建分支。父概括仅在子块摘要全部就绪后生成；缺失子块保留原文，不能以空摘要代替。2026-10-18 的 [DeepSeek 复测](translation-p7-deepseek-results.md) 包含 128k/256k 离线规划、真实局部上卷与相同 prompt 的模型对照，三类证据不能相互替代。

`inputs.json` 是已经过可见性与章节对应审查的冻结输入，主要字段如下：

| 字段 | 内容 |
| --- | --- |
| `targets` | 按阅读顺序排列的待翻译完整日文 document；不含参考中文 |
| `history_pairs` | 目标窗口之前允许使用的完整日文及对应中文 |
| `history_nodes` | 已有历史摘要，含精确 `source_ids`、范围、正文依赖及中文的 canonical source 绑定 |
| `explicit_gaps` | 历史摘要尚未覆盖的明确缺口，不能用空摘要冒充覆盖 |
| `reserved_source_ids` | 保护条目，禁止进入正文依赖 |
| `core` | 可选的冻结核心知识，必须保留来源与候选状态 |

document 沿用 `NarrativeView.body()` 的结构：`chapter_id/revision_id/language/match_key/order/title/text/text_hash/paragraph_ids/paragraph_texts`。历史摘要的依赖携带确切正文；它们用于本地来源核验和工具取证，不自动全部拼入 prompt。输入中不放目标窗口任何一章的原参考中文，包括第一章之后的章节。已有实验可复用 `scripts/run_p7_pilot.py prepare` 生成的冻结输入，但那是本书开发窗口，不是通用语料导入器。

## 2. CLI

以下命令在项目根目录执行；使用已安装依赖的 Python。`--tokenizer` 应指向与运行清单一致的 tokenizer。

```powershell
python -m translation.sequence create --db artifacts/my-run/sequence.sqlite --inputs path/to/inputs.json --config config/translation-p7.example.toml --tokenizer path/to/tokenizer.json --branch trial
python -m translation.sequence plan --db artifacts/my-run/sequence.sqlite --tokenizer path/to/tokenizer.json --output artifacts/my-run/next-plan.json
python -m translation.sequence run --db artifacts/my-run/sequence.sqlite --config config/translation-p7.example.toml --tokenizer path/to/tokenizer.json --stop-after 3
python -m translation.sequence status --db artifacts/my-run/sequence.sqlite
python -m translation.sequence resume --db artifacts/my-run/sequence.sqlite --config config/translation-p7.example.toml --tokenizer path/to/tokenizer.json --stop-after 3
```

`stop-after` 是本次新增提交章数，恢复时不会重新计算已提交章节。`create/plan/status/explain` 不调用模型；`plan` 可以保存本地计划或初始检查点。`run/resume` 执行翻译、映射、规则、审校、裁决及有界修订，可能在下一章之前物化已闭合的 L2。

三种策略为 `R`（逐章重建滚动上下文）、`U`（保留会话追加，不压缩）、`H`（层次检查点，默认）。它们在新建时冻结。`U` 超预算时阻断，不自动换成 H。

```powershell
python -m translation.sequence materialize --db artifacts/my-run/sequence.sqlite --config config/translation-p7.example.toml --tokenizer path/to/tokenizer.json --seal-tail
python -m translation.sequence checkpoint --db artifacts/my-run/sequence.sqlite --config config/translation-p7.example.toml --tokenizer path/to/tokenizer.json
python -m translation.sequence explain --db artifacts/my-run/sequence.sqlite --tokenizer path/to/tokenizer.json --source-id <实际日文章节ID> --output artifacts/my-run/explain
python -m translation.sequence fork --db artifacts/my-run/sequence.sqlite --tokenizer path/to/tokenizer.json --commit-id <保留前缀最后一个提交ID> --output artifacts/my-fork/sequence.sqlite --branch revised
```

`materialize --seal-tail` 为当前已提交尾部建立快照，即便窗口已完成也能执行，不读取未来原文或参考中文。`checkpoint` 可能生成缺少的摘要和短概括，属于显式付费操作；完整窗口结束后没有下一翻译请求，返回空检查点。Python API 的 `plan_checkpoint()` 可事先查看物化范围。

`explain` 导出上下文、来源范围、完整翻译请求、原响应、草稿／最终工作译文及审校结果。新建提交还导出 `actual-request.json/actual-response.json/raw-generation.txt`；早期冒烟提交缺 generation 关联时，独立 HTTP 捕获目录仍保存原始请求与响应。审校等每次物理请求均可通过 SQLite 中的 `review-generation`、attempts 和原响应对象追溯。

若最小检查点仍超预算，`explain` 输出 `budget-diagnostic.json` 和 `prompt-not-sent.txt`，保留分消息 token 占用、软上限、完整待发送消息及覆盖范围；这些消息明确标记为尚未发给模型。

`fork` 继承明确的有效前缀，不继承旧后缀的会话状态。旧章修订先用 `invalidate_from()` 标记依赖，再从该章之前派生分支。更换容量、模型或 tokenizer 的 fork overrides 当前通过 Python API 传入；CLI 暂未暴露全部 overrides。

## 3. Python API

```python
from translation.sequence import SequenceSpec, create_sequence, SequenceEngine
from translation.narrative import TokenCounter
from translation.providers import HTTPProvider

spec = SequenceSpec.from_settings(settings)
counter = TokenCounter(tokenizer_path)
repo = create_sequence(db_path, spec, frozen_inputs, branch_name="trial",
    provider_identity=settings.generation.public(), counter_identity=counter.identity)
engine = SequenceEngine(repo, HTTPProvider(settings.generation,
    max_requests=spec.max_requests), counter)
plan = engine.plan_next()                 # 本地预检
result = engine.advance_sequence(stop_after=3)
repo.close()
```

调用方应保证 provider 内部自动重试关闭，所有重试计入持久账本。每个逻辑任务最多三次物理调用，工具续接共用上限；网络结果未知仍保留预算。已落盘响应恢复时复用，不重发长上下文。语义审校失败不会自动当作“零发现”。

## 4. 状态与知识

默认 `working` 只表示流程完整的工作版本，允许保留带标签的未决；机械硬失败、审校不完整或修订后仍确认存在的严重问题会阻断。`accepted_only` 继续走 P5 正式接受规则，不通过降低标准推进。模型零发现也不代表经人工认证。

已提交的原文和最终工作译文形成双语 L2；摘要、实体译法、事件、状态、视角及时间约束同次抽取。同父 L2 按阅读顺序携带前驱摘要，父元信息确定性上卷。生成中文只能证明本分支实际使用过的译法，不能覆盖日文事实。

检查点保留最近 N 章完整双语、近 L2、远 L1、核心知识、候选及未决。抽取失败且没有可用摘要的范围继续保留全文；坏引用被隔离，缺口显式注入，工具可取回原始记录。所有范围记录 `full_pair/L2/L1/explicit_gap`，覆盖不等于语义无损。必要信息仍放不下时返回 `minimum_context_does_not_fit`，不暗中丢弃远块。

当前结构化工作集采取保守保留策略，长到无法容纳时阻断；相关性筛选和实体消歧不是本轮新增功能。缓存友好仅指稳定消息前缀，实际命中以供应方 usage 为准。
