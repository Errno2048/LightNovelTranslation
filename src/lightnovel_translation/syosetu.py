"""Narou metadata search and isolated FanFicFare acquisition.

Search results are candidates, never an automatic cross-language identity claim.
No translation provider, personal FanFicFare INI, login cookie or API key is used.
"""
from __future__ import annotations

from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import codecs
from hashlib import sha256
from html import unescape
from importlib.metadata import version
from importlib.resources import files
from io import StringIO
import json
import logging
import math
from pathlib import Path, PurePosixPath
import posixpath
import re
import time
from threading import RLock, local
import unicodedata
from urllib.parse import unquote, urldefrag, urlsplit
from xml.etree import ElementTree as ET
import zipfile

from bs4 import BeautifulSoup
import httpx

from translation.ingestion.txt import decode_text
from translation.text import strip_zero_width, zero_width_counts

from .errors import TranslationError
from .acquisition_config import SyosetuConfig
from ._source_requests import SourceRequests

API_URL = "https://api.syosetu.com/novelapi/api/"
API_CACHE_SECONDS = 14 * 24 * 3600
CATALOG_CACHE_SCHEMA = "narou-catalog-v1"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _name(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", strip_zero_width(value))).casefold()


def _unique(values):
    return list(dict.fromkeys(v.strip() for v in values if isinstance(v, str) and v.strip()))


def canonical_work(value: str) -> tuple[str, str]:
    """Accept an Ncode or a normal-site work/episode URL; return stable identity."""
    value = value.strip()
    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.hostname != "ncode.syosetu.com":
            raise ValueError("expected a normal-site ncode.syosetu.com URL")
        value = parts.path.strip("/").split("/")[0]
    if not re.fullmatch(r"n[0-9]+[a-z]+", value, re.I):
        raise ValueError("invalid Ncode")
    ncode = value.lower()
    return ncode, f"https://ncode.syosetu.com/{ncode}/"


def _epub_layout(archive):
    root = ET.fromstring(archive.read("META-INF/container.xml"))
    opf = next(e.attrib["full-path"] for e in root.iter() if e.tag.endswith("}rootfile"))
    package = ET.fromstring(archive.read(opf))
    items = {e.attrib["id"]: e for e in package.iter() if e.tag.endswith("}item")}
    spine = [items[e.attrib["idref"]] for e in package.iter() if e.tag.endswith("}itemref")]
    return opf, package, items, spine


def _member(base: str, href: str) -> str:
    parts = urlsplit(href)
    if parts.scheme or parts.netloc:
        raise ValueError("external EPUB resource")
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parts.path)))
    if path.startswith("../") or path.startswith("/"):
        raise ValueError("EPUB resource escapes archive")
    return path


