# Claude Code 调用翻译技能的真实测试

日期：2026-10-19。结果：**通过小型端到端工程验证**，翻译质量仍为待人工验收的工作稿。

## 工作区与调用方式

测试工作区为 `C:/Users/Administrator/lnt-tests/claude-skill-20261019`，独立于开发仓库。新建虚拟环境安装了 `lightnovel_translation 0.3.0` wheel，技能通过公共 `skill export` 命令导出到工作区的 `.claude/skills/lightnovel-translation/`。

输入是原创三章日文 TXT（611 个非空白字符），以及第一章完整中文样章。三个标题没有编号，全部章节合并在一个文件中；要求 agent 检查并修复章节划分，建立第一章的已确认双语历史映射，仅翻译后两章。

实际调用本机原生 `claude.exe`，版本 2.1.143，以 `/lightnovel-translation` 启动任务。CLI 使用 `deepseek-flash`，开启非交互 stream-json 输出；工具限定为 Bash、Read、Write、Edit、Glob、Grep、Skill，没有调用子 agent。完整调用、任务说明及事件记录均保留在工作区中。

## 连接问题及处理

用户填写配置后，脱敏 `config check` 确认为就绪，没有读取或展示配置正文。

最初直接沿用 CLI 配置时返回 404，声明模型为 `deepseek-flash[1m]`，认证状态显示 first-party OAuth。显式指定 `deepseek-flash` 后仍然 404，因此不能将问题归因于模型名后缀。随后仅通过子进程环境传入库配置中的端点与 key，仍未成功。

受控探测发现：同一网关经 HTTPX 默认连接方式返回 502；设置 `trust_env=False` 后，模型列表、OpenAI chat/completions 与 Anthropic messages 均返回 200，且模型列表包含配置模型。探测进程未发现 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 环境变量；差异与 HTTPX 读取系统代理的行为相符。

最终会话使用临时 settings 显式指定网关端点、NO_PROXY 与空代理设置；API key 仅通过子进程环境提供，不放入命令行、settings 文件或 prompt。库调用在内存中使用 `trust_env=False`。此次覆盖后 CLI 完整执行成功，用户的翻译配置和 Claude Code 全局配置均未改动。尚未单独分离验证 CLI 中具体哪一项原配置导致了 404。

原 npm 安装的 `claude.cmd --version` 报 Windows 可执行文件不兼容，故本次使用可正常启动的原生安装。这与翻译库无关。

## Agent 实际完成的工作

| 场景 | 结果 |
|---|---|
| 技能发现 | CLI 初始化事件包含 lightnovel-translation，读取了 API 与输入修复参考文档 |
| 自动结构检查 | 发现三章被识别为一个无标题条目，共 39 块（含空行） |
| 人工结构修复 | 按实际标题块提交 set_boundary 补丁，保留初始提取证据 |
| 段落策略 | 从 auto 改为新分支的 blank_line 提取，消除歧义警告 |
| 最终日文 corpus | 3 章，正文段落数 5 / 6 / 6，校验 ready、0 issue |
| 最终中文样章 corpus | 1 章，5 段，校验 ready、0 issue |
| 历史映射 | parallel，第一章作为明确对应；仅后两章为目标 |
| 首次执行 | run(max_chapters=1) 提交一章并正常 paused_at_limit |
| Checkpoint | 保存后关闭并重开 Project / Job，恢复原任务 |
| 续译与增量索引 | resume(max_chapters=1, materialize=True) 返回 complete，两章增量任务均完成 |
| 段落覆盖 | 两章均 6/6，无覆盖错误 |
| 审校与裁决 | 均完成，自动审校零 findings，保留 semantics_unreviewed 标签 |
| 完整 I/O | 两章均导出实际请求、响应、原始输出、覆盖、审校、裁决与提交 |
| 独立恢复复验 | 再次 resume 已完成任务，仍为 complete，未新增请求 |

独立核对确认诺亚、优衣、里奥的译名沿用样章；三年前的回忆、三枚银币、正午前一小时、水镜，以及诺亚尚未告诉里奥货车男子事情的知情差异均有对应译文。这里的抽查不构成整体语义验收。

## 请求与开销

