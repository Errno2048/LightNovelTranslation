# Python 库使用说明

执行任务优先查随包[操作手册](../skills/lightnovel-translation/references/workflows.md)和[诊断与恢复](../skills/lightnovel-translation/references/diagnostics.md)，再查 API；无需从内部源码寻找调用配方。新增 `ReferenceConfig`、局部参考准备、实际参考覆盖报告、agent 修订和有界额度扩展均在这些页面说明。

内部运行机制见[工作原理手册目录](../skills/lightnovel-translation/references/manual/index.md)。它按配置、导入、索引与参考、续译、翻译审校、增量知识、压缩恢复的顺序组织，每个模块各一页并附源码位置；按当前问题选读即可。

安装和开发安装：

```console
python -m pip install .
python -m pip install -e ".[dev,narrative]"
```

分发给其他环境时可先 `python -m build`，再安装 `dist/lightnovel_translation-0.4.0-py3-none-any.whl`。安装名为 `lightnovel-translation`，导入名为 `lightnovel_translation`。底层引擎入口为 `translation`，CLI 为 `novel-translate`。

## 私有配置与 agent skill

仅需获取原文时，先用可选的 `lightnovel_translation.syosetu` 模块；不加载翻译凭据、不调用模型。安装 `python -m pip install ".[syosetu]"`，参阅[采集 API 与恢复](../skills/syosetu-fetch/references/acquisition.md)。中文文件先读取有界头部与作者信息，作者全名优先；标题关键词和外部网页只提供待核验候选。`download_work()` 返回清洗过的 EPUB，可直接 `import_book`。独立技能导出为 `python -m lightnovel_translation skill export <destination> --name syosetu-fetch`；不传名称保持原有翻译技能。

```console
python -m lightnovel_translation config init
python -m lightnovel_translation config path
```

初始化只创建空模板，已有文件不会被覆盖或自动迁移。默认文件为 `~/.lightnovel_translation/config.toml`；也可设置 `LIGHTNOVEL_TRANSLATION_HOME` 指向另一个私有目录，或对命令指定 `--directory`。新建文件在 POSIX 下限制为 0600，目录为 0700；Windows 下限制为当前账户与 SYSTEM 可访问。

请用户在本机编辑该文件，填写 `[model]` 下的 `model`、`base_url`、`api_key`，并按实际端点设置 `[capacity]` 与各角色输出预算。模板见 [config.example.toml](../skills/lightnovel-translation/assets/config.example.toml)，内含占位端点，未填写完成前无法发起模型请求。不要向 agent 提供配置内容，不要把含 key 的文件复制进仓库。库在进程内加载密钥；agent 仅运行：

```console
python -m lightnovel_translation config check
```

返回路径、存在性、语法与就绪状态、缺失字段名，不输出 key 或原始解析错误。就绪时退出码为 0，否则为 1。该检查不访问远端，不保证网络或额度可用。

`TranslationProject(workspace)` 在默认文件存在时自动使用它；显式 `config=TranslationConfig()` 可跳过用户配置执行本地操作；`config="custom.toml"` 则优先使用指定文件。可用 `TranslationConfig.from_user_config()` 显式加载默认文件。文件内非空 key 优先；为空时读取 `api_key_env`（默认 `LIGHTNOVEL_TRANSLATION_API_KEY`）。无默认文件也可导入、检查与规划。

安装包附带 [SKILL.md](../skills/lightnovel-translation/SKILL.md) 与 API / 人工修复参考文档。导出到 agent 的技能目录：

```console
python -m lightnovel_translation skill export ~/.codex/skills/lightnovel-translation
```

设置了 `CODEX_HOME` 时改用其下的 `skills/lightnovel-translation`；其他 agent 使用各自支持的技能目录。相同文件可重复导出，存在定制文件时会报冲突并保留原文件。技能要求 agent 不读取凭据文件，按实际来源块修正章节结构；不能通过补造正文或删除错误标记强行通过。

Claude Code 的工作区技能可导出到 `<workspace>/.claude/skills/lightnovel-translation`，再使用 `/lightnovel-translation`。库的用户配置与 CLI 自身的模型、端点和认证配置分别加载，初始化库配置不会自动修改 CLI。向 CLI 传递凭据时只使用其支持的私有配置或子进程环境，不把 key 放在 prompt 或命令行参数里。