def inspect_reference(path: str | Path, *, max_characters: int = 8000, encoding: str | None = None) -> dict:
    """Read EPUB metadata and a bounded head, or a TXT head, without importing a book."""
    if not 200 <= max_characters <= 50000:
        raise ValueError("max_characters must be between 200 and 50000")
    path = Path(path).expanduser().resolve()
    titles, authors, evidence, head = [], [], [], []
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            opf, package, _, spine = _epub_layout(archive)
            for e in package.iter():
                local = e.tag.rsplit("}", 1)[-1]
                text = strip_zero_width(e.text or "").strip()
                if local == "title" and text:
                    titles.append(text)
                if local == "creator" and text:
                    role = next((v for k, v in e.attrib.items() if k.rsplit("}", 1)[-1] == "role"), "")
                    evidence.append({"value": text, "source": "epub.creator", "role": role})
                    if role in {"", "aut", "author"}:
                        authors.append(text)
            for item in spine[:16]:
                if item.get("media-type") not in {"application/xhtml+xml", "text/html"}:
                    continue
                resource = _member(opf, item.attrib["href"])
                # A malformed giant first resource must not defeat the bounded preview.
                if archive.getinfo(resource).file_size > 8 * 1024 * 1024:
                    raise ValueError("head resource too large; use the input repair tools")
                soup = BeautifulSoup(archive.read(resource), "html.parser")
                for unwanted in soup(["head", "script", "style", "nav", "rt", "rp"]):
                    unwanted.decompose()
                text = strip_zero_width(soup.get_text("\n", strip=True))
                if text:
                    remaining = max_characters - sum(len(x["text"]) for x in head)
                    head.append({"resource": resource, "text": text[:remaining]})
                if sum(len(x["text"]) for x in head) >= max_characters:
                    break
    elif path.suffix.lower() in {".txt", ".text"}:
        # Decode whole-file bytes only in the importer; a preview needs at most a bounded prefix.
        with path.open("rb") as stream:
            raw = stream.read(max_characters * 4 + 4)
        if encoding:
            text = codecs.getincrementaldecoder(encoding)(errors="strict").decode(raw, final=False)
        else:
            # Avoid treating a UTF-8 character cut at the prefix boundary as an encoding error.
            try:
                text = codecs.getincrementaldecoder("utf-8-sig")(errors="strict").decode(raw, final=False)
            except UnicodeDecodeError:
                text, codec, issues = decode_text(raw)
                if any(i.get("code") == "encoding_ambiguous" for i in issues):
                    raise ValueError("reference head encoding is ambiguous; specify encoding")
        head.append({"resource": "text-head", "text": strip_zero_width(text)[:max_characters]})
        titles.append(path.stem)
    else:
        raise ValueError("reference must be EPUB or TXT")
    text = "\n".join(x["text"] for x in head)
    for match in re.finditer(r"(?im)^(?:作者|著者|原著|原作者|author)\s*[:：]\s*([^\n]{1,80})$", text):
        author = match[1].strip()
        authors.append(author)
        evidence.append({"value": author, "source": "head.author_label", "excerpt": match[0]})
    original_titles = [m[1].strip() for m in re.finditer(
        r"(?im)^(?:原題|原名|日文名|日文原名|原作名)\s*[:：]\s*([^\n]{1,160})$", text)]
    return {"schema": "syosetu-reference-head-v1", "path": str(path), "titles": _unique(titles),
            "author_candidates": _unique(authors), "author_evidence": evidence,
            "original_titles": _unique(original_titles), "head": head,
            "note": "Creator and labelled names are candidates; verify author versus translator/illustrator."}


def _author_variants(value: str) -> list[str]:
    variants = [unicodedata.normalize("NFKC", value).strip()]
    try:
        from opencc import OpenCC
        variants.append(OpenCC("s2t").convert(variants[0]))
    except ImportError:
        pass
    return _unique(variants)


def _work(row: dict) -> dict:
    ncode, url = canonical_work(row["ncode"])
    return {"ncode": ncode, "url": url, "title": unescape(str(row.get("title", ""))),
            "author": unescape(str(row.get("writer", ""))), "author_id": row.get("userid"),
            "synopsis": unescape(str(row.get("story", ""))),
            # With of=nt, Narou deliberately calls this 'noveltype' instead of 'novel_type'.
            "episode_count": row.get("general_all_no"), "novel_type": row.get("noveltype", row.get("novel_type")),
            "complete": row.get("end") == 0, "updated_at": row.get("novelupdated_at")}


