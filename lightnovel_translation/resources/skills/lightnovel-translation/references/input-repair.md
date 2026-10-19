# 输入检查与人工修复

本库原生处理 EPUB/TXT。区分“机械校验通过”和“章节划分符合书籍结构”：无编号标题、一个文件包含全书、资源边界为续页时，自动结果仍可能需要人工修正。

## 先定位问题层级

| 现象 | 处理 |
|---|---|
| 一章包含多个标题 / 被续页拆开 | 检查完整块，使用 set_boundary / remove_boundary |
| 正文中的标题未被识别 | 边界块存在时使用 title_ref；不凭作品记忆生成标题 |
| 技能列表 / 人物表被视为叙事章 | 仅在有明确结构依据时 classify_range 为 reference；故事内的技能列表仍是正文 |
| TXT 硬换行 / 空行导致段落不当 | 选择 line / blank_line / wrapped，先检查对白、技能列表和分隔符 |
| 编码歧义、乱码、解码错误 | 确认编码后以新 ID 和 encoding_override 重新提取，结构 patch 不能修复字节解码 |
| 目录与 spine 顺序冲突、抽取覆盖错误、源快照被改 | 不允许结构 patch；从原书重新检查提取，或生成有转换记录的派生文件 |
| 同一个源块内部包含多个逻辑章 / 标题内嵌正文 | 当前 patch 只支持整块边界；先做可审查的派生 TXT/EPUB，再新建导入 |
| PDF、扫描图片、HTML 压缩包等不支持的格式 | 用本地可用转换工具生成派生 EPUB/TXT，记录来源与转换；识别不清的 OCR 需核对，不能补写 |

原文件、快照、提取结果、结构历史和已导出 corpus 不可手工改写。不要删除 validation issues、伪造 hash 或用通配规则丢掉“看起来多余”的段落。

## 无模型检查

```python
from pathlib import Path
from lightnovel_translation.ingestion import (
    inspect_input, extract_sources, propose_structure, load_extraction,
    load_structure, read_source_range, validate_import,
)

spec = {
    "edition_id": "web-ja-repair", "language": "ja", "edition_label": "原文修复分支",
    "sources": [{"source_id": "file-0", "path": str(Path("book.txt").resolve()),
                 "input_order": 0, "format": "txt"}],
    "paragraph_policy": "line",
}
folder = Path("work/repair/extraction")
inspection = inspect_input(spec)
extract_sources(spec, folder)
structure = propose_structure(folder)
validation = validate_import(folder)
extraction = load_extraction(folder)
```

`load_extraction` 的 blocks 带 `block_id`、`raw_text`、source/resource 和 locator。`structure['entries']` 带 entry_id、block_ids、raw_title、title_block_id、kind、boundary_kind。只读取相关窗口，避免把整本正文塞入 agent 上下文：

```python
window = read_source_range(folder, [suspect_block_id], context=3, max_blocks=64, max_chars=20000)
```

必须先根据实际窗口确认阅读顺序和边界。`suspect_block_id` 必须来自这次提取，不是猜测的 ID。

## 可用结构补丁

`apply_structure_patch` 要求 extraction 校验通过，而且 base structure 本身满足覆盖和顺序约束。encoding_ambiguous、损坏来源、非法基结构等错误不能通过 patch 绕过。

```python
from lightnovel_translation.ingestion import apply_structure_patch, export_corpus

base = load_structure(folder)
patch = {
    "schema": "import-structure-patch-v1",
    "base_structure_revision": base["fingerprint"],
    "decision_by": "agent",
    "operations": [{
        "op": "set_boundary",
        "at": {"block_id": actual_heading_block_id},
        "title_ref": {"block_id": actual_heading_block_id},
        "reason": "该块是已核对的独立章节标题，后续为本章正文",
        "evidence_refs": [actual_heading_block_id],
    }],
}
repaired = apply_structure_patch(folder, patch)
validation = validate_import(folder)
assert validation["ok"], validation["issues"]
out = folder.parent / ("corpus-" + repaired["fingerprint"][:12])
export_corpus(folder, out)
```

每个 patch 1–100 个 operations；每个 operation 都需 reason。可选 evidence_refs 记录实际块证据。可用操作只有：

- `set_boundary`：at={block_id}；可给 title_ref={block_id}；在所属条目内切出新章。初始块也可用该操作确认为有标题章节。
- `remove_boundary`：at 必须是非首条目的起始块，将它完整合并到前条目；合并不会自动把原“标题”文字删除，需检查它是否其实属于正文。
- `select_title`：at 选条目内块，title_ref 选该条目内非空完整块，只改变标题来源。未决边界需要同时确认时使用 set_boundary。
- `classify_range`：entry_ids 为连续范围；kind 仅可选 narrative、narrative_side、reference、toc、preface、afterword、volume_intro、translator_note。注意分类会影响下游是否翻译。
- `set_paragraph_policy`：连续 entry_ids、policy=line|blank_line|wrapped，仅适用于 TXT。
- `set_volume`：连续 entry_ids、volume_label，添加显式卷提示。

只有章节边界 / 标题 / 分类 / 卷 / TXT 段落策略可以改变，不能写 text/raw_text 或任意 JSON 字段。每次 patch 后重新 load_structure；边界变化会重算 entry IDs，不要在下一次 patch 复用旧 ID。stale revision 表示资料已变，重新读取并评估，不要直接替换 revision 字段重放旧决策。

`export_corpus` 不覆盖已有不同导出。使用新的输出目录；尚未注册的修复版本可 `project.attach_corpus(out, edition_id=spec['edition_id'])`。若同 ID 先前已注册或已有冻结任务，使用新 edition_id **重新提取**生成新分支，再建新 job，不能更换老 registry 指纹。

## 解码与派生输入

解码必须以文件声明、BOM 或用户提供的来源信息为依据。多种 codec 都能 decode 不代表能自动选对。确认后更新 SourceSpec 的 encoding_override（如 cp932、gb18030），使用新的 edition_id 和输出目录重新提取；旧快照保留以便比较。

外部转换保留原文件并生成新文件，附 `conversion.json`：原路径与 SHA256、输出路径与 SHA256、工具/版本、所选阅读顺序和布局变更、原资源/页/行到输出章节的可追溯映射、未确定的位置。不要把密码或用户配置写入转换记录。原生 provenance 此时绑定派生文件，原书关系由 sidecar 补充，不能宣称与直接导入有同等来源粒度。

文本布局转换尽量只改变可核对的分隔与标签；对照转换前后有效文本顺序、字符内容、重复与缺失，以及章节起止。不要通过笼统删除空白来掩盖意外拼接、句内空格或段落丢失。插图和技能列表要明确保留/分类，不能因不易翻译直接删除。

## 修复后的重新接入

验证结果通过后，再检查可疑边界前后正文、标题及其 paragraph_count；抽查开头、结尾和各输入文件连接处。机械验证并不证明剧情顺序正确。

将新 corpus 注册为 Edition，先 plan_next 验证完整目标和上下文预算；在用户授权范围内运行少量章节，确认审校、提交和恢复可执行后继续。用最终导入指纹、patch ID、派生记录和运行状态报告修复，保留无法核实的缺页或顺序问题。
