"""Source discovery, identity ambiguity and offline acquisition through real FFF."""
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit
import zipfile

import httpx
import pytest

from lightnovel_translation import TranslationConfig, TranslationError, TranslationProject, export_skill
from lightnovel_translation.syosetu import (
    NarouCatalog, canonical_work, download_work, inspect_reference, validate_source_epub,
)
from lightnovel_translation.cli import main
import lightnovel_translation.syosetu as module


def make_reference(path, *, author="马场翁"):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>')
        z.writestr("OEBPS/book.opf", '<package xmlns="http://www.idpf.org/2007/opf" '
                   'xmlns:dc="http://purl.org/dc/elements/1.1/" version="3.0"><metadata>'
                   f'<dc:title>转生成蜘蛛又怎样！</dc:title><dc:creator>{author}</dc:creator>'
                   '<dc:creator role="ill">插画家</dc:creator></metadata>'
                   '<manifest><item id="head" href="head.xhtml" media-type="application/xhtml+xml"/>'
                   '</manifest><spine><itemref idref="head"/></spine></package>')
        z.writestr("OEBPS/head.xhtml", f'<html><head><title>站点标题</title></head><body>'
                   f'<p>作者：{author}</p><p>插画：另一人</p><p>译者：译者姓名</p>'
                   '<p>未知世界转生成蜘蛛。</p></body></html>')
    return path


def row(ncode="N1234AB", *, author="馬場翁", title="蜘蛛ですが、なにか？"):
    return {"ncode": ncode, "writer": author, "title": title, "story": "蜘蛛に転生した主人公。",
            "userid": 1, "novel_type": 1, "end": 0, "general_all_no": 101}


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(module.time, "sleep", lambda value: None)


def test_read_head_distinguishes_illustrator_and_author_with_provenance(tmp_path):
    result = inspect_reference(make_reference(tmp_path / "zh.epub"))
    assert result["titles"] == ["转生成蜘蛛又怎样！"]
    assert result["author_candidates"] == ["马场翁"]
    assert any(e["source"] == "head.author_label" and e["value"] == "马场翁"
               for e in result["author_evidence"])
    assert "译者姓名" not in result["author_candidates"]
    assert "插画家" not in result["author_candidates"]
    assert len("\n".join(r["text"] for r in result["head"])) <= 8001


def test_txt_head_is_bounded_without_cutting_utf8_or_ignoring_declared_errors(tmp_path):
    p = tmp_path / "book.txt"
    p.write_text("作者：某人\n" + "蜘蛛世界。" * 3000, encoding="utf-8")
    head = inspect_reference(p, max_characters=201)
    assert head["author_candidates"] == ["某人"]
    assert len(head["head"][0]["text"]) == 201
    assert "\ufffd" not in head["head"][0]["text"]
    with pytest.raises(UnicodeDecodeError):
        inspect_reference(p, encoding="ascii")


def test_author_first_variants_exact_name_and_multiple_work_ranking(tmp_path):
    pytest.importorskip("opencc")
    calls = []
    def handler(request):
        calls.append(dict(request.url.params))
        records = [] if request.url.params["word"] == "马场翁" else [
            row("N4444AB", title="別の作品"), row(), row("N5555AB", author="馬場翁さん")]
        return httpx.Response(200, json=[{"allcount": len(records)}, *records])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, NarouCatalog(client=client) as catalog:
        result = catalog.reverse_lookup(inspect_reference(make_reference(tmp_path / "zh.epub")), title_keywords=["蜘蛛"])
    assert [r["word"] for r in calls] == ["马场翁", "馬場翁"]
    assert all(c["wname"] == "1" and c["title"] == "0" for c in calls)
    assert result["strategy"] == "author_exact" and result["requires_selection"]
    assert [r["ncode"] for r in result["candidates"]] == ["n1234ab", "n4444ab"]
    assert result["candidates"][0]["matched_keywords"] == ["蜘蛛"]


