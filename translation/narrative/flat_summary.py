"""Experimental flat summary wire format; never repairs or promotes knowledge.

Records have start markers only. Values continued on indented lines are literal;
quotes and braces need no escaping. Errors retain the other readable records but
always prevent the candidate from being considered valid.
"""
from dataclasses import dataclass
import re

from .bilingual import SYSTEM, summary_messages

FORMAT = """只输出以下平面记录，不输出JSON、Markdown围栏或说明。每条记录以@@开头，无需结束括号。
字段名严格使用下列英文，每个字段单独一行；长值可换行，续行须缩进。source_form和evidence可以重复。
引文字段格式为 定位 | 逐字引文；按第一个空格竖线空格分隔，无需转义引号或括号。
OVERVIEW恰好一条；ENTITY最多配置数量；RENDERING引用已声明ENTITY的ID；FIXED最多配置数量。
RENDERING/FIXED必须有完整双语证据组，多个证据组通过重复pairing_id、ja、zh三行表示。
没有的可选记录不输出，不写空占位记录。最后输出@@END。字段示例的值须替换成资料中的实际内容：
@@OVERVIEW
text: 整体摘要
evidence: ja:main:1@0 | 日文逐字片段
@@ENTITY e1
kind: person
source_form: 日文形式
description: 作品中的人物描述或用途
description_evidence: ja:main:1@0 | 日文逐字片段
@@RENDERING e1
source_form: 日文形式
target_form: 已有中文用法
usage_scope: 本块具体语境
pairing_id: pair:main:1
ja: ja:main:1@0 | 含日文形式的片段
zh: zh:main:1:0@0 | 含中文用法的片段
@@FIXED f1
source_form: 作品特有表达
target_form: 已有中文用法
usage_scope: 具体语境
pairing_id: pair:main:1
ja: ja:main:1@0 | 日文逐字片段
zh: zh:main:1:0@0 | 中文逐字片段
@@CONFLICT
text: 有出处的歧义
@@UNRESOLVED
text: 未知事项
@@COVERAGE
text: 实际未处理的重要内容
@@END"""


def messages(pairs, config, output_format):
    """Matched prompts share all semantic instructions and the complete data."""
    if output_format not in {"json", "flat"}:
        raise ValueError("unknown format")
    common = SYSTEM.split("只输出严格JSON", 1)[0]
    common += ("\n本任务只生成一个节点，未指定详细区间，因此details为空；这不是覆盖缺口。"
               "正常粗化与未穷举不写coverage_notes；确有未读或遗漏的重要情节才记录缺口。"
               "不同实体必须分开记录，人物与其组织不能合并；个体生物与物种分开。"
               "时序描述明确主体、时间方向、过程及信息来源，尤其区分穿越发生的年龄与后来成长。")
    representation = ("只输出严格JSON" + SYSTEM.split("只输出严格JSON", 1)[1]) if output_format == "json" else FORMAT
    system = common + "\n" + representation
    body = summary_messages(pairs, [], config)[1]["content"]
    tail = (f"\n【正文结束；执行任务】范围{pairs[0]['source']['match_key']}至{pairs[-1]['source']['match_key']}，"
            f"共{len(pairs)}章完整日中对照。请概括整个范围，整体摘要最多{config.overview_max_chars}字，"
            "不设下限。以下重申同一任务与输出格式：\n" + system)
    return [{"role": "system", "content": system}, {"role": "user", "content": body + tail}]


@dataclass
class FlatResult:
    content: dict
    errors: list[str]
    records: list[dict]


