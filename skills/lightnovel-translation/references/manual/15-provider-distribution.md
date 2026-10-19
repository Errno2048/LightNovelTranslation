# 15 Provider、分发及底层接口边界

[返回目录](index.md) · 上一步：[恢复导出](14-persistence-recovery.md)

## 模型调用的传输契约

默认 HTTP Provider 向配置前缀的 `/chat/completions` 发送非流式请求，认证通过 Bearer header，不进入 prompt。请求由引擎预算计划和耐久账本约束；公共 `model.retries=0` 关闭不可见的传输层重试。

`GenerationRequest` / `GenerationResult` 是统一调用契约。模型 usage、finish_reason 和原始文本影响后续校准、截断判断与恢复；finish_reason 为 length 等情况保留 partial text，但不能当完整成功输出提交。Provider 不把原始 HTTP 错误正文直接输出给用户，避免把网关数据或认证细节带进诊断。

端点支持哪些上下文、输出、思考字段由用户配置；库不会根据模型名自动宣称 1M/128K 容量，也不会自动转换所有厂商原生 API。兼容网关需接受当前请求格式，`thinking="omit"` 可避免发送专用思考字段。

## 自定义 Provider

```python
from lightnovel_translation import TranslationConfig, TranslationProject

# my_provider 实现 generate(GenerationRequest) -> GenerationResult
with TranslationProject(
    "work/custom", config=TranslationConfig(),
    provider=my_provider, provider_id="gateway/model/revision-1",
) as project:
    ...
```

Provider 必须附稳定且非空的 `provider_id`，覆盖实际模型/版本语义，不能给不同实现同一身份。公共项目把它纳入索引和任务依赖；已有任务身份不匹配时拒绝恢复。caller-owned Provider 生命周期由调用方管理，项目不会主动关闭它。

注入 Provider 可做离线确定性验证，也可接其他模型服务；不能用 Mock 响应通过工程流程就宣称真实翻译质量已达标。主线不强制 embedding；底层保留 embed 协议供历史工具使用。

## 两个包与两套 CLI

| 层级 | 用途 | 使用建议 |
|---|---|---|
| `lightnovel_translation` | 稳定公共 API、配置、项目、索引、任务 | agent 默认调用 |
| `translation` | 随包安装的内部引擎及历史模块 | 修改实现或明确高级集成时定位 |
| `python -m lightnovel_translation` / `lightnovel-translate` | `config init/check/path`、`skill export` | 不提供公共“全流程翻译”子命令，翻译用 Python API |
| `novel-translate` 及内部 `python -m translation...` | 历史开发 CLI | 配置及参数契约不同，不照搬到公共调用 |

内部 `translation.config.load_settings` 仍包含旧 Python 凭据加载路径；公共 `TranslationProject` 不走该路径。agent 不应为调用库去读取旧 key 文件或迁移其他项目凭据。

## 技能和文档怎样随包分发

`skills/lightnovel-translation/` 是唯一维护源：SKILL.md 是入口，references 放按需说明，本手册在 references/manual，assets 放空配置模板。`setup.py` 构建 wheel 时复制整棵技能到包内资源；editable 安装直接使用源码技能树。sdist 也包含这些资源。

`python -m lightnovel_translation skill export <destination>` 递归导出完整技能。写入前检查全部目标文件，遇到本地定制冲突则保留原文件并报 skill_conflict，不做半套覆盖。可先导出新目录，再人工合并需要更新的说明。

因此只安装 wheel 的 agent 也能按目录查本手册。源码路径按仓库/模块和符号定位，不要求技能安装在源码仓库内；不应为了找调用配方遍历整个内部引擎。

## 当前保留但未自动接入的能力

| 模块 | 当前边界 |
|---|---|
| `translation/alignment/` | 规则、序列/embedding 等历史对齐工具；create_job 不自动跑全书对齐 |
| `translation/knowledge/` | 早期知识卡/抽取接口；不是公共任务自动维护的全书核心库 |
| `translation/state/` | 时序、知情与对话状态查询；sequence 抽取结果不自动升级为 reviewed 状态图 |
| `translation/narrative/`、`context/` | 较早的摘要/上下文及当前共享能力；具体接入以 sequence/editions 调用链为准 |
| `translation/sequence/fork.py` | 低层从提交分支/失效处理；公共 TranslationJob 没有等价的便捷 fork API |
| `translation/mvp/dictionary.py` | 可选本地词典；公共配置尚无自动后端接线，不默认在线查询 |

新增能力时应同时明确输入、可见性、持久依赖和失败恢复；不能只因源码有一个类，就在使用说明里声称主线已自动完成该功能。

## 文档维护与代码位置

用户可见行为变化时，更新相应模块页、[API](../api.md)或[操作流程](../workflows.md)，不要复制整份手册到多个位置。优先验证文档链接、配置字段、公开签名和技能分发；仅改文档不需要真实付费翻译。

- `src/lightnovel_translation/project.py`：Provider 注入、身份包装与延迟初始化。
- `src/lightnovel_translation/providers.py`、`src/translation/schemas.py`：公开生成契约。
- `src/translation/providers/__init__.py`：`HTTPProvider`、`ProviderError`、`MockProvider`。
- `src/lightnovel_translation/cli.py`：`main`，公共 CLI 子命令。
- `src/lightnovel_translation/skill.py`：`export_skill`；`_resources.py`：技能资源查找。
- `setup.py`：`BuildWithSkill`；`MANIFEST.in`、`pyproject.toml`：构建和分发声明。
- 回归入口：`tests/translation/test_library_user_config.py`、`test_library_api.py`；打包验证：`scripts/verify_distribution.py`。
