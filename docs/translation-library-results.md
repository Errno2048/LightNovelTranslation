# Python 库首版实施与验证

日期：2026-10-19；版本：0.2.0。

## 交付

公共包 `novelfetch_translation` 与现有 `translation` 引擎同时打包为 `novelfetch-translation`。新接口不要求仓库工作目录、爬虫依赖、`config/apikey.py` 或 embedding 配置；既有 CLI 和内部协议保持兼容。

实现对象为 TranslationProject、Edition、Index、TranslationJob、TranslationConfig、ChapterInfo、RunResult，以及结构化 TranslationError。覆盖 EPUB/TXT 导入、标准语料挂接、独立 L2 / L1 索引、事件与实体关联、三种参考模式、连续翻译和审校、暂停与恢复、知识增量、上下文压缩、完整 I/O 导出。

设计及边界见 [公共接口设计](translation-library-design.md)，调用示例见 [使用说明](translation-library-usage.md)，配置为 `config/library.example.toml`。

## 验证证据

| 检查 | 结果 |
|---|---|
| 新增公共 API 场景 | 26 项通过 |
| 全量 `tests/translation` | **558 passed**，35.82 秒 |
| 原有 warning | 1 条，来自已有 pykakasi 的 pkg_resources 弃用提示 |
| 编译 / 差异空白检查 | compileall、git diff --check 通过 |
| wheel | 成功构建并在全新虚拟环境安装 |
| source distribution | 成功构建，包含配置模板、公共接口文档、离线示例和 API 测试 |
| 仓库外执行 | `python -I` 从临时目录加载 site-packages 中的安装包 |
| 已安装库的完整流程 | 五章，先提交两章、压缩、恢复剩余三章、增量索引、完整 I/O 导出 |
| 已安装 CLI | `novel-translate --help` 正常 |
| 真实模型调用 | **0**；固定 provider 共 14 次本地模拟回复 |

公共 API 测试包括：

- 读取 / 规划无模型调用，按需建索引、不同 L1 并发、重建复用已付费缓存。
- source_only、显式 parallel 和 cross_edition 三种模式均可执行到提交；parallel 拒绝目标答案混入历史，cross_edition 保留版本事实边界。
- 源段输出协议缺失会在有界尝试后返回 blocked；重新运行不会无界重复调用。
- 一次请求预算耗尽时保留已生成草稿，重开任务也不会重复翻译。
- 冻结 prompt、重开恢复、显式压缩、尾块增量索引以及逐章 I/O 导出。
- 变更 provider 身份时拒绝恢复；整个工作区搬移后仍可恢复；篡改标准语料会被检出。
- 配置字段、输出预算、相对 tokenizer 路径、环境凭据、脱敏文件和客户端生命周期。

最初的公共索引测试暴露 Windows 默认路径长度限制：完整 64 位索引目录名叠加调用缓存文件名可能导致 FileNotFoundError。新公共索引目录使用 24 位身份前缀，并用完整身份清单校验复用；原引擎的历史文件布局和调用 ID 不变。

## 可复现入口

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests/translation -q
.\.venv-test\Scripts\python.exe -m pip wheel . --no-deps --no-build-isolation --wheel-dir artifacts/translation/library-packaging/wheels
.\.venv-test\Scripts\python.exe scripts/verify_translation_library.py --wheel artifacts/translation/library-packaging/wheels/novelfetch_translation-0.2.0-py3-none-any.whl --output artifacts/translation/library-packaging/verification-final
```

构建产物位于 `artifacts/translation/library-packaging/wheels/` 和 `sdist/`；独立安装、模块位置、CLI 与离线执行结果保存在 `verification-final/report.json` 及同目录日志中。该验证脚本只读取显式传入的 wheel，不读取私有模型凭据。

本次未重新进行真实小说语义质量评测。库首版完成的是安装和可调用的工程闭环；既往错译、抽取漏项、审校漏检仍保留原有质量结论。通用自动对齐的统一高层入口、异步任务、共享费用额度及实际 skill 适配留待后续设计。
