"""Generic EPUB resource and text-block extraction."""

from __future__ import annotations

from copy import deepcopy
import html
import posixpath
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
import warnings

from bs4 import BeautifulSoup, Comment, NavigableString, Tag, XMLParsedAsHTMLWarning

from ..storage import file_digest
from .txt import decode_text
from .util import clean_text_with_report, normalize_text, stable_block_id


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(node: ET.Element, name: str, default: str = "") -> str:
    if name in node.attrib:
        return node.attrib[name]
    for key, value in node.attrib.items():
        if _local(key) == name:
            return value
    return default


def _decode(data: bytes, *, override: str | None = None) -> tuple[str, str, list[dict]]:
    """Decode an EPUB resource without silently replacing malformed bytes."""
    if override:
        try:
            return data.decode(override, errors="strict"), override, []
        except (LookupError, UnicodeDecodeError) as exc:
            raise ValueError("declared EPUB encoding cannot decode the resource") from exc
    if data.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff", b"\x00\x00\xfe\xff")):
        return decode_text(data)
    head = data[:4096]
    match = re.search(br"encoding\s*=\s*['\"]\s*([A-Za-z0-9._-]+)", head, re.I)
    meta = re.search(br"charset\s*=\s*['\"]?\s*([A-Za-z0-9._-]+)", head, re.I)
    declaration = match or meta
    # A declared codec is authoritative. Falling through to a different codec
    # after a decoding failure can silently turn damaged XHTML into other text.
    if declaration:
        codec = declaration[1].decode("ascii")
        text, codec, problems = decode_text(data, override=codec)
    else:
        text, codec, problems = decode_text(data)
    return text, codec, [{**row, "code": "non_utf8_resource" if row["code"] == "non_utf8_text" else row["code"]}
                         for row in problems]


def _resolve(base: str, href: str) -> str:
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        return href
    path = unquote(parts.path)
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), path)) if path else base


def _fragment(href: str) -> str | None:
    return unquote(urlsplit(href).fragment) or None


def _read_nav(data: bytes, nav_path: str, *, package_path: str) -> tuple[list[dict], list[dict]]:
    """Read EPUB 3 nav or EPUB 2 NCX into flat, source-local targets."""
    text, _, problems = _decode(data)
    entries: list[dict] = []
    try:
        # html.parser is intentionally retained for tolerant EPUB navigation
        # handling; XML declarations in valid XHTML are expected here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
            soup = BeautifulSoup(text, "html.parser")
        nav = None
        for candidate in soup.find_all("nav"):
            value = " ".join(candidate.get("epub:type", []) if isinstance(candidate.get("epub:type"), list)
                              else [str(candidate.get("epub:type", ""))])
            if "toc" in value or "toc" in " ".join(candidate.get("class", [])):
                nav = candidate
                break
        nav = nav or (soup.find("nav") if soup.find("nav") else None)
        if nav:
            def visit(node: Tag, level: int = 0):
                for child in node.find_all("li", recursive=False):
                    anchor = child.find("a", recursive=False) or child.find("a")
                    nested = child.find("ol", recursive=False) or child.find("ul", recursive=False)
                    if anchor and anchor.get("href"):
                        href = anchor["href"]
                        entries.append({"label": clean_text_with_report(anchor.get_text("", strip=False))[0].strip(),
                                        "href": _resolve(nav_path, href),
                                        "fragment": _fragment(href),
                                        "level": level, "has_children": nested is not None,
                                        "source": "epub3-nav"})
                    if nested:
                        visit(nested, level + 1)
            ordered = nav.find("ol") or nav.find("ul")
            if ordered:
                visit(ordered)
            return entries, problems
    except Exception as exc:  # pragma: no cover - malformed navigation is reported below
        problems.append({"code": "nav_parse_error", "error": type(exc).__name__})
    try:
        root = ET.fromstring(text)
        def visit_ncx(node: ET.Element, level: int = 0):
            for child in list(node):
                if _local(child.tag) != "navPoint":
                    continue
                label = next((n.text or "" for n in child.iter() if _local(n.tag) == "text"), "")
                content = next((n for n in child.iter() if _local(n.tag) == "content"), None)
                href = _attr(content, "src") if content is not None else ""
                if href:
                    entries.append({"label": clean_text_with_report(label)[0].strip(), "href": _resolve(nav_path, href),
                                    "fragment": _fragment(href), "level": level,
                                    "has_children": any(_local(grandchild.tag) == "navPoint"
                                                        for grandchild in child),
                                    "source": "epub2-ncx"})
                visit_ncx(child, level + 1)
        for node in root.iter():
            if _local(node.tag) == "navMap":
                visit_ncx(node)
                break
    except (ET.ParseError, ValueError):
        problems.append({"code": "nav_parse_error", "error": "invalid_xml"})
    return entries, problems


