"""EPUB image assets bound to imported edition chapters and paragraphs.

Block images are positional sidecars. Inline images are stored as offsets in
the clean paragraph text; the translation layer turns them into explicit
``[[IMG:<id>]]`` markers and validates that the model preserved them.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import mimetypes
from pathlib import Path
import posixpath
import zipfile

from bs4 import BeautifulSoup, Tag

from ..ingestion.epub import _element_text, _walk_blocks
from ..ingestion.util import clean_text_with_report
from ..storage import read_jsonl

ASSET_SCHEMA = "edition-assets-v1"
_INLINE_SENTINEL = "IMG"


def _clean(value: str) -> str:
    return clean_text_with_report(value)[0].strip()


def _sha256(data: bytes) -> str:
    import hashlib
    return hashlib.sha256(data).hexdigest()


def sniff_media_type(data: bytes, path: str = "") -> str:
    """Prefer byte signatures: some EPUBs mislabel JPEG bytes as .png."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data.lstrip()
    if head.startswith((b"<svg", b"<?xml")):
        return "image/svg+xml"
    return mimetypes.guess_type(path)[0] or "application/octet-stream"


@dataclass(frozen=True)
class Asset:
    asset_id: str
    path: str
    media_type: str
    sha256: str
    alt: str = ""


@dataclass
class AssetIndex:
    edition_id: str
    source_path: str = ""
    source_sha256: str = ""
    assets: dict[str, Asset] = field(default_factory=dict)
    chapters: dict[int, dict] = field(default_factory=dict)
    cover: str | None = None
    global_assets: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def empty(cls, edition_id: str) -> "AssetIndex":
        return cls(edition_id=edition_id)

    def chapter_assets(self, order: int) -> dict:
        return self.chapters.get(order) or {"inline": [], "block": []}

    def read_bytes(self, asset_id: str) -> bytes:
        asset = self.assets.get(asset_id)
        if asset is None:
            raise KeyError(f"unknown asset: {asset_id}")
        with zipfile.ZipFile(self.source_path) as archive:
            return archive.read(asset.path)

    def to_dict(self) -> dict:
        return {
            "schema": ASSET_SCHEMA,
            "edition_id": self.edition_id,
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
            "assets": {key: asset.__dict__ for key, asset in self.assets.items()},
            "chapters": {str(key): value for key, value in self.chapters.items()},
            "cover": self.cover,
            "global": list(self.global_assets),
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AssetIndex":
        return cls(
            edition_id=value.get("edition_id", ""),
            source_path=value.get("source_path", ""),
            source_sha256=value.get("source_sha256", ""),
            assets={key: Asset(**row) for key, row in (value.get("assets") or {}).items()},
            chapters={int(key): row for key, row in (value.get("chapters") or {}).items()},
            cover=value.get("cover"),
            global_assets=list(value.get("global") or []),
            warnings=list(value.get("warnings") or []),
        )


def _source_epub(project, edition_id: str) -> Path | None:
    manifest = project._manifest()
    item = (manifest.get("editions") or {}).get(edition_id)
    if not item:
        return None
    corpus = project.path / item["corpus_folder"]
    sources_path = corpus.parent / "extraction" / "sources.jsonl"
    if not sources_path.is_file():
        return None
    for row in read_jsonl(sources_path):
        if row.get("format") == "epub" and Path(row.get("path", "")).is_file():
            return Path(row["path"])
    return None


def _paragraphs_by_order(entries: list[dict], paragraphs: list[dict]) -> dict[int, list[dict]]:
    order_by_entry = {entry["entry_id"]: entry["edition_order"] for entry in entries}
    result: dict[int, list[dict]] = {}
    for row in paragraphs:
        if row.get("kind") != "body" or not str(row.get("raw_text", "")).strip():
            continue
        order = order_by_entry.get(row.get("entry_id"))
        if order is None:
            continue
        result.setdefault(order, []).append(row)
    for rows in result.values():
        rows.sort(key=lambda row: row.get("ordinal", 0))
    return result


def _resource_entries(entries: list[dict]) -> dict[str, int]:
    result: dict[str, int] = {}
    for entry in entries:
        for source_range in (entry.get("source_ranges") or []):
            resource = source_range.get("resource_id")
            if resource:
                result.setdefault(resource, entry["edition_order"])
    return result


def _add_asset(assets: dict[str, Asset], archive: zipfile.ZipFile,
               resource_id: str, src: str, alt: str) -> Asset | None:
    if not src or src.startswith("data:"):
        return None
    path = posixpath.normpath(posixpath.join(posixpath.dirname(resource_id), src))
    path = path.lstrip("/")
    names = set(archive.namelist())
    if path not in names:
        # Some EPUBs use root-relative image references.
        path = posixpath.normpath(src.lstrip("/"))
        if path not in names:
            return None
    data = archive.read(path)
    sha = _sha256(data)
    asset_id = "img-" + sha[:16]
    assets.setdefault(asset_id, Asset(asset_id=asset_id, path=path,
                                      media_type=sniff_media_type(data, path), sha256=sha,
                                      alt=alt or ""))
    return assets[asset_id]


def _block_positions(block: Tag, images: list[Tag]) -> tuple[str, list[int]]:
    """Return cleaned text with sentinels removed and marker offsets."""
    clone = deepcopy(block)
    clone_images = clone.find_all("img")
    sentinels = []
    for index, original in enumerate(images):
        sentinel = f"{_INLINE_SENTINEL}{index}"
        if index < len(clone_images):
            clone_images[index].replace_with(sentinel)
        sentinels.append(sentinel)
    marked = _clean(_element_text(clone))
    positions: list[int] = []
    chunks: list[str] = []
    cursor = 0
    offset = 0
    for sentinel in sentinels:
        found = marked.find(sentinel, cursor)
        if found < 0:
            return marked, []
        chunk = marked[cursor:found]
        chunks.append(chunk)
        offset += len(chunk)
        positions.append(offset)
        cursor = found + len(sentinel)
    chunks.append(marked[cursor:])
    return "".join(chunks), positions


def _chapter_block(assets: dict[str, Asset], archive: zipfile.ZipFile,
                   resource_id: str, image: Tag, *, after: int | None,
                   before: int | None) -> dict | None:
    asset = _add_asset(assets, archive, resource_id,
                       image.get("src", ""), image.get("alt", ""))
    if asset is None:
        return None
    row = {"asset_id": asset.asset_id}
    if after is not None:
        row["after_paragraph"] = after
    else:
        row["before_paragraph"] = before if before is not None else 0
    return row


def _nearest_entry(resources: list[dict], resource_entries: dict[str, int],
                   paragraphs_by_order: dict[int, list[dict]],
                   index: int) -> tuple[int, str] | None:
    for cursor in range(index - 1, -1, -1):
        order = resource_entries.get(resources[cursor].get("resource_id", ""))
        if order is not None and paragraphs_by_order.get(order):
            return order, "after"
    for cursor in range(index + 1, len(resources)):
        order = resource_entries.get(resources[cursor].get("resource_id", ""))
        if order is not None and paragraphs_by_order.get(order):
            return order, "before"
    return None


def build_asset_index(project, edition_id: str, source_path: Path,
                      source_sha256: str) -> AssetIndex:
    index = AssetIndex(edition_id=edition_id, source_path=str(source_path),
                       source_sha256=source_sha256)
    manifest = project._manifest()
    item = (manifest.get("editions") or {}).get(edition_id)
    if not item:
        return index
    corpus = project.path / item["corpus_folder"]
    entries = read_jsonl(corpus / "entries.jsonl")
    paragraphs = read_jsonl(corpus / "paragraphs.jsonl")
    paragraphs_by_order = _paragraphs_by_order(entries, paragraphs)
    resource_entries = _resource_entries(entries)
    sources_path = corpus.parent / "extraction" / "sources.jsonl"
    source_record = None
    for row in read_jsonl(sources_path):
        if Path(row.get("path", "")).resolve() == source_path.resolve():
            source_record = row
            break
    resources = list((source_record or {}).get("resources") or [])
    if not resources:
        index.warnings.append("source manifest has no resources")
        return index

    with zipfile.ZipFile(source_path) as archive:
        for resource_pos, resource in enumerate(resources):
            resource_id = resource.get("resource_id") or resource.get("href") or ""
            media_type = resource.get("media_type", "")
            if not resource_id or ("html" not in media_type and
                                   not resource_id.endswith((".xhtml", ".html"))):
                continue
            if resource_id not in archive.namelist():
                continue
            soup = BeautifulSoup(archive.read(resource_id).decode("utf-8", "replace"),
                                 "html.parser")
            body = soup.body or soup
            order = resource_entries.get(resource_id)
            chapter_paragraphs = paragraphs_by_order.get(order, []) if order is not None else []
            text_blocks = [element for element, _kind, extracted, _origins in _walk_blocks(body)
                           if extracted.strip()]
            block_to_paragraph: dict[int, int] = {}
            if chapter_paragraphs:
                used: set[int] = set()
                for block_index, block in enumerate(text_blocks):
                    text = _clean(_element_text(block))
                    for paragraph_index, paragraph in enumerate(chapter_paragraphs):
                        if paragraph_index in used:
                            continue
                        if _clean(paragraph["raw_text"]) == text:
                            block_to_paragraph[block_index] = paragraph_index
                            used.add(paragraph_index)
                            break
            block_ids = {id(block): number for number, block in enumerate(text_blocks)}
            first_paragraph = min(block_to_paragraph.values()) if block_to_paragraph else 0
            last_paragraph: int | None = None
            inline_images: list[tuple[Tag, Tag]] = []
            image_only: list[Tag] = []
            for node in body.descendants:
                if isinstance(node, Tag) and id(node) in block_ids:
                    block_number = block_ids[id(node)]
                    if block_number in block_to_paragraph:
                        last_paragraph = block_to_paragraph[block_number]
                    continue
                if not isinstance(node, Tag) or node.name != "img":
                    continue
                parent = node.parent
                block = None
                while isinstance(parent, Tag):
                    if id(parent) in block_ids:
                        block = parent
                        break
                    parent = parent.parent
                if block is None:
                    image_only.append(node)
                else:
                    inline_images.append((node, block))

            if order is not None and chapter_paragraphs:
                inline: list[dict] = []
                block_rows: list[dict] = []
                for image, block in inline_images:
                    block_number = block_ids[id(block)]
                    paragraph_index = block_to_paragraph.get(block_number)
                    if paragraph_index is None:
                        placement = _chapter_block(
                            index.assets, archive, resource_id, image,
                            after=last_paragraph, before=first_paragraph)
                        if placement:
                            block_rows.append(placement)
                        continue
                    block_images = block.find_all("img")
                    clean_text, positions = _block_positions(block, block_images)
                    image_position = next(
                        (position for position, candidate in enumerate(block_images)
                         if candidate is image), -1)
                    source_text = _clean(chapter_paragraphs[paragraph_index]["raw_text"])
                    if image_position >= 0 and positions and clean_text == source_text:
                        asset = _add_asset(index.assets, archive, resource_id,
                                           image.get("src", ""), image.get("alt", ""))
                        if asset:
                            inline.append({
                                "asset_id": asset.asset_id,
                                "paragraph": paragraph_index,
                                "offset": positions[image_position],
                            })
                    else:
                        placement = _chapter_block(
                            index.assets, archive, resource_id, image,
                            after=paragraph_index, before=None)
                        if placement:
                            block_rows.append(placement)
                for image in image_only:
                    if last_paragraph is not None:
                        placement = _chapter_block(
                            index.assets, archive, resource_id, image,
                            after=last_paragraph, before=None)
                    else:
                        placement = _chapter_block(
                            index.assets, archive, resource_id, image,
                            after=None, before=first_paragraph)
                    if placement:
                        block_rows.append(placement)
                if inline or block_rows:
                    entry = index.chapters.setdefault(
                        order, {"inline": [], "block": []})
                    entry["inline"].extend(inline)
                    entry["block"].extend(block_rows)
                continue

            # Resources without a text entry: cover, illustration page, etc.
            # Inline images in an unmapped resource are treated as page images.
            if order is None:
                image_only.extend(image for image, _block in inline_images)
            if not image_only:
                continue
            lower = resource_id.lower()
            if "cover" in lower:
                for image in image_only:
                    asset = _add_asset(index.assets, archive, resource_id,
                                       image.get("src", ""), image.get("alt", ""))
                    if asset and index.cover is None:
                        index.cover = asset.asset_id
                continue
            nearest = _nearest_entry(resources, resource_entries, paragraphs_by_order,
                                     resource_pos)
            if nearest is None:
                for image in image_only:
                    asset = _add_asset(index.assets, archive, resource_id,
                                       image.get("src", ""), image.get("alt", ""))
                    if asset and asset.asset_id not in index.global_assets:
                        index.global_assets.append(asset.asset_id)
                continue
            target_order, position = nearest
            target_paragraphs = paragraphs_by_order[target_order]
            for image in image_only:
                asset = _add_asset(index.assets, archive, resource_id,
                                   image.get("src", ""), image.get("alt", ""))
                if asset is None:
                    continue
                placement = {"asset_id": asset.asset_id}
                if position == "after":
                    placement["after_paragraph"] = len(target_paragraphs) - 1
                else:
                    placement["before_paragraph"] = 0
                index.chapters.setdefault(target_order, {"inline": [], "block": []})
                index.chapters[target_order]["block"].append(placement)
    return index


def load_asset_index(project, edition_id: str, *, refresh: bool = False) -> AssetIndex:
    source_path = _source_epub(project, edition_id)
    if source_path is None:
        return AssetIndex.empty(edition_id)
    source_sha = _sha256(source_path.read_bytes())
    cache = project.path / "assets" / f"{edition_id}.json"
    if cache.is_file() and not refresh:
        try:
            value = json.loads(cache.read_text(encoding="utf-8"))
            if value.get("source_sha256") == source_sha:
                return AssetIndex.from_dict(value)
        except (OSError, ValueError, TypeError):
            pass
    index = build_asset_index(project, edition_id, source_path, source_sha)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(index.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
    return index