def parse_flat(text):
    records, errors = [], []
    current, last, ended = None, None, False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("@@"):
            if ended:
                errors.append(f"line {number}: record after END")
            head = line[2:].strip().split(None, 1)
            current = {"kind": head[0] if head else "", "id": head[1] if len(head) == 2 else "", "fields": []}
            records.append(current)
            ended = current["kind"] == "END" or ended
            last = None
        elif line[:1].isspace() and current is not None and last is not None:
            current["fields"][last][1] += "\n" + line.lstrip()
        else:
            match = re.fullmatch(r"([a-z_]+):[ \t]*(.*)", line)
            if current is None or not match:
                errors.append(f"line {number}: expected record or named field")
                continue
            current["fields"].append([match[1], match[2]])
            last = len(current["fields"])-1
    if not ended:
        errors.append("missing END marker")
    value = {"overview": {"text": "", "evidence": []}, "details": [],
             "translation_metadata": {"entities": [], "fixed_expressions": [], "conflicts": []},
             "unresolved": [], "coverage_notes": []}
    entities, deferred, seen_records = {}, [], set()

    def fields(r, allowed, required=()):
        fs = {}
        for key, val in r["fields"]:
            if key not in allowed:
                errors.append(f"{r['kind']} {r['id']}: unknown field {key}")
            fs.setdefault(key, []).append(val)
        for key in required:
            if key not in fs:
                errors.append(f"{r['kind']} {r['id']}: missing {key}")
        return fs

    def one(fs, key, default=""):
        rows = fs.get(key, [])
        if len(rows) > 1:
            errors.append(f"duplicate scalar field {key}")
        return rows[0] if rows else default

    def cite(raw):
        ref, sep, quote = raw.partition(" | ")
        if not sep or not ref.strip() or not quote:
            errors.append("invalid citation separator or empty reference/quote")
        return {"ref": ref.strip(), "quote": quote}

    for r in records:
        kind, ident = r["kind"], r["id"]
        if kind not in {"OVERVIEW", "ENTITY", "RENDERING", "FIXED", "CONFLICT", "UNRESOLVED", "COVERAGE", "END"}:
            errors.append("unknown record " + kind)
            continue
        if kind in {"ENTITY", "RENDERING", "FIXED"} and not ident:
            errors.append("missing record id: " + kind)
        if kind not in {"ENTITY", "RENDERING", "FIXED"} and ident:
            errors.append("unexpected record id: " + kind)
        if kind in {"OVERVIEW", "ENTITY", "FIXED", "END"}:
            key = (kind, ident)
            if key in seen_records:
                errors.append("duplicate record: " + str(key))
            seen_records.add(key)
        if kind == "OVERVIEW":
            fs = fields(r, {"text", "evidence"}, ("text", "evidence"))
            value["overview"] = {"text": one(fs, "text"), "evidence": [cite(v) for v in fs.get("evidence", [])]}
        elif kind == "ENTITY":
            fs = fields(r, {"kind", "source_form", "description", "description_evidence"}, ("kind", "source_form", "description"))
            e = {"local_entity_id": ident, "kind": one(fs, "kind"), "source_forms": fs.get("source_form", []),
                 "description": {"text": one(fs, "description"), "evidence": [cite(v) for v in fs.get("description_evidence", [])]}, "renderings": []}
            if ident not in entities:
                entities[ident] = e
            value["translation_metadata"]["entities"].append(e)
        elif kind in {"RENDERING", "FIXED"}:
            fs = fields(r, {"source_form", "target_form", "usage_scope", "pairing_id", "ja", "zh"},
                        ("source_form", "target_form", "usage_scope", "pairing_id", "ja", "zh"))
            row = {key: one(fs, key) for key in ("source_form", "target_form", "usage_scope")}
            row["evidence"] = []
            active = None
            for key, val in r["fields"]:
                if key == "pairing_id":
                    active = {"pairing_id": val}
                    row["evidence"].append(active)
                elif key in {"ja", "zh"}:
                    if active is None or key in active:
                        errors.append("orphan/duplicate bilingual citation: " + key)
                    else:
                        active[key] = cite(val)
            if any(set(e) != {"pairing_id", "ja", "zh"} for e in row["evidence"]):
                errors.append("incomplete bilingual evidence group")
            if kind == "FIXED":
                value["translation_metadata"]["fixed_expressions"].append(row)
            else:
                deferred.append((ident, row))
        elif kind != "END":
            fs = fields(r, {"text"}, ("text",))
            target = {"CONFLICT": value["translation_metadata"]["conflicts"],
                      "UNRESOLVED": value["unresolved"], "COVERAGE": value["coverage_notes"]}[kind]
            target.append(one(fs, "text"))
        else:
            fields(r, set())
    if ("OVERVIEW", "") not in seen_records:
        errors.append("missing OVERVIEW record")
    for ident, row in deferred:
        if ident not in entities:
            errors.append("orphan rendering for " + ident)
        else:
            entities[ident]["renderings"].append(row)
    return FlatResult(value, errors, records)


def serialize_flat(content):
    """Deterministic codec used for equivalence tests and offline fixtures."""
    lines = []

    def field(key, val):
        rows = val.split("\n")
        lines.append(key + ": " + rows[0])
        lines.extend("  " + row for row in rows[1:])

    def citation(key, e):
        field(key, e["ref"] + " | " + e["quote"])

    def rendering(kind, ident, row):
        lines.append("@@" + kind + " " + ident)
        for key in ("source_form", "target_form", "usage_scope"):
            field(key, row[key])
        for e in row["evidence"]:
            field("pairing_id", e["pairing_id"])
            citation("ja", e["ja"])
            citation("zh", e["zh"])

    lines.append("@@OVERVIEW")
    field("text", content["overview"]["text"])
    for e in content["overview"]["evidence"]:
        citation("evidence", e)
    for e in content["translation_metadata"]["entities"]:
        ident = e["local_entity_id"]
        lines.append("@@ENTITY " + ident)
        field("kind", e["kind"])
        for form in e["source_forms"]:
            field("source_form", form)
        field("description", e["description"]["text"])
        for cite in e["description"]["evidence"]:
            citation("description_evidence", cite)
        for row in e["renderings"]:
            rendering("RENDERING", ident, row)
    for i, row in enumerate(content["translation_metadata"]["fixed_expressions"]):
        rendering("FIXED", f"f{i+1}", row)
    for kind, texts in (("CONFLICT", content["translation_metadata"]["conflicts"]),
                        ("UNRESOLVED", content["unresolved"]), ("COVERAGE", content["coverage_notes"])):
        for text in texts:
            lines.append("@@" + kind)
            field("text", text)
    lines.append("@@END")
    return "\n".join(lines)