def test_no_author_match_falls_back_to_agent_supplied_title_terms():
    calls = []
    def handler(request):
        calls.append(dict(request.url.params))
        records = [row()] if request.url.params.get("title") == "1" else []
        return httpx.Response(200, json=[{"allcount": len(records)}, *records])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, NarouCatalog(client=client) as catalog:
        result = catalog.reverse_lookup({"author_candidates": ["Unknown"], "titles": ["中文书名"]}, title_keywords=["蜘蛛"])
    assert result["strategy"] == "title_keywords"
    assert [c["word"] for c in calls] == ["Unknown", "蜘蛛"]
    assert result["candidates"][0]["ncode"] == "n1234ab"


def test_unknown_work_returns_external_search_handoff_not_invented_match():
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{"allcount": 0}]))) as client:
        with NarouCatalog(client=client) as catalog:
            result = catalog.reverse_lookup({"titles": ["中文书名"], "author_candidates": []}, title_keywords=["未知"])
    assert result["strategy"] == "external_lookup_required"
    assert result["candidates"] == []
    assert "中文书名" in result["external_search_queries"][0]


def test_catalog_cache_ttl_and_query_scope_keep_truncation_visible(tmp_path, monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[{"allcount": 105}, row()])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with NarouCatalog(client=client, cache_dir=tmp_path) as catalog:
            first = catalog.author("馬場翁")
        with NarouCatalog(client=client, cache_dir=tmp_path) as catalog:
            assert catalog.author("馬場翁")["cached"]
        assert len(requests) == 1 and first["truncated"] and first["total"] == 105
        cache = next(tmp_path.glob("*.json"))
        data = json.loads(cache.read_text(encoding="utf-8"))
        data["fetched_at"] -= module.API_CACHE_SECONDS + 1
        cache.write_text(json.dumps(data), encoding="utf-8")
        with NarouCatalog(client=client, cache_dir=tmp_path) as catalog:
            assert not catalog.author("馬場翁")["cached"]
        assert len(requests) == 2


def test_catalog_has_bounded_retries_and_sanitizes_proxy_exception():
    calls = []
    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("http://user:private-token@proxy.invalid", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, NarouCatalog(client=client, retries=1) as catalog:
        with pytest.raises(TranslationError) as caught:
            catalog.author("作者")
    assert len(calls) == 2
    assert "private-token" not in json.dumps(caught.value.to_dict())


def test_keyword_ties_keep_api_order_instead_of_sorting_by_unrelated_ncode():
    records = [row("N9999ZZ"), row("N0001AB", title="蜘蛛の別作品")]
    def handler(request):
        assert request.url.params["order"] == "hyoka"
        return httpx.Response(200, json=[{"allcount": 2}, *records])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client, NarouCatalog(client=client) as catalog:
        result = catalog.reverse_lookup({"titles": ["中文蜘蛛小说"]}, title_keywords=["蜘蛛"])
    assert [r["ncode"] for r in result["candidates"]] == ["n9999zz", "n0001ab"]


def test_api_projected_noveltype_name_is_normalized():
    record = row()
    record["noveltype"] = record.pop("novel_type")
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[{"allcount": 1}, record]))) as client:
        with NarouCatalog(client=client) as catalog:
            work = catalog.work("n1234ab")
    assert work["novel_type"] == 1 and work["complete"]


@pytest.mark.parametrize("value", ["https://example.com/n1234ab/", "../n1234ab", "n1234ab[1-2]", "n1234ab\nignore"])
def test_ncode_rejects_unrelated_urls_or_embedded_ranges(value):
    with pytest.raises(ValueError):
        canonical_work(value)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jYYQAAAAASUVORK5CYII=")