翻译库的持久账本共 **10 次尝试：9 完成、1 失败**。其中翻译 2、审校 2、裁决 2、知识分析 4。用量合计 57,158 prompt tokens、41,133 completion tokens，包含 31,795 reasoning tokens；失败尝试也已计账。

第 9 次知识分析的 16,384 completion tokens 全部用于 reasoning，返回 truncated；第 10 次成功，使用 14,955 completion tokens，其中 reasoning 11,107。即使输入仅为短篇，抽取仍可能耗尽推理预算；这次失败不能归因于长原文越界。重试上限设为 2，未通过换任务名或改数据库绕过限制。

上述用量不包含 CLI 自身的编排。成功 CLI 会话约 413 秒，num_turns=63；其独立统计为 54,265 input tokens、31,707 output tokens、2,496,000 累计 cache-read input tokens。另有两次很短的协议探测生成。CLI 自报的美元估价未按用户网关费率核验，因此不作为实际费用。

## Checkpoint 验证的边界

此次验证证明 checkpoint 可生成、保存并用于重开任务；没有证明压缩能减少长篇请求。

首轮输入估算为 10,105 tokens（乘安全系数后 10,611）；续译为 30,003（安全值 31,504）。二者目标和历史不同，不能直接作为同一输入的压缩前后对照。

对于第二个目标，checkpoint 记录的 previous_budget 安全输入为 20,267；checkpoint 自身基础消息预算为 19,040，但最终编译、加入定向证据后的实际请求预算为 31,504。基础 checkpoint 预算不能替代最终请求预算。当前 n_chapter=3，前文只有两章，仍全部保留；新抽取知识和定向证据增加了上下文。compact_ids 为空，未触发 L1 摘要再压缩。这说明短窗口的强制 checkpoint 可以增加最终 prompt，需要更长窗口验证真正的历史替换与压缩收益。

## 仍存在的问题

- 导出标题仍为日文原题，标题翻译未纳入当前正文管线。
- 自动审校零发现不代表语义已接受；两章均为 experimental_working / semantics_unreviewed。
- CLI 自检提出了语气与时间边界的待核实意见，但它也是模型判断，不属于人工审阅结论；其报告还有一处章节编号引用不准确，不能将其意见直接认作错译。
- 小样本知识抽取仍产生较多 reasoning 与一次截断，增加成本和等待时间。
- Agent 使用了多次 API 签名检查与临时结构分支。流程正确，但 CLI 编排开销明显，后续可用更具体的高层修复示例减少探索。
- 本次覆盖 source/TXT、明确 parallel 映射、两章连续翻译；没有实测跨版本关联、复杂 EPUB 或长篇语义质量。

## 安全与证据位置

独立检查了 150 个非虚拟环境文件，未发现配置 API key 的明文；CLI 事件中未发现读取凭据文件的 Read 调用，也没有工具权限拒绝。密钥仅由库在进程内加载和传给本次子进程，不输出 key、前缀或长度。

以下均相对于测试工作区：

- `TASK.md`、`invocation.txt`、`launch_cli.py`：任务与受控调用。
- `claude.events.jsonl`、`attempt-02/03.events.jsonl`：初始失败记录。
- `attempt-04.events.jsonl`、`attempt-04.exit.json`：成功的完整 CLI 事件和退出状态。
- `gateway-probe.json`、`gateway-probe-direct.json`：脱敏协议探测。
- `report.json`：CLI 自行生成的报告。
- `independent-verification.json`、`verify_result.py`：独立验证结果与脚本。
- `work/export-final/translation.txt`、`chapters.jsonl`：最终两章译文。
- `work/export-final/io/`：每章完整 prompt、实际请求、响应、审校、裁决和覆盖记录。
- `work/report/plan-initial.json`、`plan-resume.json`：初始与续译完整输入。
- `work/project/jobs/ja-parallel-20261019/sequence.sqlite`：持久请求与提交账本。
- `work/final-ja/extraction/patches/`：结构修复证据。

本次没有修改翻译引擎或用户配置，也没有执行 git commit。验证对象为已经构建的 0.3.0 安装包，因此未为文档变更重复运行全量离线回归。