class NarouCatalog:
    """Official metadata API; optional proxy, bounded retries, and <=14-day cache."""
    def __init__(self, *, proxy: str | None = None, timeout: float = 20, retries: int = 1,
                 cache_dir: str | Path | None = None, client: httpx.Client | None = None):
        if timeout <= 0 or retries not in range(4):
            raise ValueError("invalid timeout or retries (0-3)")
        self.client = client or httpx.Client(proxy=proxy, timeout=httpx.Timeout(timeout, connect=min(timeout, 10)),
                                             follow_redirects=True, trust_env=proxy is None,
                                             headers={"User-Agent": "LightNovelTranslation/0.4 (Narou metadata)"})
        self.owns_client, self.retries = client is None, retries
        self.cache_dir = Path(cache_dir).expanduser().resolve() if cache_dir else None
        self._memory, self._last_request = {}, 0.0

    def close(self):
        if self.owns_client:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _query(self, **query) -> dict:
        params = {"out": "json", "of": "t-n-u-w-s-nt-e-ga-nu", "lim": 100, **query}
        key = sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        cached = self._memory.get(key)
        path = self.cache_dir / (key + ".json") if self.cache_dir else None
        if cached is None and path and path.is_file():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                cached = None
        if (isinstance(cached, dict) and cached.get("schema") == CATALOG_CACHE_SCHEMA
                and 0 <= time.time() - cached.get("fetched_at", 0) < API_CACHE_SECONDS):
            self._memory[key] = cached
            return {**cached["result"], "cached": True}
        for attempt in range(self.retries + 1):
            time.sleep(max(0, 1 - (time.monotonic() - self._last_request)))
            self._last_request = time.monotonic()
            try:
                response = self.client.get(API_URL, params=params)
                if response.status_code in {429, 500, 502, 503, 504, 525} and attempt < self.retries:
                    time.sleep(attempt + 1)
                    continue
                response.raise_for_status()
                rows = response.json()
                if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or "allcount" not in rows[0]:
                    raise ValueError("invalid API envelope")
                total = int(rows[0]["allcount"])
                result = {"query": params, "total": total, "truncated": total > len(rows) - 1,
                          "candidates": [_work(r) for r in rows[1:]], "cached": False}
                cached = {"schema": CATALOG_CACHE_SCHEMA, "fetched_at": time.time(), "result": result}
                self._memory[key] = cached
                if path:
                    _write_json(path, cached)
                return result
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                if attempt == self.retries:
                    raise TranslationError("source_search_failed", "Narou metadata request failed", retryable=True) from None
        raise AssertionError("unreachable")

    def author(self, name: str) -> dict:
        name = name.strip()
        if not name:
            raise ValueError("author is empty")
        result = self._query(word=name, wname=1, title=0, ex=0, keyword=0)
        return {**result, "exact_matches": [r for r in result["candidates"] if _name(r["author"]) == _name(name)]}

    def keywords(self, terms: list[str] | tuple[str, ...], *, order: str = "hyoka") -> dict:
        terms = _unique(terms)
        if not terms or len(terms) > 8:
            raise ValueError("provide 1-8 meaningful title keywords")
        if order not in {"hyoka", "new", "old", "weeklypoint", "monthlypoint"}:
            raise ValueError("unsupported catalogue order")
        attempts, candidates = [], {}
        for term in terms:
            # Newest-first can bury an established original under newer imitations.
            # Points improve bounded recall; they never prove cross-language identity.
            result = self._query(word=term, title=1, wname=0, ex=0, keyword=0, order=order)
            attempts.append({k: v for k, v in result.items() if k != "candidates"})
            for row in result["candidates"]:
                candidates[row["ncode"]] = row
        for row in candidates.values():
            row["matched_keywords"] = [t for t in terms if _name(t) in _name(row["title"])]
        return {"attempts": attempts, "candidates": sorted(candidates.values(),
                    key=lambda r: -len(r["matched_keywords"]))}

    def work(self, ncode_or_url: str) -> dict:
        ncode, _ = canonical_work(ncode_or_url)
        result = self._query(ncode=ncode, lim=1)
        if not result["candidates"]:
            raise TranslationError("source_not_found", "Ncode was not found in the normal-site API")
        return result["candidates"][0]

    def reverse_lookup(self, reference: dict | str | Path, *, title_keywords=(), author_aliases=()) -> dict:
        """Try full author names (including conservative character variants), then title terms."""
        reference = inspect_reference(reference) if isinstance(reference, (str, Path)) else reference
        authors = _unique([*reference.get("author_candidates", []), *author_aliases])
        terms = _unique([*reference.get("original_titles", []), *title_keywords])
        attempts, candidates = [], {}
        for author in _unique([v for a in authors for v in _author_variants(a)]):
            result = self.author(author)
            attempts.append({"stage": "author", "query": author, "total": result["total"],
                             "exact_count": len(result["exact_matches"]), "truncated": result["truncated"]})
            for row in result["exact_matches"]:
                candidates[row["ncode"]] = {**row, "matched_author_query": author}
        stage = "author_exact"
        if not candidates and terms:
            result = self.keywords(terms)
            candidates = {r["ncode"]: r for r in result["candidates"]}
            attempts.extend({"stage": "title_keyword", **a} for a in result["attempts"])
            stage = "title_keywords"
        if not candidates:
            stage = "external_lookup_required"
        for row in candidates.values():
            row["matched_keywords"] = [t for t in terms if _name(t) in _name(row["title"])]
        ranked = sorted(candidates.values(), key=lambda r: -len(r["matched_keywords"]))
        queries = _unique([f'{t} {a} 原作 小説家になろう' for t in reference.get("titles", [])[:1]
                           for a in (authors[:2] or [""])])
        return {"schema": "syosetu-reverse-lookup-v1", "strategy": stage, "reference": reference,
                "attempts": attempts, "candidates": ranked, "requires_selection": True,
                "external_search_queries": queries,
                "note": "Author/title retrieval is not identity confirmation. Compare synopsis and edition facts."}


