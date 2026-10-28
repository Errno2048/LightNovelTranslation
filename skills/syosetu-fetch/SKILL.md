---
name: syosetu-fetch
description: 在成为小说家吧按作者或标题检索日文 Web 小说，从中文 EPUB/TXT 反查原作，并通过 FanFicFare 下载可供 LightNovelTranslation 导入的 EPUB。适用于原文获取和作品定位；下载后的翻译另按翻译技能执行。
---

# Syosetu Fetch

使用已安装的 `lightnovel_translation.syosetu` 公共 API 或 `python -m lightnovel_translation syosetu`。安装可选依赖：`python -m pip install "lightnovel-translation[syosetu]"`；源码开发安装用 `python -m pip install -e ".[syosetu]"`。具体参数、离线重建及产物见 [调用与恢复](references/acquisition.md)。

## 定位作品

1. 给定中文文件时，**先用 `inspect-reference` 读取 OPF 和有界头部**，核对标题、作者栏与版权 / 录入页；区分作者、译者、插画家、发布站。元信息缺失时可按需扩大头部或读相应页面，无需先读全书或全文建索引。
2. **优先按作者全名检索**。`lookup` 会尝试原字形和 OpenCC 简转繁变体，并保留确切的作者姓名匹配；这不是日语姓名的权威转换。多个作品同作者时，用书名关键词、简介及头部信息区分。不要把作者相同当作作品相同。
3. 作者没有命中、返回作品中没有所需原作或作者不可信时，拆出少量有辨识力的标题关键词，调用 `search --keywords ...`。例如中文书名里保留的动物、职业、特定人名、关键物品，可优于“转生”“异世界”一类宽泛词。词表由本次作品的实际头部 / 用户信息提供；库不假装能可靠地自动翻译书名。可先用多词排名，再扩大单词检索。返回 `truncated` 时仅代表有限候选，不能宣称全站没有此书。
4. 仍无法定位时，使用 agent 可用的联网搜索，组合中文标题、作者、“原名 / 原作 / 小説家になろう”；优先查出版方、作者及站点作品页，确认日文作者、原题或 Ncode。将得到的日文名传回 `search` / `metadata` 核验。保存查询、证据页面、选定 Ncode 和理由。库返回 `external_search_queries` 供参考，不会自行爬搜索引擎，也不把网络片段当作已确认身份。

匹配结果始终是候选。证据足够时在已授权范围内自行选定并继续，不重复请求批准；只有确实无法区分的作品才向用户说明候选差异。中文文库标题与 Web 标题可能不同，也可能不存在 Web 版，不强行配对。

## 下载与交接

确定作品后，通过 `metadata` 检查作者、简介、Ncode 和话数；再用 `download` 的明确范围生成快照。首次验证可只下载少量话，授权全本获取时再下载全本。`--first` / `--last` 是网站目录中的 **1 基、包含端点的话序**，不是标题的故事章号，也不是导入器的 0 基位置。

下载返回的 `epub_path` 是清洗过的 `source.epub`，可直接 `project.import_book(epub_path, edition_id="web-ja", language="ja")`。检查导入章数、开头结尾与 `validate_inputs()`；中文文库与 Web 默认按独立版本处理，不按章节序号硬配。原始 EPUB、响应缓存、清洗计数、篇章归属、稳定 URL 与修订标识保留在同一快照。

下载结果还包含 `chapter_plan_path`（`chapter_plan.json`）。它按 manifest 的 `section` 和标题规则区分 `main`/`extra`/`character_sheet`/`setting`；把该文件复制到翻译项目根目录，续译 runner 会优先使用它。不要只用 `^\d+话` 判断主线：篇名前缀、裸编号、书信番外和人物表都会造成误判；`main_ratio < 0.90` 时应人工复核分类。

本模块不用模型、API key 或个人 FanFicFare INI。下载仅使用用户配置中的 `[syosetu]` 节，不初始化模型或使用翻译凭据。默认 2 个线程预取正文，全部下载线程共享 1 秒请求启动间隔；`--concurrency 1` 恢复串行，显式参数优先于配置。429 / 503 会降低并发并共享退避，遵守 Retry-After；重试次数用尽或等待超过 60 秒时返回错误并保留缓存，不自动扩大并发重试。每份 manifest 的 `download_stats` 可核对实际并发、重试和限流事件。

网络不通时使用用户给定的代理参数，例如 `--proxy http://127.0.0.1:7890`；不修改系统代理或个人配置，不在报告中输出含认证信息的代理 URL。仅支持普通站公开作品；不尝试登录、验证码绕过或成人站获取。

失败时保留 manifest 和缓存，检查错误代码。已有成功 / 失败目录都不覆盖；有界重试使用新输出目录并复用同一快照缓存。更新原作时另建缓存和输出版本。离线模式缺任何必需页面 / 图片就失败，不能用错误占位页或其他作品正文补齐。
