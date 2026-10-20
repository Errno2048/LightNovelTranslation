# 调用与恢复

本模块不依赖翻译工作区、模型或密钥。检索使用官方 Narou 作品 API；正文解析与 EPUB 输出使用 FanFicFare。公共 API 在 `lightnovel_translation.syosetu`，可选依赖安装为 `lightnovel-translation[syosetu]`，固定已测试的正式版 FanFicFare 4.62.0。

## 下载并发与配置

在 `~/.lightnovel_translation/config.toml` 添加以下节即可复用；不存在此节时使用同样的默认值，不需要填写模型或 API key：

```toml
[syosetu]
concurrency = 2
request_interval = 1.0
```

`concurrency` 支持 1–8，1 为串行；这是库的配置范围，不是站点承诺的允许上限。`request_interval` 是所有下载线程共享的最小请求启动间隔，单位秒，缓存命中不等待。2 个线程与每秒最多启动一次请求同时生效，并发主要用于覆盖网络等待。官方元信息 API 检索仍独立保留至少一秒查询间隔。

优先级为 API / CLI 显式参数 → 指定配置文件中的 `[syosetu]` → 默认用户配置中的 `[syosetu]` → 内置默认值。指定文件替代默认文件；没有指定时才读取默认用户文件。模型节可以不完整，采集不会初始化翻译模型、读取 API key 环境变量或发起模型请求。不要让 agent 输出含密钥的完整文件；可只提交下面的配置片段给用户添加。已有配置不会自动覆盖。

```console
python -m lightnovel_translation syosetu download n7975cr work/source-01 --first 1 --last 20 --concurrency 2 --interval 1 --proxy http://127.0.0.1:7890
python -m lightnovel_translation syosetu download n7975cr work/source-02 --first 1 --last 20 --config source-config.toml
```

Python 同样支持 `download_work(..., concurrency=2, request_interval=1.0, config="source-config.toml")`；也可传入 `config=SyosetuConfig(concurrency=2)`（从 `lightnovel_translation` 导入），完全跳过用户文件。设置后由库校验，非法参数在网络访问前拒绝。

实现先按范围并发预取正文，每个线程独立持有 FanFicFare 配置与 HTTP session；响应索引写入串行化。正文解析、插图处理和 EPUB 打包仍按原目录顺序执行，避免线程改变章序或共享解析状态。图片按需串行获取，也受同一个请求间隔与退避约束。

收到 429 / 503 时，有效并发减半（最低 1），所有线程共享冷却时间，至少 5 秒或服务端指定的 Retry-After，按较长者等待。未提供 Retry-After 时按 5/10/20 秒退避。单个请求最多额外重试 `retries` 次（默认 1，范围 0–3）；底层自动重试关闭，避免叠加。等待超过 60 秒时直接返回可重试错误，不提前重发；403 等非暂时性错误不重试。已在途请求可能完成，后续排队请求停止。恢复用新输出目录和原缓存，长 Retry-After 应等待其到期后再恢复。

返回结果与成功 / 失败 manifest 中的 `download_stats` 记录配置并发、实际最大在途请求数、最终有效并发、网络请求数、缓存命中数、HTTP 状态统计、重试和退避事件。统计涵盖下载器的页面与图片请求，不包含独立的 Narou API 查询或重定向内部跳转。离线重建不预取、不访问网络。

## 中文反查

```python
from lightnovel_translation.syosetu import inspect_reference, NarouCatalog

head = inspect_reference("中文第一卷.epub")  # 本地，无网络，默认读取最多 8000 字符头部
print(head["titles"], head["author_candidates"], head["author_evidence"])
# 阅读 head["head"]，确定确实是小说作者，而非译者或插画家。
with NarouCatalog(proxy="http://127.0.0.1:7890", cache_dir="work/catalog") as catalog:
    result = catalog.reverse_lookup(head, title_keywords=["蜘蛛"])
    # 作者全名优先，原字形 + 简转繁；无命中才查询传入的标题关键词。
    # 不能自动选第一个：结合 matched_keywords、synopsis 和头部检查。
    print(result["strategy"], result["candidates"])
    # 作者候选有多部但均不匹配时，仍可主动进行关键词检索。
    keyword_candidates = catalog.keywords(["蜘蛛"])
    # 网络查明的日文作者可作为 author_aliases=["日文姓名"] 再次检索。
```

作者姓名仅做 NFKC / 空格规范化后的全名比较。OpenCC 变体仅增加检索词，不能处理所有日本新旧字体、假名音译或笔名。`author()` 同时返回 `exact_matches` 与站点原始候选，近似姓名不能冒充全名命中。作者可能有多个作品；全文一致的名字也不能确认同一人。

`keywords([a,b])` 分别查询标题并合并，按命中关键词数排序；不是两个词同时存在的硬性 AND。每词默认向 API 请求总积分顺序（`order="hyoka"`），避免新作淹没较早发表的原作；积分只影响召回，不能证明身份。可显式改为 `order="new"` / `"old"` / `"weeklypoint"` / `"monthlypoint"`。每个词 / 作者查询最多返回 100 条，`total` 和 `truncated` 保留覆盖范围。未命中需要更换有辨识力的词或进入外部检索，不能宣称全站不存在。`reverse_lookup` 不调用付费模型或联网搜索工具，后一步由 agent 按技能执行。

CLI 的 `--output` 将完整 JSON 写入新文件，只打印最多 10 个简要候选；省略它会打印完整结果。文件已存在时拒绝覆盖。