def _element_text(element: Tag) -> str:
    if element.name == "tr":
        cells = element.find_all(["td", "th"], recursive=False)
        if cells:
            return "\t".join(_element_text(cell) for cell in cells)
    clone = deepcopy(element)
    for annotation in clone.find_all(["rt", "rp", "script", "style", "noscript", "nav", "head"]):
        annotation.decompose()
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return clone.get_text("", strip=False).strip().replace("\r\n", "\n").replace("\r", "\n")


def _ruby(element: Tag) -> list[dict[str, str]]:
    result = []
    for ruby in element.find_all("ruby"):
        rt = ruby.find("rt")
        if rt is None:
            continue
        base = "".join(str(child) for child in ruby.contents if not (isinstance(child, Tag) and child.name in {"rt", "rp"}))
        result.append({"base": clean_text_with_report(BeautifulSoup(base, "html.parser").get_text())[0],
                       "reading": clean_text_with_report(rt.get_text("", strip=True))[0]})
    return result


def _has_leaf(node: Tag) -> bool:
    return node.find(["h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "pre",
                      "li", "dt", "dd", "tr", "div", "section", "article", "main", "table"]) is not None


def _walk_blocks(body: Tag):
    leaves = {"h1": "heading", "h2": "heading", "h3": "heading", "h4": "heading",
              "h5": "heading", "h6": "heading", "p": "paragraph", "blockquote": "paragraph",
              "pre": "paragraph", "li": "list_item", "dt": "list_item", "dd": "list_item",
              "tr": "table_row"}
    containers = {"html", "div", "section", "article", "main", "aside", "header", "footer", "body"}

    def visit(node: Tag):
        inline = []

        def flush_inline():
            if not inline:
                return
            wrapper = BeautifulSoup("<span></span>", "html.parser").span
            originals = list(inline)
            for item in inline:
                wrapper.append(deepcopy(item))
            text = _element_text(wrapper)
            inline.clear()
            if text.strip():
                yield wrapper, "text", text, originals

        for child in node.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                if str(child).strip():
                    inline.append(child)
                continue
            if not isinstance(child, Tag) or child.name in {"script", "style", "noscript", "nav", "head"}:
                continue
            if child.name in leaves:
                yield from flush_inline()
                text = _element_text(child)
                if text.strip():
                    yield child, leaves[child.name], text, [child]
                continue
            if child.name == "table":
                yield from flush_inline()
                if not _has_leaf(child):
                    text = _element_text(child)
                    if text.strip():
                        yield child, "table", text, [child]
                else:
                    yield from visit(child)
                continue
            if child.name in containers:
                yield from flush_inline()
                if _has_leaf(child):
                    yield from visit(child)
                else:
                    text = _element_text(child)
                    if text.strip():
                        yield child, "text", text, [child]
                continue
            if _has_leaf(child):
                yield from flush_inline()
                yield from visit(child)
            else:
                inline.append(child)
        yield from flush_inline()

    yield from visit(body)


