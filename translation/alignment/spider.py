"""Version-aware corpus and candidate retrieval for the Spider case.

This module deliberately lives beside the existing alignment code.  The older
rules are useful for editions with a unique chapter key, while this corpus has
repeated volume-local numbers, reordered side stories and edition-specific
content.  The functions here therefore preserve native identities and return
review candidates; they never silently promote a candidate to an approved
translation pair.
"""
from __future__ import annotations

import html
import math
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ..corpus import epub_entries, extract_paragraphs
from ..storage import digest, file_digest, read_jsonl, write_json, write_jsonl

VERSION = "spider-corpus-v2"
ALIGNMENT_VERSION = "spider-window-candidates-v1"


@dataclass(frozen=True)
class SpiderDocument:
    edition_id: str
    language: str
    document_id: str
    path: Path
    volume: int | None = None
    extra: bool = False


def _natural_volume(path: Path) -> tuple[int, int, str]:
    if path.stem.endswith(" Ex"):
        return (1, 10_000, path.name)
    match = re.search(r" (\d+)$", path.stem)
    return (0, int(match[1]) if match else 9_999, path.name)


def spider_documents(root: Path) -> list[SpiderDocument]:
    """Return the explicit Spider input set in stable reading order."""
    ja = root / "out_syosetu" / "蜘蛛ですが、なにか？.epub"
    if not ja.exists():
        raise FileNotFoundError(ja)
    zh_paths = sorted((root / "out_linovelib").glob("转生成蜘蛛又怎样！ *.epub"), key=_natural_volume)
    if not zh_paths:
        raise FileNotFoundError("out_linovelib/转生成蜘蛛又怎样！ *.epub")
    result = [SpiderDocument("web-ja", "ja", "web-main", ja)]
    for path in zh_paths:
        if path.stem.endswith(" Ex"):
            result.append(SpiderDocument("print-zh", "zh", "print-zh-ex", path, extra=True))
        else:
            match = re.search(r" (\d+)$", path.stem)
            if not match:
                raise ValueError(f"cannot determine Chinese volume: {path.name}")
            volume = int(match[1])
            result.append(SpiderDocument("print-zh", "zh", f"print-zh-v{volume:02d}", path, volume=volume))
    return result


def normalize_title(title: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", html.unescape(title))).strip()


# The site masks part of some Chinese titles with Latin placeholders.  This
# detector is intentionally conservative: it only marks an uppercase N/Q next
# to CJK text.  It does not replace the source string.
_MASK_PATTERNS = (
    re.compile(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])([NQ])(?=[\u3400-\u4dbf\u4e00-\u9fff])"),
    re.compile(r"(?<![A-Za-z])([NQ])(?=[\u3400-\u4dbf\u4e00-\u9fff])"),
    re.compile(r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])([NQ])(?![A-Za-z])"),
)


def mask_spans(text: str, language: str) -> list[dict]:
    if language != "zh" or not text:
        return []
    spans: dict[tuple[int, int], dict] = {}
    for pattern in _MASK_PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(1)
            spans[(start, end)] = {
                "start": start,
                "end": end,
                "raw": text[start:end],
                "detector": "uppercase_placeholder_adjacent_to_cjk",
                "confidence": "suspected_site_mask",
            }
    return [spans[key] for key in sorted(spans)]


def _list_ratio(texts: list[str]) -> float:
    if not texts:
        return 0.0
    markers = ("LV", "Lv", "ＬＶ", "HP", "MP", "SP", "技能", "スキル", "经验值", "熟练度")
    hits = sum(any(marker in text for marker in markers) for text in texts)
    return hits / len(texts)


def classify_entry(title: str, body: list[str], language: str, *, extra: bool = False, book_title: str = "") -> tuple[str, list[str]]:
    """Classify without dropping unknown entries.

    Labels are hints for retrieval and review.  A side-story label does not
    imply a character identity or a temporal position.
    """
    normalized = normalize_title(title)
    reasons: list[str] = []
    if not normalized:
        return "empty_or_layout", ["missing_title"]
    if book_title:
        normalized_book = normalize_title(book_title)
        if normalized == normalized_book or re.fullmatch(re.escape(normalized_book) + r"\s+(?:\d+|Ex)", normalized):
            return "volume_intro", ["book_title_page"]
    if extra:
        return "reference", ["extra_document"]
    if re.match(r"^(?:插图|插畫|后记|後記|特典|访谈|訪談|人物档案|人物檔案|人气投票|人氣投票|短篇故事初次发表时间表)", normalized):
        return "reference", ["backmatter_or_reference_title"]
    if re.search(r"(?:\s(?:设定|設定)$|^(?:技能一覧|スキル一覧|用语集|用語集)$)", normalized):
        return "reference", ["setting_or_list_title"]
    if _list_ratio(body) >= 0.35:
        reasons.append("state_or_skill_density")
        return "narrative_list", reasons
    if re.match(r"^(?:S|Y|R|K|B|血|鬼|王|黑|白|O|裏鬼|小苏菲亚日记|小ソフィア日記|幕间|幕間|间章|閑話|間話)", normalized, re.I):
        reasons.append("side_story_or_interlude_label")
        return "narrative_side", reasons
    return "narrative", reasons