class _ResponseSnapshot:
    """GET response bytes only; no headers/cookies/pickle. Existing snapshots are immutable."""
    def __init__(self, folder: Path, *, offline: bool, requests: SourceRequests | None = None):
        self.folder, self.offline = folder, offline
        self.requests, self.lock = requests, RLock()
        self.failures = []
        path = folder / "responses.json"
        self.index = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def install(self, fetcher):
        from fanficfare.fetchers.base_fetcher import FetcherResponse
        original = fetcher.do_request
        # Each worker owns its own FFF/requests session. Headers are not persisted.
        if self.requests and hasattr(fetcher, 'get_requests_session'):
            def remember_retry_after(response, *args, **kwargs):
                fetcher._lnt_retry_after = response.headers.get('Retry-After')
                fetcher._lnt_status = response.status_code
            fetcher.get_requests_session().hooks['response'].append(remember_retry_after)

        def request(method, url, **kwargs):
            if method != "GET":
                raise TranslationError("unsupported_source_request", "This wrapper supports public GET acquisition only")
            canonical = urldefrag(url)[0]
            key = sha256((method + " " + canonical).encode()).hexdigest()
            with self.lock:
                row = self.index.get(key)
            if row:
                try:
                    if row["file"] != key + ".bin":
                        raise ValueError("unexpected response snapshot path")
                    data = (self.folder / row["file"]).read_bytes()
                    if sha256(data).hexdigest() != row["sha256"]:
                        raise ValueError("response snapshot hash mismatch")
                except (OSError, KeyError, ValueError):
                    self.failures.append({"url": canonical, "code": "source_cache_corrupt"})
                    raise TranslationError("source_cache_corrupt", "Response snapshot is missing or corrupt") from None
                if self.requests:
                    self.requests.cached()
                return FetcherResponse(data, row["final_url"], True)
            if self.offline:
                self.failures.append({"url": canonical, "code": "source_cache_miss"})
                raise TranslationError("source_cache_miss", "Offline response snapshot is incomplete",
                                       details={"url": canonical})
            try:
                response = (self.requests.request(original, fetcher, method, url, **kwargs)
                            if self.requests else original(method, url, **kwargs))
            except Exception as exc:
                with self.lock:
                    self.failures.append({"url": canonical, "code": exc.code if isinstance(exc, TranslationError)
                                          else "source_request_failed"})
                if self.requests:
                    self.requests.abort()
                raise
            data = response.content
            # Serialize commits, not network calls; responses.json has one atomic writer.
            with self.lock:
                self.folder.mkdir(parents=True, exist_ok=True)
                filename = key + ".bin"
                (self.folder / filename).write_bytes(data)
                self.index[key] = {"url": canonical, "final_url": urldefrag(response.redirecturl or canonical)[0],
                                   "file": filename, "sha256": sha256(data).hexdigest(),
                                   "bytes": len(data), "fetched_at": time.time()}
                _write_json(self.folder / "responses.json", self.index)
            return response

        fetcher.do_request = request