def extract_resource(markup: bytes, *, source_id: str, source_hash: str,
                     resource_id: str, source_order: int,
                     encoding_override: str | None = None) -> tuple[list[dict], dict, list[dict]]:
    text, encoding, decode_issues = _decode(markup, override=encoding_override)
    soup = BeautifulSoup(text, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    body = soup.body or soup
    blocks: list[dict] = []
    node_blocks: dict[int, int] = {}
    for index, (element, node_kind, extracted, origins) in enumerate(_walk_blocks(body)):
        cleaned, local_counts = clean_text_with_report(extracted)
        if not cleaned.strip():
            continue
        anchors = []
        current = element
        while isinstance(current, Tag) and current is not body.parent:
            anchor = current.get("id") or current.get("name")
            if anchor:
                anchors.append(anchor)
            if current is body:
                break
            current = current.parent
        locator = {"resource_id": resource_id, "block_index": index,
                   "tag": element.name, "anchor": element.get("id") or element.get("name"),
                   "anchor_chain": list(reversed(anchors))}
        blocks.append({
            "block_id": stable_block_id(source_id, source_hash, resource_id, source_order, index),
            "source_id": source_id, "source_sha256": source_hash,
            "resource_id": resource_id, "source_order": source_order,
            "node_kind": node_kind, "raw_text": cleaned, "normalized_text": normalize_text(cleaned),
            "locator": locator, "ruby": _ruby(element),
            "asset_refs": [{"src": image.get("src"), "alt": image.get("alt", "")}
                           for image in element.find_all("img")],
            "layout_hints": {"encoding": encoding, "title": title,
                             "zero_width_removed": local_counts},
        })
        for original in origins:
            node_blocks[id(original)] = len(blocks) - 1
            if isinstance(original, Tag):
                for descendant in original.descendants:
                    node_blocks[id(descendant)] = len(blocks) - 1
    # An empty <a id=.../> immediately before a paragraph is a valid navigation
    # target too. Resolve it to the next readable block without inventing text.
    next_block: int | None = None
    for node in reversed([body, *body.descendants]):
        if id(node) in node_blocks:
            next_block = node_blocks[id(node)]
        if (not isinstance(node, Tag) or node.name in {"head", "nav", "script", "style", "rt", "rp"} or
                node.find_parent(["head", "nav", "script", "style", "rt", "rp"])):
            continue
        if next_block is not None:
            anchors = {value for value in (node.get("id"), node.get("name")) if value}
            if anchors:
                locator = blocks[next_block]["locator"]
                locator["anchors"] = sorted(set(locator.get("anchors", [])) | anchors)
    issues = list(decode_issues)
    removed = {}
    for block in blocks:
        for char, count in block["layout_hints"]["zero_width_removed"].items():
            removed[char] = removed.get(char, 0) + count
    if removed:
        issues.append({"code": "zero_width_removed", "counts": removed,
                       "resource_id": resource_id})
    visible = "".join(str(node) for node in body.descendants
                      if isinstance(node, NavigableString) and not isinstance(node, Comment)
                      and not node.find_parent(["rt", "rp", "script", "style", "noscript", "nav", "head"]))
    source_sequence = re.sub(r"\s+", "", clean_text_with_report(visible)[0])
    extracted_sequence = re.sub(r"\s+", "", "".join(row["raw_text"] for row in blocks))
    from ..storage import digest
    coverage = {"source_nonspace_hash": digest(source_sequence),
                "extracted_nonspace_hash": digest(extracted_sequence),
                "source_nonspace_chars": len(source_sequence),
                "extracted_nonspace_chars": len(extracted_sequence),
                "complete": source_sequence == extracted_sequence}
    if not coverage["complete"]:
        issues.append({"code": "text_coverage_mismatch", "severity": "error", "resource_id": resource_id})
    images = [{"src": image.get("src"), "alt": image.get("alt", "")} for image in body.find_all("img")]
    if not blocks:
        issues.append({"code": "image_only_resource" if images else "empty_resource",
                       "severity": "warning", "resource_id": resource_id})
    return blocks, {"resource_id": resource_id, "encoding": encoding, "title": title,
                    "byte_count": len(markup), "text_coverage": coverage,
                    "asset_refs": images}, issues


def extract_epub(source, path: Path) -> tuple[dict, list[dict], list[dict], list[dict]]:
    source_hash = file_digest(path)
    blocks: list[dict] = []
    navigation: list[dict] = []
    issues: list[dict] = []
    resources: list[dict] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        if "META-INF/container.xml" not in names:
            raise ValueError(f"EPUB container missing: {path}")
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = next((n for n in container.iter() if _local(n.tag) == "rootfile"), None)
        if rootfile is None:
            raise ValueError(f"EPUB rootfile missing: {path}")
        package_path = _attr(rootfile, "full-path")
        package = ET.fromstring(archive.read(package_path))
        metadata_title = next((n.text or "" for n in package.iter() if _local(n.tag) == "title"), "")
        manifest = {}
        for item in package.iter():
            if _local(item.tag) != "item":
                continue
            item_id = _attr(item, "id")
            href = _resolve(package_path, _attr(item, "href"))
            manifest[item_id] = {"id": item_id, "href": href,
                                 "media_type": _attr(item, "media-type"),
                                 "properties": _attr(item, "properties"),
                                 "linear": True}
        spine = []
        spine_node = next((n for n in package.iter() if _local(n.tag) == "spine"), None)
        if spine_node is not None:
            for itemref in spine_node:
                if _local(itemref.tag) != "itemref":
                    continue
                if _attr(itemref, "idref") in manifest:
                    item = manifest[_attr(itemref, "idref")]
                    item = {**item, "linear": _attr(itemref, "linear", "yes") != "no"}
                    spine.append(item)
                else:
                    issues.append({"code": "missing_spine_target", "severity": "error",
                                   "source_id": source.source_id, "idref": _attr(itemref, "idref")})
        if not spine:
            spine = [item for item in manifest.values() if item["media_type"] in {"application/xhtml+xml", "text/html"}]
            issues.append({"code": "missing_spine", "severity": "error",
                           "source_id": source.source_id})
        nav_item = next((item for item in manifest.values() if "nav" in item["properties"].split()), None)
        if nav_item and nav_item["href"] in names:
            navigation, nav_issues = _read_nav(archive.read(nav_item["href"]), nav_item["href"], package_path=package_path)
            issues.extend({**row, "source_id": source.source_id} for row in nav_issues)
        else:
            ncx = next((item for item in manifest.values() if item["media_type"] == "application/x-dtbncx+xml"), None)
            if ncx and ncx["href"] in names:
                navigation, nav_issues = _read_nav(archive.read(ncx["href"]), ncx["href"], package_path=package_path)
                issues.extend({**row, "source_id": source.source_id} for row in nav_issues)
        for order, item in enumerate(spine):
            if item["href"] not in names:
                issues.append({"code": "missing_resource", "resource_id": item["href"], "source_id": source.source_id})
                continue
            markup = archive.read(item["href"])
            resource_blocks, resource, resource_issues = extract_resource(
                markup, source_id=source.source_id, source_hash=source_hash,
                resource_id=item["href"], source_order=order,
                encoding_override=source.encoding_override)
            blocks.extend(resource_blocks)
            resources.append({**resource, "href": item["href"], "linear": item["linear"],
                              "media_type": item["media_type"]})
            issues.extend({**row, "source_id": source.source_id, "resource_id": item["href"]}
                          for row in resource_issues)
    if not any(row["raw_text"].strip() for row in blocks):
        issues.append({"code": "no_text_blocks", "severity": "error", "source_id": source.source_id})
    return ({"source_id": source.source_id, "path": source.path, "format": "epub",
             "sha256": source_hash, "byte_count": path.stat().st_size,
             "encoding": "per-resource", "metadata_title": html.unescape(metadata_title),
             "resources": resources, "navigation_count": len(navigation)},
            blocks, navigation, issues)
