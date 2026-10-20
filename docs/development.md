# 开发与分发

## 目录依据

本仓库采用 [Agent Skills 开放规范](https://agentskills.io/specification)（2026-10-19 查阅）与 [Anthropic 官方技能仓库](https://github.com/anthropics/skills)的技能目录形式。规范规定技能目录含带 `name` / `description` YAML 元信息的 `SKILL.md`，可选 `references/`、`scripts/`、`assets/`。本技能没有专用脚本，不创建空的 `scripts/`；仓库的分发验证脚本属于开发工具。

Python 代码采用 [Python Packaging User Guide 的 src layout](https://packaging.python.org/en/latest/discussions/src-layout-vs-flat-layout/)。`src/lightnovel_translation` 是公共接口，`src/translation` 是现有运行引擎；导入名与 CLI 保持不变。

## 维护与验证

修改技能只编辑顶层 `skills/` 下对应的技能目录。翻译配置模板只编辑 `skills/lightnovel-translation/assets/config.example.toml`，不可加入真实 API key。`setup.py` 在构建时复制技能到 wheel 的包资源内；editable 安装读取顶层技能目录，无需生成第二份文件。

1. `python -m pip install -e ".[dev,narrative,syosetu]"` 后运行 `python -m pytest -q`。
2. `python -m build` 默认先构建 sdist，再从 sdist 构建 wheel，验证源分发不依赖仓库外文件。
3. 用 `scripts/verify_distribution.py --wheel <wheel> --output <仓库外临时目录>` 验证技能资源完整性、隔离安装与离线五章翻译恢复。该脚本不访问模型、不读取用户凭据；安装依赖需要包索引网络或本机缓存。

工作区、小说、请求日志、旧构建和临时模型验证脚本保存在仓库外。临时目录应传给示例和验证命令；`.venv`、构建目录及测试缓存可在本地重建，均受 Git 忽略规则约束。

连续翻译历史索引的已知缺陷、直接 L1 优先及仅在容量不足时回退到 L2 汇总的设计，记录在[连续翻译历史索引修复规划](sequence-index-repair-plan.md)。实现前，不要把当前 parallel_pairs 全文回退行为视为已经完成远距 L1 / 近距 L2 滑动窗口。

公开行为变更同时维护 README、使用说明和技能引用文档。