本机网关若出现配置就绪但 HTTP 失败的情况，可检查是否需要在 `[model]` 中设置 `trust_env = false` 直连；临时实验也可在内存中 replace 模型配置，不改用户文件。

## 最简连续翻译

先完成上述本地配置。公共 API 使用用户配置文件，不要求配置 embedding。

```python
from lightnovel_translation import TranslationProject

with TranslationProject("work/my-novel") as project:
    source = project.import_book("novel.epub", edition_id="web-ja", language="ja")
    result = project.translate(
        source,
        name="opening",
        chapters=source.chapters()[:5],
    )
    print(result.to_dict())
```

`result.artifact_dir` 下有 `translation.txt`、`chapters.jsonl` 和 `status.json`。`complete` 表示指定窗口已完成工程闭环，`blocked` 需检查结构化 error 与保存的草稿。译文保留实验工作稿标签，不代表人工审定。

## 高级配置与模型替换

```python
import os
from lightnovel_translation import (
    TranslationConfig, ModelConfig, ModelCapacity, Limits, RoleBudget,
)

config = TranslationConfig(
    model=ModelConfig(
        model="your-model",
        base_url="http://127.0.0.1:8000/v1",
        api_key=os.environ["LIGHTNOVEL_TRANSLATION_API_KEY"],
    ),
    capacity=ModelCapacity(131072, 16384, token_inflation_factor=1.05),
    roles={"translator": RoleBudget(8192)},
    limits=Limits(max_requests=100),
    sequence={"n_chapter": 3, "translation_tools": True},
)
```

把 config 传给 Project 即可。不同模型用新 job 名；旧 job 恢复时保留原模型和冻结配置。容量由调用者声明，输出预算与角色配额必须在容量内。index 和每个 job 分别累计预算，不会在 reopen 时重置账本。

## 计划、暂停、恢复与完整 I/O

```python
with TranslationProject("work/my-novel") as project:
    source = project.edition("web-ja")
    with project.create_job("window", source=source, chapters=source.chapters()[400:450]) as job:
        plan = job.plan_next()                 # 不调用模型
        print(plan["context"]["messages"])    # 首章完整 prompt
        print(plan["context"]["budget"])
        result = job.run(max_chapters=2, on_event=print)
        if result.ok and result.state != "complete":
            job.checkpoint()                  # 显式测试压缩，可能调用模型
        job.export(include_io=True)           # 精确请求、原始响应、审校、最终稿

# 也可以在之后的另一个进程中恢复。
with TranslationProject("work/my-novel") as project:
    with project.open_job("window") as job:
        result = job.resume(materialize=True) # 同步构建已提交范围的增量索引
        print(result.to_dict())
```

切片是 0 基叙事章位置，不等于标题的“400章”；应先打印 `source.chapters()` 确定需要的 ID。`max_chapters` 是本次最多新提交的章数。关闭 Project 会关闭它创建的任务和 HTTP 客户端。每个任务只使用一个写入者。

## 已确认的一一 / 一对多译文映射

```python
source = project.edition("web-ja")
reference = project.import_book("existing-zh.epub", edition_id="parallel-zh", language="zh")
ja, zh = source.chapters(), reference.chapters()
pairs = {ja[0].id: [zh[0].id], ja[1].id: [zh[1].id]}
job = project.create_job(
    "parallel-window", source=source, chapters=ja[2:5],
    mode="parallel", references=[reference], parallel_pairs=pairs,
)
```

pairs 是调用者已确认的历史映射，不按列表位置自动推测。这里只提供目标之前的历史译文，不能传入本次目标的参考答案。创建后使用 `with job:` 或最终 `job.close()`。

## 跨版本：独立索引与关联

需要先判断从哪里接续时，可使用参考集排序与定位接口。参考可以只含连续局部卷，例如第 7–9 卷；这不会将本次任务改为补译前 1–6 卷：

