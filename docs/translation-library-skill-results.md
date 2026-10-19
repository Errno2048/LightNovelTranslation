# 0.3.0 更名、私有配置与 agent skill

日期：2026-10-19。首版 0.2.0 的历史名称和验证证据保留在 [原报告](translation-library-results.md)。

公共模块更名为 `lightnovel_translation`，安装名为 `lightnovel-translation`，版本 0.3.0。调用方需更新原 `novelfetch_translation` import；现有 `translation` 引擎、`novel-translate` CLI、持久工作区 schema 和历史实验协议保留。更名未引入新的真实模型质量结论。

## 配置与安全边界

`python -m lightnovel_translation config init` 创建 `~/.lightnovel_translation/config.toml` 空模板，不覆盖、读取或自动迁移旧文件。用户在本机填写模型、端点、API key 和容量；不需要在对话中提供密钥。默认 Project 自动复用该配置，显式配置对象或文件优先；显式 `TranslationConfig()` 可跳过用户配置执行本地工作。

支持 `LIGHTNOVEL_TRANSLATION_HOME`、`--directory` 及空 key 时的环境变量后备。`config check` / `user_config_status()` 只报告路径、状态和缺失字段名；无远程探测。占位模型、占位端点与空 key 会在模型生成前被拒绝。文件语法错误不打印解析器保留的配置原文，repr 与公共快照不包含 key。

POSIX 新目录/文件采用 0700/0600；Windows 为当前用户和 SYSTEM 设置受保护的 DACL。验证中发现外部 `icacls` 会卡住，已改为 Windows 系统权限 API；SID 查询有超时。实际用户配置的 ACL 已检查为受保护，仅当前账户和 SYSTEM 有完全控制权限。未读出任何实际凭据。

本机已创建空配置：`C:/Users/Administrator/.lightnovel_translation/config.toml`。脱敏检查为 valid，但尚未 generation_ready，缺少 `model.model`、`model.base_url`、`model.api_key`；待用户本地填写。

## Skill 与人工修复

随包资源为 [SKILL.md](../lightnovel_translation/resources/skills/lightnovel-translation/SKILL.md)，并附 API 与输入修复参考文档。通过 `skill export DESTINATION` 导出，逐文件预检冲突；相同内容可重复导出，定制文件不会被覆盖。已导出到 `C:/Users/Administrator/.codex/skills/lightnovel-translation`，随包与导出副本均通过 skill-creator 的 quick_validate。

指南覆盖：

- 读取实际章节 ID、标题、来源块并抽查自动划分；选择三种参考模式。
- 用有版本约束的结构补丁修正完整块上的章节边界、标题、分类、卷和 TXT 段落策略。
- 编码歧义使用有证据的 encoding_override 和新提取分支；源块内多章、未支持格式使用带转换记录的派生 TXT/EPUB。
- 修复后验证、导出 corpus、注册新 Edition 并启动新任务；已冻结输入不允许原地替换。
- blocked 时保留已付费响应与已提交译文，读取 explain 诊断，不用补造正文、重复段落或删除错误标记绕过检查。
- agent 不读取凭据文件，不将其复制到工作区、Git、prompt 或报告；只调用脱敏状态接口。

## 验证

| 检查 | 结果 |
|---|---|
| 全量 `tests/translation` | **566 passed**，45.33 秒 |
| 新增配置/skill 场景 | 8 项通过 |
| 原有 warning | 1 条，已有 pykakasi 的 pkg_resources 弃用提示 |
| 编译与空白检查 | compileall、git diff --check 通过 |
| SKILL 校验 | 随包资源、用户技能副本均通过；Windows 使用 Python UTF-8 模式 |
| 构建 | `lightnovel_translation-0.3.0` wheel 与 sdist 成功 |
| wheel 内容 | 含类型标记、模板、SKILL、参考文件；无旧公共包、凭据文件或实验语料 |
| 独立安装 | 新虚拟环境安装，临时工作目录中 `python -I` 加载 site-packages |
| 五章离线闭环 | 提交两章、压缩、恢复三章、增量索引、完整 I/O 导出 |
| 已安装命令 | 旧 CLI help、新 CLI config init/check 和 skill export 通过 |
| 真实模型请求 | **0**；离线示例 14 次固定 provider 回复 |

新增测试还实际执行了“无编号隐藏标题 → 来源块检查 → 两次 set_boundary → 验证导出 → 注册 → 两章离线翻译”，并确认编码歧义不能靠结构 patch 绕过；空模板不覆盖已有配置，配置状态、报错和 traceback 不含合成密钥标记。

仓库外首轮验证在初始化成功后遇到一次 Windows 启动程序拒绝访问，另一轮 venv ensurepip 启动失败；原命令单独复验均成功，最终完整验证通过。保留首次日志，不将环境启动失败归因为模型或翻译流程。

产物目录为 `artifacts/translation/library-skill-packaging/`；最终安装及运行证据见 `verification-final/report.json`，首次失败日志见 `verification/`。当前源码包另含本文、使用文档、离线示例及测试。

复验命令：

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests/translation -q
.\.venv-test\Scripts\python.exe -X utf8 C:/Users/Administrator/.codex/skills/.system/skill-creator/scripts/quick_validate.py lightnovel_translation/resources/skills/lightnovel-translation
.\.venv-test\Scripts\python.exe scripts/verify_translation_library.py --wheel artifacts/translation/library-skill-packaging/wheels/lightnovel_translation-0.3.0-py3-none-any.whl --output artifacts/translation/library-skill-packaging/verification-final
```

本次验证范围为更名、配置隔离、技能可执行示例和安装后的工程行为；已有语义错译与审校漏检不因这些检查而自动解决。
