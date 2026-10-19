# LightNovelTranslation

日式轻小说的输入标准化、分层索引、续译定位、连续翻译与审校工具。
公共 API 位于 `lightnovel_translation`，底层 `translation` 引擎随项目安装。要求 Python >= 3.11。

## 安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,narrative]"
```

## 配置

```powershell
.\.venv\Scripts\python.exe -m lightnovel_translation config init
.\.venv\Scripts\python.exe -m lightnovel_translation config path
```

在本机编辑 `~/.lightnovel_translation/config.toml`，填写模型、兼容 API 端点、API key 和容量。不要把凭据粘贴到对话或提交进 Git。`config check` 仅报告就绪状态和缺失字段，不发起模型请求。
配置模板见 [config.example.toml](lightnovel_translation/resources/config.example.toml)。

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

完整流程见[库使用说明](docs/translation-library-usage.md)、[API 参考](lightnovel_translation/resources/skills/lightnovel-translation/references/api.md)及随包 [SKILL.md](lightnovel_translation/resources/skills/lightnovel-translation/SKILL.md)。跨版本参考需显式构建两侧索引和关联；仅提供中文文件不会自动建立双语映射。

## 测试与构建

```powershell
.\.venv\Scripts\python.exe -m pytest tests/translation -q
.\.venv\Scripts\python.exe -m build
```

`scripts/verify_translation_library.py --wheel <wheel> --output <folder>` 验证隔离安装、CLI、配置与技能导出。工程完成状态不代表语义质量已经通过人工验收。

本仓库从 novelfetch 项目的翻译模块独立整理而来；不包含爬虫、小说数据、用户凭据或实验数据库。源版本及迁移验证记录见 [迁移说明](docs/project-extraction.md)。
