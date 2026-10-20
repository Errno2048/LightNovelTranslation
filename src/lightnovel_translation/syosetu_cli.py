"""Credential-free source acquisition commands; translation settings are not loaded."""
from __future__ import annotations

import json
from pathlib import Path
import zipfile

from .errors import TranslationError
from .syosetu import NarouCatalog, download_work, inspect_reference


def add_parser(commands):
    parser = commands.add_parser("syosetu", help="Find and acquire a normal-site Narou Web novel")
    actions = parser.add_subparsers(dest="action", required=True)
    head = actions.add_parser("inspect-reference", help="Read bounded EPUB/TXT metadata and head locally")
    head.add_argument("path")
    head.add_argument("--encoding")
    head.add_argument("--max-characters", type=int, default=8000)
    search = actions.add_parser("search")
    query = search.add_mutually_exclusive_group(required=True)
    query.add_argument("--author")
    query.add_argument("--keywords", nargs="+")
    lookup = actions.add_parser("lookup", help="Author-first reverse lookup from a Chinese EPUB/TXT")
    lookup.add_argument("path")
    lookup.add_argument("--author-alias", action="append", default=[])
    lookup.add_argument("--keywords", nargs="*", default=[])
    lookup.add_argument("--encoding")
    metadata = actions.add_parser("metadata")
    metadata.add_argument("ncode")
    download = actions.add_parser("download")
    download.add_argument("ncode")
    download.add_argument("output_dir")
    download.add_argument("--first", type=int, default=1)
    download.add_argument("--last", type=int)
    download.add_argument("--interval", type=float, help="Minimum start interval shared by all workers (default: config or 1s)")
    download.add_argument("--concurrency", type=int, help="Chapter download workers, 1-8 (default: config or 2)")
    download.add_argument("--config", help="TOML/JSON containing [syosetu]; model settings are not initialized")
    download.add_argument("--offline", action="store_true")
    download.add_argument("--no-images", action="store_true")
    download.add_argument("--no-author-notes", action="store_true")
    for action in (head, search, lookup, metadata, download):
        action.add_argument("--output", help="Write full JSON to this file and print a small summary")
    for action in (search, lookup, metadata, download):
        action.add_argument("--proxy", help="HTTP(S) proxy, for example http://127.0.0.1:7890")
        action.add_argument("--timeout", type=float, default=20)
        action.add_argument("--retries", type=int, default=1)
        action.add_argument("--cache-dir")


def run(args):
    try:
        if args.action == "inspect-reference":
            result = inspect_reference(args.path, max_characters=args.max_characters, encoding=args.encoding)
        elif args.action == "download":
            result = download_work(args.ncode, args.output_dir, first_chapter=args.first, last_chapter=args.last,
                proxy=args.proxy, timeout=args.timeout, retries=args.retries, request_interval=args.interval,
                concurrency=args.concurrency, config=args.config,
                include_images=not args.no_images, include_author_notes=not args.no_author_notes,
                cache_dir=args.cache_dir, offline=args.offline)
        else:
            with NarouCatalog(proxy=args.proxy, timeout=args.timeout, retries=args.retries, cache_dir=args.cache_dir) as catalog:
                if args.action == "search":
                    result = catalog.author(args.author) if args.author else catalog.keywords(args.keywords)
                elif args.action == "lookup":
                    reference = inspect_reference(args.path, encoding=args.encoding)
                    result = catalog.reverse_lookup(reference, title_keywords=args.keywords, author_aliases=args.author_alias)
                else:
                    result = catalog.work(args.ncode)
        if args.output:
            path = Path(args.output).expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Reports may contain selection evidence: preserve previous versions.
            with path.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
            candidates = result.get("candidates", [])
            result = {"ok": True, "output_path": str(path),
                      **{k: result[k] for k in ("strategy", "requires_selection", "epub_path", "chapter_count") if k in result},
                      "candidate_count": len(candidates),
                      "candidates": [{k: row[k] for k in ("ncode", "title", "author", "matched_keywords") if k in row}
                                     for row in candidates[:10]]}
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 0
    except TranslationError as exc:
        print(json.dumps({"ok": False, "error": exc.to_dict()}, ensure_ascii=True, indent=2))
        return 1
    except (ValueError, OSError, KeyError, zipfile.BadZipFile) as exc:
        # Error type only: local paths/proxy credentials must not leak through raw exceptions.
        print(json.dumps({"ok": False, "error": {"code": "source_input_error", "exception_type": type(exc).__name__}}))
        return 1