def _paragraph_id(entry_id: str, revision_id: str, ordinal: int) -> str:
    return f"{entry_id}@{revision_id[:16]}:p{ordinal:04d}"


def build_spider_corpus(root: Path, output: Path) -> dict:
    """Build a deterministic, edition-aware sidecar corpus.

    The source EPUB files are read only.  `raw_title` and `raw_text` are kept
    exactly as extracted; normalized values and mask annotations are metadata.
    """
    documents = spider_documents(root)
    entries: list[dict] = []
    paragraphs: list[dict] = []
    issues: list[dict] = []
    document_rows: list[dict] = []
    edition_offsets: dict[str, int] = defaultdict(int)
    for document in documents:
        before = file_digest(document.path)
        document_rows.append({
            "edition_id": document.edition_id,
            "language": document.language,
            "document_id": document.document_id,
            "path": str(document.path.relative_to(root)),
            "volume": document.volume,
            "extra": document.extra,
            "sha256": before,
        })
        for ordinal, href, title, markup, book_title in epub_entries(document.path):
            rows, source_url = extract_paragraphs(markup, document.language)
            body = [row["text"] for row in rows if row["kind"] == "body" and row["text"].strip()]
            entry_id = f"{document.document_id}:{href}"
            revision = digest({"parser": VERSION, "href": href, "title": title, "rows": rows})
            kind, reasons = classify_entry(title, body, document.language, extra=document.extra, book_title=book_title)
            image_count = len(re.findall(br"<img\b", markup, re.I))
            title_masks = mask_spans(title, document.language)
            if title_masks:
                issues.append({"type": "suspected_site_mask", "entry_id": entry_id, "title": title, "spans": title_masks})
            if kind == "empty_or_layout":
                issues.append({"type": "empty_or_layout_entry", "entry_id": entry_id, "href": href, "title": title})
            if not body and kind != "empty_or_layout":
                issues.append({"type": "image_only_or_layout" if image_count else "no_extracted_body",
                               "entry_id": entry_id, "href": href, "title": title, "image_count": image_count})
            entry = {
                "schema_version": "1",
                "work_id": "spider",
                "edition_id": document.edition_id,
                "language": document.language,
                "document_id": document.document_id,
                "entry_id": entry_id,
                "native_order": ordinal,
                "edition_order": edition_offsets[document.edition_id] + ordinal,
                "href": href,
                "raw_title": title,
                "normalized_title": normalize_title(title),
                "title_mask_spans": title_masks,
                "book_title": book_title,
                "kind": kind,
                "classification_reasons": reasons,
                "source_url": source_url,
                "revision_id": revision,
                "source_hash": digest(markup),
                "body_char_count": sum(map(len, body)),
                "body_paragraph_count": len(body),
                "paragraph_ids": [],
                "metadata": {"volume": document.volume, "extra": document.extra, "image_count": image_count},
            }
            for p_ordinal, row in enumerate(rows):
                pid = _paragraph_id(entry_id, revision, p_ordinal)
                text = row["text"]
                paragraph = {
                    "schema_version": "1",
                    "work_id": "spider",
                    "edition_id": document.edition_id,
                    "language": document.language,
                    "document_id": document.document_id,
                    "entry_id": entry_id,
                    "paragraph_id": pid,
                    "revision_id": revision,
                    "ordinal": p_ordinal,
                    "kind": row["kind"],
                    "raw_text": text,
                    "normalized_text": text.replace("\r\n", "\n").replace("\r", "\n"),
                    "mask_spans": mask_spans(text, document.language),
                    "original_locator": row["original_locator"],
                    "ruby": row.get("ruby", []),
                }
                entry["paragraph_ids"].append(pid)
                paragraphs.append(paragraph)
            entries.append(entry)
        edition_offsets[document.edition_id] += sum(1 for e in entries if e["document_id"] == document.document_id)
        if file_digest(document.path) != before:
            raise RuntimeError(f"input changed during corpus build: {document.path}")
    manifest = {
        "schema_version": "1",
        "parser_version": VERSION,
        "work_id": "spider",
        "documents": document_rows,
        "entry_count": len(entries),
        "paragraph_count": len(paragraphs),
        "counts": dict(Counter(f"{e['edition_id']}:{e['kind']}" for e in entries)),
        "mask_entries": sum(bool(e["title_mask_spans"]) for e in entries),
        "issue_count": len(issues),
        "corpus_fingerprint": digest([(e["entry_id"], e["revision_id"]) for e in entries]),
    }
    output.mkdir(parents=True, exist_ok=True)
    write_jsonl(output / "entries.jsonl", entries)
    write_jsonl(output / "paragraphs.jsonl", paragraphs)
    write_jsonl(output / "issues.jsonl", issues)
    write_json(output / "manifest.json", manifest)
    return manifest