@pytest.fixture
def site(monkeypatch):
    pytest.importorskip("fanficfare")
    from fanficfare.fetchers.fetcher_requests import RequestsFetcher
    from fanficfare.fetchers.base_fetcher import FetcherResponse
    calls = []
    def request(self, method, url, **kwargs):
        calls.append(url)
        parsed = urlsplit(url)
        if parsed.hostname == "images.example.invalid":
            return FetcherResponse(PNG, url, False)
        if "/novelview/infotop/" in url:
            values = {"作者名": '<a href="https://mypage.syosetu.com/1/">馬場翁</a>', "あらすじ": "Offline fixture.",
                      "掲載日": "2020年 01月01日 00時00分", "最終更新日": "2020年 01月02日 00時00分",
                      "文字数": "12,345文字", "キーワード": "冒険 剣", "ジャンル": "ハイファンタジー〔ファンタジー〕"}
            text = ('<a href="https://ncode.syosetu.com/n1234ab/">Fixture</a>'
                    '<a href="https://mypage.syosetu.com/1/">作者マイページ</a>'
                    '<span class="p-infotop-type__type">完結済</span>'
                    '<span class="p-infotop-type__allep">全101エピソード</span><dl>'
                    + ''.join(f'<dt>{k}</dt><dd>{v}</dd>' for k, v in values.items()) + '</dl>')
        elif "?p=" in url:
            page = int(parsed.query.split("=")[1])
            bounds = range(1, 101) if page == 1 else range(101, 102)
            text = '<div class="p-eplist">' + ('<div class="p-eplist__chapter-title">第一部</div>' if page == 1 else '')
            text += ''.join(f'<div class="p-eplist__sublist"><a href="/n1234ab/{i}/">第{i}話</a>'
                            '<div class="p-eplist__update">2020/01/01 00:00<span title="2020/01/02 00:00">改稿</span>'
                            '</div></div>' for i in bounds) + '</div>'
        else:
            text = ('<div class="p-novel__text p-novel__text--preface"><p>前言</p></div>'
                    '<div class="p-novel__text"><p id="L1">本\u200b文&#x200b;。</p>'
                    '<p id="L2"><ruby>剣<rt>けん</rt></ruby>の物語。</p>'
                    '<p><img src="https://images.example.invalid/a.png"/></p></div>'
                    '<div class="p-novel__text p-novel__text--afterword"><p>後記</p></div>')
        return FetcherResponse(text.encode("utf-8"), url, False)
    monkeypatch.setattr(RequestsFetcher, "request", request)
    monkeypatch.setattr(NarouCatalog, "work", lambda self, ncode: {"ncode": "n1234ab", "title": "Fixture", "complete": True})
    return calls