def _prefetch_chapters(chapters, snapshot, make_config, requests):
    """Bounded byte prefetch; adapter parsing and EPUB assembly remain ordered."""
    worker = local()
    fetchers = []
    lock = RLock()

    def fetch(url):
        if not hasattr(worker, 'fetcher'):
            worker.config = make_config()
            worker.fetcher = worker.config.get_fetcher()
            with lock:
                fetchers.append(worker.fetcher)
            snapshot.install(worker.fetcher)
        worker.fetcher.do_request('GET', worker.fetcher.condition_url(url))

    urls = iter(dict.fromkeys(chapter['url'] for chapter in chapters))
    try:
        with ThreadPoolExecutor(max_workers=requests.concurrency, thread_name_prefix='syosetu') as pool:
            pending = {pool.submit(fetch, url) for url in [next(urls, None) for _ in range(requests.concurrency)] if url}
            try:
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    # Check all completed futures before submitting replacements.
                    for future in done:
                        future.result()
                    for _ in done:
                        url = next(urls, None)
                        if url:
                            pending.add(pool.submit(fetch, url))
            except Exception:
                requests.abort()
                for future in pending:
                    future.cancel()
                raise
    finally:
        for fetcher in fetchers:
            session = getattr(fetcher, 'requests_session', None)
            if session is not None:
                session.close()


def _clean_markup(text: str) -> tuple[str, Counter]:
    counts = Counter(zero_width_counts(text))
    text = strip_zero_width(text)

    def entity(match):
        decoded = unescape(match[0])
        if decoded != match[0] and decoded and not strip_zero_width(decoded):
            counts.update(zero_width_counts(decoded))
            return ""
        return match[0]
    return re.sub(r"&(?:#[xX][0-9a-fA-F]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);", entity, text), counts


def _clean_epub(source: Path, destination: Path) -> dict:
    counts = Counter()
    with zipfile.ZipFile(source) as old, zipfile.ZipFile(destination, "w") as new:
        new.writestr("mimetype", old.read("mimetype"), compress_type=zipfile.ZIP_STORED)
        for item in old.infolist():
            if item.filename == "mimetype":
                continue
            data = old.read(item.filename)
            if PurePosixPath(item.filename).suffix.lower() in {".xhtml", ".html", ".opf", ".ncx", ".xml", ".css"}:
                text, removed = _clean_markup(data.decode("utf-8"))
                counts.update(removed)
                data = text.encode("utf-8")
            new.writestr(item, data)
    return dict(counts)


def validate_source_epub(path: str | Path, *, expected_chapters: int | None = None) -> dict:
    """Validate a FanFicFare EPUB and its chapter/resource coverage, without an LLM."""
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError("EPUB CRC error")
        opf, package, items, spine = _epub_layout(archive)
        names = set(archive.namelist())
        for item in items.values():
            if _member(opf, item.attrib["href"]) not in names:
                raise ValueError("EPUB manifest resource missing")
        chapters = [n for n in names if re.search(r"(?:^|/)file[0-9]+\.xhtml$", n)]
        if not chapters or expected_chapters is not None and len(chapters) != expected_chapters:
            raise ValueError("EPUB chapter count mismatch")
        for name in chapters:
            data = archive.read(name)
            ET.fromstring(data)
            soup = BeautifulSoup(data, "html.parser")
            for heading in soup(["head", "h1", "h2", "h3", "h4", "h5", "h6", "style", "script", "rt", "rp"]):
                heading.decompose()
            if not soup.get_text("", strip=True) and not soup.find(["img", "svg"]):
                raise ValueError("EPUB contains empty chapter")
            for image in soup.find_all("img", src=True):
                if _member(name, image["src"]) not in names:
                    raise ValueError("EPUB chapter image missing")
        controls = Counter()
        for name in names:
            if PurePosixPath(name).suffix.lower() in {".xhtml", ".html", ".opf", ".ncx", ".xml"}:
                _, removed = _clean_markup(archive.read(name).decode("utf-8"))
                controls.update(removed)
        if controls:
            raise ValueError("EPUB still contains zero-width controls")
        return {"epub_version": package.attrib["version"], "chapter_count": len(chapters),
                "spine_count": len(spine), "crc_ok": True, "zero_width_controls": {}}


