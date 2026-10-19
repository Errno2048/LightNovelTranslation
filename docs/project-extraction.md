# 独立项目迁移记录

日期：2026-10-19；库版本：0.4.0。

来源为 novelfetch 仓库的 `55fa1c357dc1eeb5e25a612cd815e34ff092e64a`，
主要功能提交为 `9bc29ad5280dce219f9044e56c2bdc9bcd35ef81`。
此次独立整理保留公共 API 包 `lightnovel_translation` 和其运行所需的 `translation` 引擎。
未复制爬虫包、下载小说、用户凭据、实验缓存或原仓库 Git 历史。

库的 Python 文件与来源版本保持一致。README 改为独立项目入口，
分发清单包含本项目的测试、文档和配置模板。历史兼容命令仍随内部引擎提供，
新调用方使用公共 API 和 `lightnovel-translate`；部分设计文档中的历史实验路径属于来源仓库。

验证结果：

- 来源仓库完整回归：583 passed（一个既有爬虫依赖弃用警告）。
- 独立 Python 3.11 环境：82 passed，覆盖公共 API、用户配置、输入标准化和续译定位。
- wheel 构建与 compileall 通过。
- wheel 隔离安装通过，实际从 site-packages 导入。离线五章 mock 流程覆盖暂停、恢复、
  checkpoint、完整 I/O 导出；CLI、空配置检查与技能导出通过。未调用真实模型。

以上说明库安装与工程流程可运行，不代表模型翻译质量达到人工验收。
后续真实小说与 Claude Code CLI 验证保存于被 Git 忽略的 `workspaces/`，不进入初始提交。