def test_download_clean_epub_import_and_strict_offline_rebuild(tmp_path, site, monkeypatch):
    first = download_work("N1234AB", tmp_path / "online", first_chapter=100, last_chapter=101, request_interval=0)
    manifest = json.loads(Path(first["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "complete" and manifest["work"]["complete"]
    assert len(site) == 6  # info + two catalogue pages + two episodes + one image
    assert [c["source_ordinal"] for c in manifest["chapters"]] == [100, 101]
    assert all(c["section"] == "第一部" for c in manifest["chapters"])
    assert all("#" not in c["url"] and c["revision_marker"] for c in manifest["chapters"])
    assert manifest["cleanup_counts"]["U+200B"] >= 2
    with zipfile.ZipFile(first["epub_path"]) as epub:
        body = "\n".join(epub.read(n).decode("utf-8") for n in epub.namelist() if n.endswith(".xhtml"))
        assert "<ruby>" in body and "<rt>けん</rt>" in body and "<img" in body
        assert any(n.endswith(".png") for n in epub.namelist())
        assert any("Completed" in epub.read(n).decode("utf-8") for n in epub.namelist() if n.endswith(".opf"))
    def no_network(*args, **kwargs):
        pytest.fail("offline rebuild accessed network")
    from fanficfare.fetchers.fetcher_requests import RequestsFetcher
    monkeypatch.setattr(RequestsFetcher, "request", no_network)
    second = download_work("n1234ab", tmp_path / "offline", first_chapter=100, last_chapter=101,
                           cache_dir=first["cache_dir"], offline=True)
    assert validate_source_epub(second["epub_path"], expected_chapters=2)["zero_width_controls"] == {}
    with TranslationProject(tmp_path / "project", TranslationConfig()) as project:
        ja = project.import_book(second["epub_path"], edition_id="ja", language="ja")
        assert len(ja.chapters()) == 2
        assert "本文。" in ja.read(ja.chapters()[0])["text"]
        assert project.validate_inputs()["valid"]
    def content(path):
        with zipfile.ZipFile(path) as z:
            return {n: z.read(n) for n in z.namelist() if n.endswith((".png", ".xhtml"))}
    assert content(first["epub_path"]) == content(second["epub_path"])


def test_missing_image_cache_never_becomes_success_placeholder(tmp_path, site):
    first = download_work("n1234ab", tmp_path / "online", last_chapter=1, request_interval=0)
    cache = Path(first["cache_dir"])
    index_path = cache / "responses/responses.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    key = next(k for k, v in index.items() if "images.example.invalid" in v["url"])
    del index[key]
    index_path.write_text(json.dumps(index), encoding="utf-8")
    count = len(site)
    with pytest.raises(TranslationError) as caught:
        download_work("n1234ab", tmp_path / "missing", last_chapter=1, cache_dir=cache, offline=True)
    assert caught.value.code == "source_cache_miss" and len(site) == count
    assert not (tmp_path / "missing/source.epub").exists()
    assert json.loads((tmp_path / "missing/manifest.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_corrupt_cached_image_is_detected_even_if_upstream_discards_failed_images(tmp_path, site):
    first = download_work("n1234ab", tmp_path / "online", last_chapter=1, request_interval=0)
    cache = Path(first["cache_dir"])
    index = json.loads((cache / "responses/responses.json").read_text(encoding="utf-8"))
    image = next(row for row in index.values() if "images.example.invalid" in row["url"])
    (cache / "responses" / image["file"]).write_bytes(b"damaged")
    with pytest.raises(TranslationError) as caught:
        download_work("n1234ab", tmp_path / "corrupt", last_chapter=1, cache_dir=cache, offline=True)
    assert caught.value.code == "source_cache_corrupt"
    assert not (tmp_path / "corrupt/source.epub").exists()


def test_existing_snapshot_is_not_overwritten_and_range_errors_are_preserved(tmp_path, site):
    result = download_work("n1234ab", tmp_path / "ok", last_chapter=1, request_interval=0)
    with pytest.raises(TranslationError) as caught:
        download_work("n1234ab", tmp_path / "ok", last_chapter=1)
    assert caught.value.code == "source_output_exists"
    with pytest.raises(TranslationError):
        download_work("n1234ab", tmp_path / "range", first_chapter=102, cache_dir=result["cache_dir"], offline=True)
    assert json.loads((tmp_path / "range/manifest.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_cli_and_both_skills_remain_available_without_model_config(tmp_path, capsys):
    p = tmp_path / "zh.txt"
    p.write_text("作者：作者全名\n中文内容", encoding="utf-8")
    report = tmp_path / "report.json"
    assert main(["syosetu", "inspect-reference", str(p), "--output", str(report)]) == 0
    assert json.loads(report.read_text(encoding="utf-8"))["author_candidates"] == ["作者全名"]
    assert main(["syosetu", "inspect-reference", str(p), "--output", str(report)]) == 1
    assert export_skill(tmp_path / "old")["skill_dir"]
    result = export_skill(tmp_path / "new", name="syosetu-fetch")
    assert "references/acquisition.md" in result["files"]
    assert main(["skill", "export", str(tmp_path / "cli"), "--name", "syosetu-fetch"]) == 0