def load_spider_corpus(folder: Path) -> tuple[list[dict], list[dict]]:
    return read_jsonl(folder / "entries.jsonl"), read_jsonl(folder / "paragraphs.jsonl")


def _split_oversize(text: str, max_chars: int) -> list[tuple[int, int, str]]:
    """Split a long paragraph while keeping reversible Python offsets."""
    if len(text) <= max_chars:
        return [(0, len(text), text)]
    sentence_end = re.compile(r"[。！？!?\n]\s*")
    chunks: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        hard_end = min(len(text), start + max_chars)
        if hard_end < len(text):
            boundary = None
            for match in sentence_end.finditer(text, start, hard_end):
                if match.end() > start:
                    boundary = match.end()
            end = boundary or hard_end
        else:
            end = hard_end
        chunks.append((start, end, text[start:end]))
        start = end
    return chunks


def system_or_list_text(text: str) -> bool:
    """Conservative retrieval track hint; the original text is retained."""
    normalized = unicodedata.normalize("NFKC", text).strip()
    if re.match(r"^[《〈【]?(?:経験値|熟練度|经验值|經驗值|熟练度|熟練度).*(?:LV|Lv|技能|スキル|個体|个体|個體)", normalized):
        return True
    if re.match(r"^(?:HP|MP|SP)\s*[:：]?\s*\d", normalized):
        return True
    return normalized.startswith(("「", "〈", "【")) and len(re.findall(r"(?:LV|Lv)\s*\d+", normalized)) >= 2


def windowize(entries: list[dict], paragraphs: list[dict], *, entry_ids: set[str] | None = None, max_chars: int = 2400, overlap: int = 1) -> list[dict]:
    """Create stable paragraph windows for cross-edition retrieval."""
    if max_chars < 200 or overlap < 0:
        raise ValueError("invalid window configuration")
    wanted = ({e["entry_id"] for e in entries} if entry_ids is None else entry_ids)
    by_entry: dict[str, list[dict]] = defaultdict(list)
    for paragraph in paragraphs:
        if paragraph["entry_id"] in wanted and paragraph["kind"] == "body" and paragraph["raw_text"].strip():
            by_entry[paragraph["entry_id"]].append(paragraph)
    entry_map = {e["entry_id"]: e for e in entries if e["entry_id"] in wanted}
    windows: list[dict] = []
    for entry in sorted(entry_map.values(), key=lambda e: (e["edition_id"], e["edition_order"], e["entry_id"])):
        rows = sorted(by_entry.get(entry["entry_id"], []), key=lambda p: p["ordinal"])
        if not rows:
            continue
        units = []
        for row in rows:
            for char_start, char_end, fragment in _split_oversize(row["raw_text"], max_chars):
                units.append({"paragraph": row, "char_start": char_start, "char_end": char_end, "text": fragment})
        start = 0
        while start < len(units):
            end = start
            chars = 0
            while end < len(units) and (end == start or chars + len(units[end]["text"]) + 2 <= max_chars):
                chars += len(units[end]["text"]) + (2 if end > start else 0)
                end += 1
            selected = units[start:end]
            text = "\n\n".join(unit["text"] for unit in selected)
            first, last = selected[0], selected[-1]
            window_id = f"{entry['entry_id']}@{entry['revision_id'][:16]}:w{first['paragraph']['ordinal']:04d}.{first['char_start']:05d}-{last['paragraph']['ordinal']:04d}.{last['char_end']:05d}"
            segments = []
            cursor = 0
            for index, unit in enumerate(selected):
                if index:
                    cursor += 2
                piece = unit["text"]
                segments.append({
                    "paragraph_id": unit["paragraph"]["paragraph_id"],
                    "paragraph_start": unit["char_start"],
                    "paragraph_end": unit["char_end"],
                    "text_start": cursor,
                    "text_end": cursor + len(piece),
                })
                cursor += len(piece)
            windows.append({
                "schema_version": "1",
                "window_id": window_id,
                "work_id": "spider",
                "edition_id": entry["edition_id"],
                "language": entry["language"],
                "document_id": entry["document_id"],
                "entry_id": entry["entry_id"],
                "revision_id": entry["revision_id"],
                "native_order": entry["native_order"],
                "edition_order": entry["edition_order"],
                "raw_title": entry["raw_title"],
                "kind": entry["kind"],
                "paragraph_ids": list(dict.fromkeys(unit["paragraph"]["paragraph_id"] for unit in selected)),
                "paragraph_spans": [{
                    "paragraph_id": unit["paragraph"]["paragraph_id"],
                    "start": unit["char_start"],
                    "end": unit["char_end"],
                } for unit in selected],
                "text_segments": segments,
                "start_ordinal": first["paragraph"]["ordinal"],
                "end_ordinal": last["paragraph"]["ordinal"],
                "text": text,
                "text_hash": digest(text),
                "retrieval_text": "\n\n".join(unit["text"] for unit in selected if not system_or_list_text(unit["text"])) or text,
                "retrieval_track": "status" if all(system_or_list_text(unit["text"]) for unit in selected) else "narrative",
                "system_or_list_units": sum(system_or_list_text(unit["text"]) for unit in selected),
                "mask_spans": [{**span, "paragraph_id": unit["paragraph"]["paragraph_id"]} for unit in selected for span in unit["paragraph"]["mask_spans"]
                               if unit["char_start"] <= span["start"] and span["end"] <= unit["char_end"]],
            })
            if end == len(units):
                break
            start = max(start + 1, end - overlap)
    return windows