```python
from lightnovel_translation import plan_reference_set

refs = plan_reference_set(
    ["第9卷.epub", "第7卷.epub", "第8卷.epub"],
    edition_id="print-07-09", language="zh",
)
print(refs.to_dict())                       # 本地顺序证据、冲突、缺卷与特别卷
reference = project.import_edition(refs.to_input_spec())
index = project.index([source, reference])
plan = index.plan_continuation(source=source, references=[reference], reference_set=refs)
result = index.locate_continuation(plan, max_new_nodes=20, max_requests=30)
print(result["status"], result["recommended_start"], result["artifact_dir"])

if result["status"] == "suggested":
    start_id = result["recommended_start"]["chapter_id"]
    chapters = source.chapters()
    start = next(i for i, c in enumerate(chapters) if c.id == start_id)
    # 定位与翻译是两个显式操作；先检查证据和版本差异，再创建有限连续窗口。
    job = project.create_job("continuation", source=source, references=[reference],
                             index=index, mode="cross_edition", chapters=chapters[start:start+2])
```

排序仅检查给定文件，保留卷内 EPUB/TXT 阅读顺序。卷号不明、同卷不同副本或卷序证据冲突时，`to_input_spec()` 拒绝导入；可用 `overrides={"文件名.epub": {"volume_ordinal": 9}}` 显式修正。Ex 默认不推进主线终点；无法自动识别的合本卷界、文件内部乱序和复杂独立章文件需要先使用输入修复工具。

当前定位支持一个已整理的中文参考版本，先由末端 L2 事件与全书目录召回，再读取候选正文核验。摘要构建、查证、格式重试均纳入同一定位持久预算。结果保留 `search_complete=False`，没有宣称穷尽全书对应。`suggested` 与 `auto_eligible` 都不代表人工验收；`diverged/ambiguous/insufficient_evidence` 不自动开始翻译。`artifact_dir` 下保存计划、局部摘要、结果及 `calls/` 内完整请求响应。

定位关联不自动成为译名或逐句对应；若需要跨版语言参考，仍需下述显式 `associate/match_entities` 流程。参考译文存在缺卷时，续译定位以最后提供卷的尾部剧情证据为依据；排序不能仅依赖文件系统枚举顺序。

```python
source = project.edition("web-ja")
reference = project.import_book(["vol1.epub", "vol2.epub"], edition_id="print-zh", language="zh")
index = project.index([source, reference])             # 本地计划
ja_nodes = index.nodes(edition="web-ja")
zh_nodes = index.nodes(edition="print-zh")

# 调用者根据标题/阅读阶段明确候选，避免无意触发全书处理。
selected = [ja_nodes[0]["node_id"], zh_nodes[0]["node_id"]]
built = index.build(selected, workers=2)
assert built["complete"], built
association = index.associate([selected[0]], [selected[1]])
assert association["status"] == "complete", association

with project.create_job(
    "cross-window", source=source, chapters=source.chapters()[:3],
    mode="cross_edition", references=[reference], index=index,
    associations=[association],
) as job:
    print(job.plan_next()["context"]["messages"])
    result = job.run()
```

可继续对指定 L1 下的全部子节点 `build(..., include_parents=True)`，再用 `match_entities` 得到实体映射，创建任务时通过 `entity_matches=[artifact]` 注入。跨版本关联是候选证据，不能替换 Web 事实；没有关联材料时不会自动猜测该选哪份中文。

## 不规则输入与 agent 工具

`import_book` 支持明确顺序的多个 EPUB/TXT 文件、`paragraph_policy` 和 `encoding`。复杂输入可构造 `InputSpec` 调用 `import_edition`，给每个来源设置 format / volume_hint / encoding_override。

若捕获 `TranslationError` 且 `code == 'import_needs_resolution'`，details 中带导入目录与 issues。通过 `lightnovel_translation.ingestion` 的 `load_structure`、`read_source_range`、`apply_structure_patch` 等工具修正结构，再验证并重试导入。保留原文，不允许 patch 改写正文。文件无法解码等前置输入错误仍以 ValueError / OSError 报告。

`TranslationError.to_dict()`、`RunResult.to_dict()`、`ChapterInfo.to_dict()` 是 agent / skill 可用的结构化返回值。修复示例、完整 patch 字段及分支规则见 [输入修复指南](../skills/lightnovel-translation/references/input-repair.md)。只读查询不会为了补资料自动生成内容。

## 无凭据离线示例

```console
python examples/library_offline.py artifacts/translation/library-offline
```

该示例仅使用公开 API 和固定的 provider，完成五章翻译、暂停、压缩、恢复、增量索引和 I/O 导出。自定义 provider 只需实现 `generate(GenerationRequest) -> GenerationResult`，传给 Project 并提供稳定的 `provider_id`。示例演示协议和工程行为，不评估翻译质量。