def download_work(ncode_or_url: str, output_dir: str | Path, *, first_chapter: int = 1,
                  last_chapter: int | None = None, proxy: str | None = None, timeout: float = 20,
                  retries: int = 1, request_interval: float | None = None, concurrency: int | None = None,
                  config: str | Path | SyosetuConfig | None = None, include_images: bool = True,
                  include_author_notes: bool = True, cache_dir: str | Path | None = None,
                  offline: bool = False, selection_evidence: dict | None = None) -> dict:
    """Write immutable raw/clean EPUBs, response snapshots and a source manifest.

    The cleaned ``epub_path`` is directly accepted by ``TranslationProject.import_book``.
    Use a new output directory for an update; reuse cache_dir only for the same snapshot.
    """
    ncode, url = canonical_work(ncode_or_url)
    options = config if isinstance(config, SyosetuConfig) else SyosetuConfig.load(config)
    options = SyosetuConfig(concurrency=options.concurrency if concurrency is None else concurrency,
                            request_interval=options.request_interval if request_interval is None else request_interval)
    concurrency, request_interval = options.concurrency, options.request_interval
    if type(first_chapter) is not int or first_chapter < 1 or (last_chapter is not None and
            (type(last_chapter) is not int or last_chapter < first_chapter)):
        raise ValueError("chapter range is inclusive and 1-based")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0 or type(retries) is not int or retries not in range(4):
        raise ValueError("invalid request limits")
    try:
        from fanficfare import adapters, writers
        from fanficfare.configurable import Configuration
    except ImportError:
        raise TranslationError("source_dependency_missing", "Install lightnovel-translation[syosetu] before downloading") from None
    output = Path(output_dir).expanduser().resolve()
    if any((output / name).exists() for name in ("source.epub", "raw.epub", "manifest.json")):
        raise TranslationError("source_output_exists", "Use a new output directory; acquisition snapshots are immutable")
    output.mkdir(parents=True, exist_ok=True)
    cache = Path(cache_dir).expanduser().resolve() if cache_dir else output / "cache"
    manifest = {"schema": "syosetu-source-v1", "status": "started", "ncode": ncode, "url": url,
                "fanficfare_version": version("FanFicFare"), "offline": offline,
                "cache_dir": str(cache), "requested_range": [first_chapter, last_chapter],
                "selection_evidence": selection_evidence,
                "options": {"include_images": include_images, "include_author_notes": include_author_notes,
                            "concurrency": concurrency, "request_interval": request_interval}}
    requests = SourceRequests(concurrency, request_interval, retries)
    fetcher = None
    fff_logger = logging.getLogger("fanficfare")
    previous_level = fff_logger.level
    # FFF defaults to DEBUG, including session proxy configuration. Keep it out of tool output.
    fff_logger.setLevel(logging.WARNING)
    try:
        work_path = cache / "work.json"
        if work_path.is_file():
            work = json.loads(work_path.read_text(encoding="utf-8"))
            if work.get("ncode") != ncode:
                raise ValueError("cache belongs to another work")
        elif offline:
            raise TranslationError("source_cache_miss", "Offline work metadata is missing")
        else:
            with NarouCatalog(proxy=proxy, timeout=timeout, retries=retries) as catalog:
                work = catalog.work(ncode)
            _write_json(work_path, work)
        defaults = files("fanficfare").joinpath("defaults.ini").read_text(encoding="utf-8")
        settings = {"include_images": str(include_images).lower(), "include_author_notes": str(include_author_notes).lower(),
                    "epub_version": "3.0", "include_titlepage": "false", "include_tocpage": "false",
                    "continue_on_chapter_error": "false", "slow_down_sleep_time": "0",
                    "connect_timeout": str(timeout), "max_request_retries": "0",
                    "prepend_section_titles": "firstepisode",
                    # Preserve the site's original image bytes.  FanFicFare's
                    # Pillow conversion path can reject valid PNGs with an
                    # alpha channel (for example mode LA) when converting to
                    # JPEG, silently replacing them with ``failedtoload``.
                    # The acquisition wrapper validates that the referenced
                    # resource is present, so conversion is unnecessary here.
                    "no_image_processing": "true"}
        if proxy:
            settings.update(http_proxy=proxy, https_proxy=proxy)
        def make_config():
            result = Configuration(["syosetu.com"], "epub")
            result.read_file(StringIO(defaults))
            if not result.has_section('overrides'):
                result.add_section('overrides')
            for key, value in settings.items():
                result.set('overrides', key, value)
            if proxy:
                result.get_fetcher().get_requests_session().trust_env = False
            return result
        fff_config = make_config()
        fetcher = fff_config.get_fetcher()
        snapshot = _ResponseSnapshot(cache / "responses", offline=offline, requests=requests)
        snapshot.install(fetcher)
        adapter = adapters.getAdapter(fff_config, url)
        adapter.setChaptersRange(str(first_chapter), str(last_chapter) if last_chapter else None)
        story = adapter.getStoryMetadataOnly()
        # Upstream 4.62.16 compares a Tag to '完結済'; the official API is authoritative here.
        story.setMetadata("status", "Completed" if work["complete"] else "In-Progress")
        toc = adapter.get_chapters()
        end = last_chapter if last_chapter is not None else len(toc)
        if not first_chapter <= end <= len(toc):
            raise ValueError("requested chapter range exceeds the current table of contents")
        chapters, section = [], None
        for ordinal, chapter in enumerate(toc, 1):
            raw_title = chapter["title"]
            match = re.match("^\u200b(.*?)\u3000\u200b(.*)$", raw_title, re.S)
            if match:
                section, episode_title = match.groups()
            else:
                episode_title = raw_title
            canonical, revision = urldefrag(chapter["url"])
            if first_chapter <= ordinal <= end:
                chapters.append({"source_ordinal": ordinal, "id": f"{ncode}:{ordinal}", "url": canonical,
                                 "revision_marker": revision, "title": strip_zero_width(episode_title),
                                 "section": strip_zero_width(section) if section else None,
                                 "display_title": strip_zero_width(raw_title)})
        if concurrency > 1 and not offline:
            _prefetch_chapters(chapters, snapshot, make_config, requests)
        raw_path = output / "raw.epub"
        with raw_path.open("xb") as stream:
            writers.getWriter("epub", fff_config, adapter).writeStory(outstream=stream)
        # FFF can catch image-fetch failures internally. A missing resource must still fail this wrapper.
        if snapshot.failures:
            raise TranslationError(snapshot.failures[0]["code"], "Source response or image retrieval failed",
                                   details={"failures": snapshot.failures}, retryable=not offline)
        if story.chapter_error_count:
            raise TranslationError("source_chapter_error", "FanFicFare reported failed chapters")
        temporary = output / "source.epub.partial"
        cleaned = _clean_epub(raw_path, temporary)
        validation = validate_source_epub(temporary, expected_chapters=len(chapters))
        epub_path = output / "source.epub"
        temporary.replace(epub_path)
        manifest.update(status="complete", work=work, chapters=chapters, cleanup_counts=cleaned,
                        validation=validation, response_count=len(snapshot.index), download_stats=requests.summary(),
                        raw_epub_sha256=sha256(raw_path.read_bytes()).hexdigest(),
                        epub_sha256=sha256(epub_path.read_bytes()).hexdigest(), epub_path=str(epub_path))
        _write_json(output / "manifest.json", manifest)
        return {"ok": True, "epub_path": str(epub_path), "manifest_path": str(output / "manifest.json"),
                "cache_dir": str(cache), "chapter_count": len(chapters), "work": work, "validation": validation,
                "download_stats": requests.summary()}
    except Exception as exc:
        code = exc.code if isinstance(exc, TranslationError) else "source_download_failed"
        manifest.update(status="failed", error_code=code, download_stats=requests.summary(),
                        error=exc.to_dict() if isinstance(exc, TranslationError)
                        else {'exception_type': type(exc).__name__})
        _write_json(output / "manifest.json", manifest)
        if isinstance(exc, TranslationError):
            raise
        raise TranslationError(code, "Source acquisition failed; inspect the saved manifest and response snapshot",
                               details={"manifest_path": str(output / "manifest.json"),
                                        "exception_type": type(exc).__name__}, retryable=True) from None
    finally:
        fff_logger.setLevel(previous_level)
        session = getattr(fetcher, "requests_session", None)
        if session is not None:
            session.close()
