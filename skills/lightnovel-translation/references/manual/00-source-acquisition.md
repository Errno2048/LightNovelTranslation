# 原作检索与采集

此阶段可选，在输入标准化之前执行。`lightnovel_translation.syosetu` 不初始化翻译模型或调用模型，只使用配置中的 `[syosetu]` 节，按需安装 `lightnovel-translation[syosetu]`。库分发独立的 `syosetu-fetch` 技能，用 `export_skill(path, name="syosetu-fetch")` 或 CLI `skill export <path> --name syosetu-fetch` 导出；其 `references/acquisition.md` 是完整调用和恢复文档。

数据流为：中文 EPUB OPF / 有界头部 → 作者全名与简繁字形候选 → 官方 Narou 元信息检索 → agent 根据关键词及简介消歧 → 明确 Ncode → FanFicFare 下载 → 原始响应快照 / raw.epub → 零宽清洗 / 结构校验 → source.epub → `TranslationProject.import_book`。

作者查询无所需结果时，agent 提供有辨识力的标题关键词；仍不明确时，用外部网络查作者 / 原题 / Ncode，再回官方元信息验证。库不提供搜索引擎抓取，不自动翻译书名，不将第一个检索结果视为同一作品。每词最多 100 条，`truncated` 明确保留有限召回事实。候选不是 cross_edition 章节对应，也不替代后续参考关联。

HTTP(S) 代理通过参数 `proxy` / CLI `--proxy` 传入。请求有超时和有界重试；下载只加载随包默认 INI 和明确参数，不继承个人配置。成功快照包含原作身份、1 基网站话序、canonical URL、修订哈希、原始话标题、篇章归属、清洗计数和输出哈希。网站话序与导入后的 `ChapterInfo.position` / 稳定章 ID 分属两层，不能混用。

`[syosetu] concurrency=2` / `request_interval=1.0` 控制默认正文预取并发和全局请求启动间隔，API / CLI 显式值优先，`concurrency=1` 恢复串行。线程独享下载 session，响应索引串行提交，随后按章序解析与打包。429 / 503 降并发并共享退避，Retry-After 过长则返回错误而不提前重试。`download_stats` 保存实际并发、网络 / 缓存次数及退避；离线重建不启动预取。

响应缓存与已下载 EPUB 不覆盖。离线重建使用新输出目录、原 `cache_dir` 和 `offline=True`；缺页、图片请求失败或缓存损坏都阻断成功状态。更新原作应建立新采集快照，不能在旧翻译任务下替换输入。这里的校验检查打包结构、数量、资源和清洗，不能证明全部原文语义正确。

源码位置：`src/lightnovel_translation/syosetu.py` 的 `inspect_reference`、`NarouCatalog`、`download_work`、`validate_source_epub`；`syosetu_cli.py`；`skill.py` / `_resources.py`；打包入口 `setup.py`。回归场景在 `tests/translation/test_syosetu.py`。
