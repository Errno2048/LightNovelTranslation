# 调用与开发约定

使用库执行导入、索引、续译或翻译时，先阅读 [README](README.md)、[库使用说明](docs/usage.md)及[操作流程](skills/lightnovel-translation/references/workflows.md)；遇到阻断查[诊断与恢复](skills/lightnovel-translation/references/diagnostics.md)。使用公开 API 和保存的诊断产物，不先遍历 `src/translation` 内部源码寻找调用方式。

需要解释工作机制、配置生效范围或模块交互时，从[工作原理手册目录](skills/lightnovel-translation/references/manual/index.md)按当前流程选择模块页；各页列出数据流、持久化产物、费用边界及源码符号。不要一次性读取全部手册；仅在文档不足或需要修改实现时读取对应源码。

修改库本身时可以直接阅读相关实现。公共行为变更同时更新随包 API/操作文档和有意义的回归场景。默认在独立项目维护翻译功能，不依赖原爬虫项目的脚本、凭据或目录结构。凭据文件不进入工具输出、Git、测试夹具或报告。