def _cosine(left: list[float], right: list[float]) -> float:
    denominator = math.sqrt(sum(x * x for x in left) * sum(y * y for y in right))
    return sum(x * y for x, y in zip(left, right)) / max(denominator, 1e-12)


def retrieve_window_candidates(source_windows: list[dict], target_windows: list[dict], vectors: dict[str, list[float]], *, top_k: int = 8, min_score: float | None = None) -> list[dict]:
    """Rank cross-edition candidates; scores are retrieval scores only."""
    if top_k <= 0:
        raise ValueError("top_k must be positive")
    targets, sources = target_windows, source_windows
    missing = [w["window_id"] for w in source_windows + target_windows if w["window_id"] not in vectors]
    if missing:
        raise ValueError(f"missing embedding vector: {missing[0]}")
    if not sources or not targets:
        return []
    dimension = len(vectors[sources[0]["window_id"]])
    if not dimension or any(len(vectors[w["window_id"]]) != dimension or not any(vectors[w["window_id"]])
                            or any(not math.isfinite(value) for value in vectors[w["window_id"]]) for w in sources + targets):
        raise ValueError("invalid or mismatched embedding matrix")
    source_ranked: list[list[tuple[float, int]]] = []
    target_rank: list[dict[int, int]] = [dict() for _ in targets]
    try:
        import numpy as np
        a = np.asarray([vectors[w["window_id"]] for w in sources], dtype=np.float32)
        b = np.asarray([vectors[w["window_id"]] for w in targets], dtype=np.float32)
        if a.ndim != 2 or b.ndim != 2 or a.shape[1] != b.shape[1] or not np.isfinite(a).all() or not np.isfinite(b).all():
            raise ValueError("invalid or mismatched embedding matrix")
        a /= np.maximum(np.linalg.norm(a, axis=1, keepdims=True), 1e-12)
        b /= np.maximum(np.linalg.norm(b, axis=1, keepdims=True), 1e-12)
        similarities = a @ b.T
        k = min(top_k, len(targets))
        for i in range(len(sources)):
            ranked_indices = sorted(range(len(targets)), key=lambda j: (-float(similarities[i, j]), targets[j]["edition_order"], targets[j]["window_id"]))
            source_ranked.append([(float(similarities[i, j]), j) for j in ranked_indices[:k]])
        reverse_k = min(top_k, len(sources))
        for j in range(len(targets)):
            ranked_indices = sorted(range(len(sources)), key=lambda i: (-float(similarities[i, j]), sources[i]["edition_order"], sources[i]["window_id"]))
            target_rank[j] = {i: rank for rank, i in enumerate(ranked_indices[:reverse_k], 1)}
    except ImportError:
        for i, source in enumerate(sources):
            ranked = sorted(((_cosine(vectors[source["window_id"]], vectors[target["window_id"]]), j) for j, target in enumerate(targets)),
                            key=lambda item: (-item[0], targets[item[1]]["edition_order"], targets[item[1]]["window_id"]))
            source_ranked.append(ranked[:top_k])
        for j, target in enumerate(targets):
            ranked = sorted(((_cosine(vectors[source["window_id"]], vectors[target["window_id"]]), i) for i, source in enumerate(sources)),
                            key=lambda item: (-item[0], sources[item[1]]["edition_order"], sources[item[1]]["window_id"]))
            target_rank[j] = {i: rank for rank, (_, i) in enumerate(ranked[:top_k], 1)}
    result = []
    for i, source in enumerate(sources):
        for rank, (score, j) in enumerate(source_ranked[i], 1):
            target = targets[j]
            if min_score is not None and score < min_score:
                continue
            result.append({
                "alignment_id": "candidate:" + digest([source["window_id"], target["window_id"]])[:20],
                "source_window_id": source["window_id"],
                "reference_window_id": target["window_id"],
                "source_entry_id": source["entry_id"],
                "reference_entry_id": target["entry_id"],
                "source_revision_id": source["revision_id"],
                "reference_revision_id": target["revision_id"],
                "source_order": source["edition_order"],
                "reference_document_id": target["document_id"],
                "reference_order": target["edition_order"],
                "rank": rank,
                "similarity": round(score, 8),
                "reverse_rank": target_rank[j].get(i),
                "mutual_top_k": i in target_rank[j],
                "status": "candidate",
                "content_relation": "unresolved",
                "allowed_uses": [],
                "method": ALIGNMENT_VERSION,
                "evidence": {"source_text_hash": source["text_hash"], "reference_text_hash": target["text_hash"], "calibrated": False},
            })
    return result


