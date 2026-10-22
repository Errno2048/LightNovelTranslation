# LightNovelTranslation

日式轻小说的输入标准化、lazy 分层索引、证据绑定上下文与日译中翻译工具。main 分支使用新的 `translation.index` 架构；旧 `sequence` 引擎归档在 `legacy-sequence` 分支。公共 API 为 `lightnovel_translation`。要求 Python >= 3.11。

## 安装与配置

```console
python -m pip install .
python -m lightnovel_translation config init
python -m lightnovel_translation config path
```

在本机编辑 `~/.lightnovel_translation/config.toml`，填写模型、兼容 API 端点、API key 和实际容量。不要把凭据粘贴到对话或提交到 Git。`python -m lightnovel_translation config check` 只报告就绪状态与缺失字段，不发起模型请求。

配置模板只有一份：[config.example.toml](skills/lightnovel-translation/assets/config.example.toml)。初始化配置和导出技能均使用它。

## Python API（新分层索引）

```python
from lightnovel_translation import TranslationProject
from translation.index import TranslationWorkflow, GlossaryTerm, LevelConfig

with TranslationProject("workspaces/my-novel") as project:
    source = project.import_book("web-ja.epub", edition_id="web-ja", language="ja")
    reference = project.import_book("web-zh.epub", edition_id="web-zh", language="zh")
    flow = TranslationWorkflow(project, "web-ja", "web-zh",
                         levels=LevelConfig((10, 10), (3, 1)))

    plan = flow.reference_plan()                 # 只规划 not_created 边界
    docs = flow.documents(start=0, end=112)
    summary, _ = flow.summarize_reference(start=0, end=100)
    snapshot = flow.make_snapshot(
        summary, summary_start=0, summary_end=100,
        full_start=100, full_end=110, documents=docs)

    result, report, context = flow.translate(
        snapshot=snapshot, documents=docs,
        target_order=110, batch_orders=[110, 111],
        glossary=[GlossaryTerm("イオ", "依尤", "person")],
        unresolved=["前章未决问题"],
        threads={"active": [], "resolved": [], "dormant_relevant": []},
        output_tokens=16384)
    print(result.text[:1000])
```

新流程：lazy 参考规划 → 高层摘要 → ContextPlan + workset/实体/线程/定向证据 → `source-records` 输出协议 → 翻译 → `chapter-review` 审校/裁决/修订 → accepted 提交与 EPUB 导出。当前 runner 使用章节快照与 level-2 摘要；lazy 低层物化器和目标 level-1 自动物化仍是规划能力，尚未接入自动闭环。

**章节闭环**：`translation.index.TranslationRunner` 提供逐章翻译、审校、裁决、有限修订/复审、accepted 提交、滚动摘要与 EPUB 导出；状态保存在 SQLite，可中断续跑。跨版本目录对齐仍需按标题/内容建立映射（可用模型或显式锚点），单章拆分、线程目录和部分质量判定仍可能需人工接管。完整说明见[新架构技能文档](skills/lightnovel-translation/references/new-architecture.md)、[库使用说明](docs/usage.md)和[架构设计](docs/refactor-architecture-design.md)。

## 获取日文 Web 原作

可选采集模块通过官方 Narou API 搜索，使用 FanFicFare 下载 EPUB，不需要模型或 API key：

```console
python -m pip install ".[syosetu]"
python -m lightnovel_translation syosetu lookup "中文第一卷.epub" --keywords 蜘蛛 --proxy http://127.0.0.1:7890 --output work/lookup.json
python -m lightnovel_translation syosetu download n7975cr work/source-01 --first 1 --last 2 --proxy http://127.0.0.1:7890
```

反查优先读取中文文件元信息及头部，再按作者全名检索，最后使用 agent 提供的标题关键词。网络检索回退和作品消歧由独立技能 [syosetu-fetch](skills/syosetu-fetch/SKILL.md) 指导。`lookup` 返回候选，下载使用明确 Ncode；`source.epub` 可直接交给 `import_book`。完整 API、代理、来源清单和离线重建见[采集说明](skills/syosetu-fetch/references/acquisition.md)。

下载默认 2 个线程预取正文，全部线程共享 1 秒请求启动间隔；429 / 503 自动降并发并退避。可在用户配置 `~/.lightnovel_translation/config.toml` 的 `[syosetu]` 节设置 `concurrency = 2`、`request_interval = 1.0`，或用 `--concurrency` / `--interval` 覆盖；`--concurrency 1` 恢复串行。并发范围不是站点允许上限，实测速率会受代理和延迟影响。

## Agent 技能

技能入口为 [skills/lightnovel-translation/SKILL.md](skills/lightnovel-translation/SKILL.md)，使用 Agent Skills 标准目录：入口含 YAML 元信息，按需查阅的文档在 `references/`，配置模板在 `assets/`。技能和所有依赖文档随 Python 安装包分发。

```console
python -m lightnovel_translation skill export ~/.codex/skills/lightnovel-translation
python -m lightnovel_translation skill export ~/.codex/skills/syosetu-fetch --name syosetu-fetch
```

Claude Code 工作区可导出到 `.claude/skills/lightnovel-translation`。导出会保留本地定制；遇到内容冲突时改用新目录检查后再合并。

## 开发与目录

```text
src/
  lightnovel_translation/   项目/配置/导入/技能导出外壳与 CLI
  translation/index/        新分层索引、ContextPlan、lazy 规划、TaskStore/指针、TranslationWorkflow
  translation/              共享存储、预算、provider、ingestion、edition/review 基础
skills/lightnovel-translation/
  SKILL.md                 Agent 技能入口
  references/new-architecture.md  新工作流与手动接管
  references/              旧 API/手册（legacy）与输入修复
  assets/                  无凭据的配置模板
skills/syosetu-fetch/       原作反查、检索与 EPUB 采集技能
docs/refactor-architecture-design.md  新架构完整设计
docs/usage.md              库使用说明
examples/library_offline.py
tests/translation/
scripts/verify_distribution.py
```

```console
python -m pip install -e ".[dev,narrative]"
python -m pytest -q
python -m build
python scripts/verify_distribution.py --wheel dist/lightnovel_translation-0.4.0-py3-none-any.whl --output <temporary-folder>
```

`src` 布局要求先安装再使用。`skills/` 是技能的唯一维护来源；`setup.py` 仅负责在构建 wheel 时将它打包为库资源，开发安装直接读取源目录。构建验证会检查技能和模板逐字一致，并在仓库外运行隔离安装、CLI、配置初始化、技能导出及离线翻译恢复。

目录依据与维护约定见[开发说明](docs/development.md)。
