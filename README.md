# LightNovelTranslation

日式轻小说的输入标准化、分层索引、续译定位、连续翻译与审校工具。公共 API 为 `lightnovel_translation`，内部 `translation` 引擎随包安装。要求 Python >= 3.11。

## 安装与配置

```console
python -m pip install .
python -m lightnovel_translation config init
python -m lightnovel_translation config path
```

在本机编辑 `~/.lightnovel_translation/config.toml`，填写模型、兼容 API 端点、API key 和实际容量。不要把凭据粘贴到对话或提交到 Git。`python -m lightnovel_translation config check` 只报告就绪状态与缺失字段，不发起模型请求。

配置模板只有一份：[config.example.toml](skills/lightnovel-translation/assets/config.example.toml)。初始化配置和导出技能均使用它。

## Python API

```python
from lightnovel_translation import TranslationProject

with TranslationProject("workspaces/my-novel") as project:
    source = project.import_book("novel.epub", edition_id="web-ja", language="ja")
    with project.create_job("opening", source=source,
                            chapters=source.chapters()[:3]) as job:
        print(job.plan_next())
        result = job.run(max_chapters=1)
        job.export(include_io=True)
```

完整流程见[操作手册](skills/lightnovel-translation/references/workflows.md)、[诊断与恢复](skills/lightnovel-translation/references/diagnostics.md)及[库使用说明](docs/usage.md)。需要理解配置、数据流、模块边界或定位实现时，按流程查阅[工作原理手册目录](skills/lightnovel-translation/references/manual/index.md)，各模块附对应源码文件和函数。跨版本中文正文可作为文风与术语参考，剧情关联需显式构建两侧索引。通过 `job.reference_report()` 检查实际参考，预算默认按配置容量调整。工程完成不表示译文已经通过人工质量验收。

## 获取日文 Web 原作

可选采集模块通过官方 Narou API 搜索，使用 FanFicFare 下载 EPUB，不需要模型或 API key：

```console
python -m pip install ".[syosetu]"
python -m lightnovel_translation syosetu lookup "中文第一卷.epub" --keywords 蜘蛛 --proxy http://127.0.0.1:7890 --output work/lookup.json
python -m lightnovel_translation syosetu download n7975cr work/source-01 --first 1 --last 2 --proxy http://127.0.0.1:7890
```

反查优先读取中文文件元信息及头部，再按作者全名检索，最后使用 agent 提供的标题关键词。网络检索回退和作品消歧由独立技能 [syosetu-fetch](skills/syosetu-fetch/SKILL.md) 指导。`lookup` 返回候选，下载使用明确 Ncode；`source.epub` 可直接交给 `import_book`。完整 API、代理、来源清单和离线重建见[采集说明](skills/syosetu-fetch/references/acquisition.md)。

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
  lightnovel_translation/   公共 Python API、CLI
  translation/             内部翻译引擎
skills/lightnovel-translation/
  SKILL.md                 Agent 技能入口
  agents/openai.yaml       可选 UI 元信息
  references/              API、操作流程、诊断、输入修复及 manual/ 工作原理手册
  assets/                  无凭据的配置模板
skills/syosetu-fetch/       原作反查、检索与 EPUB 采集技能
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
