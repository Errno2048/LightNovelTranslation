"""Source-aware corpus ingestion; the source files are never modified."""
from __future__ import annotations

import html
import posixpath
import re
import unicodedata
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict
from pathlib import Path
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
from ..text import clean_dom

from ..schemas import Chapter, Paragraph, record
from ..storage import atomic_text, digest, file_digest, read_json, read_jsonl, write_json, write_jsonl

PARSER_VERSION = "corpus-v3-clean"
SIDE_NUMBERS = {"": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def normal_title(title: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", title)).strip()


def classify(title: str, source: str, locator: str, book_title: str = "") -> tuple[str, str | None]:
    title = normal_title(title)
    if "cover" in locator.lower() or not title:
        return "cover", None
    if title == normal_title(book_title):
        return "volume_intro", None
    if source == "linovelib" and posixpath.basename(locator) == "page0.html" and title.startswith(normal_title(book_title)):
        return "volume_intro", None
    if title in {"作品相关", "作品相關"}:
        return "publication_info", None
    if re.match(r"^0+\s", title):
        return "reference", None
    number = re.match(r"^(?:第)?([1-9]\d*)(?:章)?(?:\s|$)", title)
    if number:
        return "narrative", "main:" + str(int(number[1]))
    if re.match(r"^最[終终](?:話|话|章)", title):
        return "narrative", "final"
    side = re.match(r"^[閑闲]章([一二三四五]?)\s", title)
    if side:
        number = re.search(r"(?:\s)(\d+)(?:\s*(?:終|终))?$", title)
        if number:
            return "narrative", f"side:{SIDE_NUMBERS[side[1]]}:{int(number[1])}"
    return "unclassified", None


def epub_entries(path: Path):
    with zipfile.ZipFile(path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        opf = next(n.attrib["full-path"] for n in container.iter() if n.tag.endswith("rootfile"))
        package = ET.fromstring(archive.read(opf))
        items = {n.attrib["id"]: n.attrib for n in package.iter() if n.tag.endswith("}item")}
        book_title = next((n.text or "" for n in package.iter() if n.tag.endswith("}title")), "")
        for ordinal, node in enumerate(n for n in package.iter() if n.tag.endswith("}itemref")):
            item = items[node.attrib["idref"]]
            href = posixpath.normpath(posixpath.join(posixpath.dirname(opf), item["href"]))
            content = archive.read(href)
            title = re.search(r"<title[^>]*>(.*?)</title>", content.decode("utf-8-sig"), re.S | re.I)
            yield ordinal, href, html.unescape(title[1]).strip() if title else "", content, book_title


def cache_index(folder: Path) -> dict[str, list[Path]]:
    index = defaultdict(list)
    for path in sorted(folder.glob("syosetu_*.html")):
        text = path.read_text(encoding="utf-8-sig")
        match = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
        if match:
            title = html.unescape(match[1]).split(" - ", 1)[-1]
            index[normal_title(title)].append(path)
    return index


def extract_paragraphs(markup: bytes | str | BeautifulSoup, language: str) -> tuple[list[dict], str | None]:
    # A supplied soup is consumed (ruby reading nodes are removed below).
    soup = markup if isinstance(markup, BeautifulSoup) else BeautifulSoup(markup, "html.parser", from_encoding="utf-8" if isinstance(markup, bytes) else None)
    # Cached Japanese HTML remains immutable; apply the crawler cleanup to its
    # parsed view too, so ingestion cannot reintroduce discarded watermarks.
    if language == "ja":
        clean_dom(soup)
    url = soup.find("meta", attrs={"property": "og:url"})
    source_url = url.get("content") if url else None
    rows = []
    for ordinal, p in enumerate(soup.find_all("p")):
        pid = p.get("id", "")
        if language == "ja":
            if not re.fullmatch(r"L(?:p|a)?\d+", pid):
                continue
            kind = "preface" if pid.startswith("Lp") else "afterword" if pid.startswith("La") else "body"
        else:
            if p.find_parent("body") is None:
                continue
            kind = "body"
        ruby = []
        for annotation in p.find_all("ruby"):
            rt = annotation.find("rt")
            if rt is not None:
                generated = any(str(c).startswith("japanese-text") for c in rt.get("class", []))
                reading = rt.get_text()
                base = "".join(t for t in annotation.find_all(string=True) if not t.find_parent(["rt", "rp"]))
                if not generated:
                    ruby.append({"base": base, "reading": reading})
        for tag in p.find_all(["rt", "rp", "script", "style"]):
            tag.decompose()
        for br in p.find_all("br"):
            br.replace_with("\n")
        text = p.get_text().replace("\r\n", "\n").replace("\r", "\n").strip()
        # Preserve notes as separate records rather than irreversibly deleting them.
        if language == "zh":
            is_frontmatter = len(rows) < 14 and re.match(r"^(网译|網譯|作者[：:]|翻译[：:]|翻譯[：:]|录入|錄入|声明[：:]|聲明[：:]|仅供|僅供|请尊重|請尊重|下载后|下載後|轻之国度|輕之國度|超长连载|超長連載)", text)
            if is_frontmatter or re.match(r"^(?:译者注|譯者注|译注|譯注)[：:]", text):
                kind = "translator_note"
        rows.append({"kind": kind, "text": text, "original_locator": f"p#{pid}" if pid else f"p[{ordinal}]", "ruby": ruby})
    return rows, source_url


def ingest(root: Path, output: Path) -> dict:
    if (output / "corpus_manifest.json").exists() and read_json(output / "corpus_manifest.json").get("reference_audit_fingerprint"):
        raise ValueError("derived repair corpus cannot be overwritten by ingest; use a new import branch")
    output.mkdir(parents=True, exist_ok=True)
    index = cache_index(root / "temp")
    chapters, paragraphs, documents, issues = [], [], [], []
    raw_documents = {}
    inputs = [(p, "ja", "syosetu") for p in sorted((root / "out_syosetu").glob("*.epub"))]
    inputs += [(p, "zh", "linovelib") for p in sorted((root / "out_linovelib").glob("*.epub"))]
    if not inputs:
        raise FileNotFoundError("No EPUB inputs under out_syosetu or out_linovelib")
    for path, language, source in inputs:
        before_hash = file_digest(path)
        doc = {"path": str(path.relative_to(root)), "language": language, "sha256": before_hash}
        documents.append(doc)
        previous = None
        for order, href, title, epub_bytes, book_title in epub_entries(path):
            raw = None
            candidates = index.get(normal_title(title), []) if language == "ja" else []
            if len(candidates) == 1:
                raw = candidates[0]
            elif len(candidates) > 1:
                issues.append({"type": "ambiguous_cache", "epub": str(path), "href": href, "candidates": [str(p) for p in candidates]})
            content = raw.read_bytes() if raw else epub_bytes
            rows, url = extract_paragraphs(content, language)
            locator = str(raw.relative_to(root)) if raw else str(path.relative_to(root)) + "!" + href
            if raw:
                raw_documents[str(raw.relative_to(root))] = digest(content)
            native = re.search(r"ncode\.syosetu\.com/([^/]+)/(\d+)/", url or "")
            chapter_id = f"ja:syosetu:{native[1]}:{native[2]}" if native else f"{language}:{source}:{path.stem}:{href}"
            revision = digest({"parser": PARSER_VERSION, "title": title, "paragraphs": rows})
            kind, key = classify(title, source, href, book_title)
            chapter_paragraphs = []
            for number, row in enumerate(rows):
                pid = f"{chapter_id}@{revision[:16]}:p{number:04d}"
                chapter_paragraphs.append(Paragraph(pid, chapter_id, revision, number, **row))
            body = "\n\n".join(p.text for p in chapter_paragraphs if p.kind == "body")
            chapter = Chapter(chapter_id, revision, language, source, order, title, kind, key, locator, url,
                              body, [p.paragraph_id for p in chapter_paragraphs], len(body), digest(content),
                              {"epub": str(path.relative_to(root)), "epub_href": href, "book_title": book_title,
                               "used_raw_cache": bool(raw), "nonempty_body_paragraphs": sum(bool(p.text) and p.kind == "body" for p in chapter_paragraphs)})
            chapters.append(record(chapter))
            paragraphs.extend(record(p) for p in chapter_paragraphs)
            if kind == "unclassified" or (kind == "narrative" and len(body.strip()) < 300):
                issues.append({"type": "unclassified" if kind == "unclassified" else "short_body", "chapter_id": chapter_id, "title": title, "characters": len(body)})
            if "\ufffd" in body or any(0xE000 <= ord(c) <= 0xF8FF for c in body):
                issues.append({"type": "encoding_artifact", "chapter_id": chapter_id})
            invisible = Counter(f"U+{ord(c):04X}" for c in body if c in "\u200b\u200c\u200d\ufeff")
            if invisible:
                issues.append({"type": "invisible_characters", "chapter_id": chapter_id, "counts": dict(invisible), "action": "preserved; review before treating reference as gold"})
            nonempty = [p.text for p in chapter_paragraphs if p.kind == "body" and p.text]
            for a, b in zip(nonempty, nonempty[1:]):
                if a == b and len(a) > 60:
                    issues.append({"type": "adjacent_repeated_paragraph", "chapter_id": chapter_id, "text_hash": digest(a)})
                    chapters[-1]['metadata'].setdefault('reference_integrity', {}).update({
                        'parallel_body_eligible': False, 'evaluation_eligible': False,
                        'quality_holds': ['adjacent_repeated_paragraph_needs_independent_review']})
            if previous and body.strip() and body == previous["text"]:
                issues.append({"type": "duplicate_chapter_body", "chapters": [previous["chapter_id"], chapter_id]})
            previous = record(chapter)
        if file_digest(path) != before_hash:
            raise RuntimeError("input file changed during ingestion")
    ids = [c["chapter_id"] for c in chapters]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate chapter identities; resolve before importing")
    manifest = {"schema_version": "1", "parser_version": PARSER_VERSION, "documents": documents,
                "raw_cache_documents": raw_documents, "chapter_count": len(chapters), "paragraph_count": len(paragraphs),
                "corpus_fingerprint": digest([(c["chapter_id"], c["revision_id"]) for c in chapters]),
                "counts": dict(Counter(c["language"] + ":" + c["kind"] for c in chapters)), "issues": len(issues)}
    write_jsonl(output / "chapters.jsonl", chapters)
    write_jsonl(output / "paragraphs.jsonl", paragraphs)
    write_jsonl(output / "corpus_issues.jsonl", issues)
    write_json(output / "corpus_manifest.json", manifest)
    lines = ["# Corpus import", "", f"Parser: {PARSER_VERSION}", f"Fingerprint: {manifest['corpus_fingerprint']}", "",
             "| Kind | Count |", "| --- | ---: |"]
    lines += [f"| {k} | {v} |" for k, v in sorted(manifest["counts"].items())]
    lines += ["", f"Raw Japanese cache used: {sum(c['metadata']['used_raw_cache'] for c in chapters)}", f"Issues: {len(issues)}",
              "", "Notes, empty paragraphs and native ruby are retained in paragraphs.jsonl. Narrative text excludes front/afterword and classified translator notes."]
    atomic_text(output / "ingest_report.md", "\n".join(lines) + "\n")
    return manifest


def load_chapters(folder: Path) -> list[dict]:
    return read_jsonl(folder / "chapters.jsonl")