def aggregate_entry_candidates(window_candidates: list[dict], *, top_k: int = 5) -> list[dict]:
    """Collapse window hits into inspectable entry-pair candidates.

    The best window similarity remains the primary score.  Support counts and
    reciprocal hits are evidence fields, not a calibrated probability.
    """
    pairs: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in window_candidates:
        pairs[(row["source_entry_id"], row["reference_entry_id"], row.get("retrieval_track", "narrative"))].append(row)
    collapsed = []
    for (source_entry_id, reference_entry_id, retrieval_track), rows in pairs.items():
        best = max(rows, key=lambda row: (row["similarity"], row["mutual_top_k"], -row["rank"]))
        collapsed.append({
            "alignment_id": "entry-candidate:" + digest([source_entry_id, best["source_revision_id"], reference_entry_id, best["reference_revision_id"], retrieval_track])[:20],
            "source_entry_id": source_entry_id,
            "reference_entry_id": reference_entry_id,
            "retrieval_track": retrieval_track,
            "source_revision_id": best["source_revision_id"],
            "reference_revision_id": best["reference_revision_id"],
            "status": "candidate",
            "content_relation": "unresolved",
            "allowed_uses": [],
            "best_similarity": best["similarity"],
            "window_support": len(rows),
            "mutual_window_hits": sum(row["mutual_top_k"] for row in rows),
            "best_window_pairs": [{
                "source_window_id": row["source_window_id"],
                "reference_window_id": row["reference_window_id"],
                "similarity": row["similarity"],
                "source_rank": row["rank"],
                "reference_rank": row["reverse_rank"],
                "mutual_top_k": row["mutual_top_k"],
            } for row in sorted(rows, key=lambda row: (-row["similarity"], row["rank"]))[:3]],
            "method": ALIGNMENT_VERSION,
            "evidence": {"calibrated": False},
        })
    by_source: dict[str, list[dict]] = defaultdict(list)
    for row in collapsed:
        by_source[row["source_entry_id"]].append(row)
    result = []
    for source, rows in by_source.items():
        rows.sort(key=lambda row: (-row["best_similarity"], -row["mutual_window_hits"], -row["window_support"], row["reference_entry_id"]))
        for rank, row in enumerate(rows[:top_k], 1):
            result.append({**row, "source_entry_rank": rank})
    return sorted(result, key=lambda row: (row["source_entry_id"], row["source_entry_rank"]))


def write_windows_and_candidates(output: Path, windows: list[dict], candidates: list[dict]) -> None:
    write_jsonl(output / "windows.jsonl", windows)
    write_jsonl(output / "candidates.jsonl", candidates)
    write_json(output / "candidate_manifest.json", {
        "version": ALIGNMENT_VERSION,
        "window_count": len(windows),
        "candidate_count": len(candidates),
        "source_count": len({c["source_window_id"] for c in candidates}),
        "top_k": max((c["rank"] for c in candidates), default=0),
        "fingerprint": digest(candidates),
    })