```console
python -m lightnovel_translation syosetu inspect-reference "中文第一卷.epub" --output work/head.json
python -m lightnovel_translation syosetu lookup "中文第一卷.epub" --keywords 蜘蛛 --proxy http://127.0.0.1:7890 --cache-dir work/catalog --output work/lookup.json
python -m lightnovel_translation syosetu search --author 馬場翁 --proxy http://127.0.0.1:7890
python -m lightnovel_translation syosetu search --keywords 蜘蛛 --proxy http://127.0.0.1:7890 --output work/keywords.json
python -m lightnovel_translation syosetu metadata n7975cr --proxy http://127.0.0.1:7890
```

TXT 支持 `--encoding` / `inspect_reference(..., encoding=...)`。预览是有界前缀；不能替代输入标准化。编码或 EPUB 头部结构不明确时，先用现有输入修复工具处理派生文件，不改原书。

## 下载与导入

```python
from lightnovel_translation import TranslationConfig, TranslationProject
from lightnovel_translation.syosetu import download_work

result = download_work(
    "n7975cr", "work/acquisition-01", first_chapter=1, last_chapter=2,
    proxy="http://127.0.0.1:7890", timeout=20, retries=1,
    selection_evidence={"reference": "中文第一卷.epub", "reason": "作者全名、蜘蛛关键词及转生简介相符"},
)
with TranslationProject("work/project", TranslationConfig()) as project:
    edition = project.import_book(result["epub_path"], edition_id="web-ja", language="ja")
    assert len(edition.chapters()) == result["chapter_count"]
    print(project.validate_inputs())
```

```console
python -m lightnovel_translation syosetu download n7975cr work/acquisition-01 --first 1 --last 2 --proxy http://127.0.0.1:7890
```

不传 `last_chapter` / `--last` 表示从首话到当前目录结尾；这可能下载全本。插图和前后记默认保留，分别用 `include_images=False` / `--no-images`、`include_author_notes=False` / `--no-author-notes` 关闭。保留原生 ruby，不自动生成额外注音。首次范围试验也会读取全书目录以确定序号。

输出目录包含：

| 文件 | 用途 |
|---|---|
| `source.epub` | 清洗后的 EPUB 3，供现有 `import_book` 导入 |
| `raw.epub` | FanFicFare 原始输出，可能含标题标记 U+200B |
| `manifest.json` | 状态、作品身份、范围、每话标题 / 篇章 / canonical URL / 修订标识、输出哈希、清洗计数 |
| `cache/work.json` | 当次作品元信息 |
| `cache/responses/responses.json` + `.bin` | GET 响应字节、URL、哈希；不保存 cookie、请求头或 pickle |

清洗删除 Unicode Cf 和 U+034F（包括编码成字符实体的控制字符），保留原生注音和有语义的组合符。下载器会要求 FanFicFare 保留原始图片字节，避免其 Pillow 转 JPEG 路径误拒绝带透明通道的合法 PNG；`validate_source_epub()` 检查 CRC、话数、空正文、资源存在及零宽字符；不等于完整 EPUBCheck 或全本语义验收。FanFicFare 的完结状态使用官方 API `end` 信息校正。

## 缓存、更新和失败

检索缓存设置有效期上限为 14 天，过期重新请求；每个 `NarouCatalog` 实例按至少一秒间隔查询。请求超时默认 20 秒，最多额外重试一次，可设 `timeout` / `retries`（0–3）。这是每次请求 / 网络操作的超时与重试边界，不是整本下载总时长上限。

正文响应缓存属于不可变采集快照。复用同一缓存不会刷新既有页面；更新小说应使用新目录和新缓存。单写入者使用一个快照，不能并发写相同缓存。失败记录为 `manifest.status=failed`，不会得到可交付的成功状态；FanFicFare 内部吞掉的图片请求失败也会被包装层阻断。

严格离线重建用另一个输出目录，并指向已有缓存：

```python
rebuilt = download_work("n7975cr", "work/rebuild-01", first_chapter=1, last_chapter=2,
                        cache_dir="work/acquisition-01/cache", offline=True)
```

离线必须保留原范围所需的作品元信息、目录、正文和图片。缺少响应时返回 `source_cache_miss`；哈希不符是 `source_cache_corrupt`。打包时间可能变化，离线重建不保证 EPUB 整包字节完全相同，应比较每话正文、ruby、资源和 manifest 的来源身份。

其他错误：`source_search_failed`（API / 格式 / 网络）、`source_not_found`、`source_dependency_missing`、`source_output_exists`、`source_download_failed`、`source_chapter_error`、`source_request_failed`、`source_rate_limited`（429，检查 Retry-After 与次数上限）。异常公开为 `TranslationError`，用 `.to_dict()` 查看结构化信息；失败 manifest 也保存脱敏错误类型及状态。CLI 错误返回非零退出码。保留失败目录；重试时使用新输出目录和原缓存。不要删除来源标记强行通过。

## Skill 导出

```console
python -m lightnovel_translation skill export ~/.codex/skills/syosetu-fetch --name syosetu-fetch
```

Claude Code 可导出至 `.claude/skills/syosetu-fetch`。旧命令不传 `--name` 仍导出 `lightnovel-translation`。文件冲突保留本地定制，选择新目录比较后再合并。

源码位置：`src/lightnovel_translation/syosetu.py` 的 `inspect_reference`、`NarouCatalog`、`download_work`、`validate_source_epub`；`_source_requests.py` 的共享并发 / 速率与退避控制；`acquisition_config.py` 的 `SyosetuConfig`；`syosetu_cli.py`；`skill.py` 和 `_resources.py`。入口流程优先查本页，无需阅读下载器或翻译引擎内部代码。
