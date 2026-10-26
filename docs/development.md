# 开发与分发

## 目录依据

本仓库采用 [Agent Skills 开放规范](https://agentskills.io/specification)（2026-10-19 查阅）与 [Anthropic 官方技能仓库](https://github.com/anthropics/skills)的技能目录形式。规范规定技能目录含带 `name` / `description` YAML 元信息的 `SKILL.md`，可选 `references/`、`scripts/`、`assets/`。本技能没有专用脚本，不创建空的 `scripts/`；仓库的分发验证脚本属于开发工具。

Python 代码采用 [Python Packaging User Guide 的 src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)。`src/lightnovel_translation` 保留项目/配置/导入/技能导出等公共外壳；新的分层索引引擎位于 `src/translation/index`。旧 `src/translation/sequence` 引擎和旧 `create_job/job.run` API 已归档到 git 分支 `legacy-sequence`，main 不再包含它们。

新的权威设计文档是 [refactor-architecture-design.md](refactor-architecture-design.md)；用户工作流见 [usage.md](usage.md) 和技能 [new-architecture.md](../skills/lightnovel-translation/references/new-architecture.md)。

## 维护与验证

修改技能只编辑顶层 `skills/` 下对应的技能目录。翻译配置模板只编辑 `skills/lightnovel-translation/assets/config.example.toml`，不可加入真实 API key。`setup.py` 在构建时复制技能到 wheel 的包资源内；editable 安装读取顶层技能目录，无需生成第二份文件。

1. `python -m pip install -e ".[dev,narrative,syosetu]"` 后运行 `python -m pytest -q`。
2. `python -m build` 默认先构建 sdist，再从 sdist 构建 wheel，验证源分发不依赖仓库外文件。
3. 用 `scripts/verify_distribution.py --wheel <wheel> --output <仓库外临时目录>` 验证技能资源完整性、隔离安装与离线翻译示例。该脚本不访问模型、不读取用户凭据；安装依赖需要包索引网络或本机缓存。

工作区、小说、请求日志、旧构建和临时模型验证脚本保存在仓库外。临时目录应传给示例和验证命令；`.venv`、构建目录及测试缓存可在本地重建，均受 Git 忽略规则约束。

旧版 `sequence` 引擎的历史设计、索引修复和实体目录文档（`sequence-index-repair-plan.md`、`token-index-entity-catalog-design.md`）仅作 legacy 参考；它们对应的实现位于 `legacy-sequence` 分支。

main 的当前自动化边界：新索引、lazy 参考规划、ContextPlan、TaskStore/指针、批次并发翻译、pipelined 流水线、实体/形式术语表、标题模式归一化、章节审校/裁决/修订、accepted 提交、滚动摘要和 EPUB 导出已实现（`translation.index.pipeline` / `translation.index.titles`）。strict 模式下的 unresolved major 进入 `manual_review` 队列，`resolve_manual_review()` 负责人工接受/修订/阻塞；翻译可以继续，但 manual hold 之后的批次保持 `ready`，不会自动 accepted 或导出。仍需人工判断：跨版本目录锚点、实体合并/短名关系、target 冲突、单章超长拆分、blocked 章节的局部修复。旧完整管线在 `legacy-sequence` 分支。公开行为变更同时维护 README、使用说明、pipelined 计划和新架构技能文档。
