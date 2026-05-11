import builtins
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "itu_downloader.py"


def load_downloader_module():
    spec = importlib.util.spec_from_file_location("itu_downloader", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


downloader = load_downloader_module()


def run_script(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )


def import_missing(module_name):
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == module_name or name.startswith(f"{module_name}."):
            error = ModuleNotFoundError(f"No module named '{module_name}'")
            error.name = module_name
            raise error

        return real_import(name, globals, locals, fromlist, level)

    return fake_import


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.url = ""
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)

        for key, payload in self.responses.items():
            if key in url:
                return FakeResponse(payload)

        raise AssertionError(f"unexpected URL: {url}")


class FakeStreamResponse:
    def __init__(self, url, status_code=200, headers=None, chunks=None):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, chunk_size):
        yield from self.chunks


class FakeStreamClient:
    def __init__(self, response):
        self.response = response
        self.request_headers = None

    def stream(self, method, url, headers=None, follow_redirects=True):
        self.request_headers = headers or {}
        return self.response


class ItuArchiveDownloaderCliTests(unittest.TestCase):
    def test_default_output_root_is_research_archive(self):
        with mock.patch.object(sys, "argv", [str(SCRIPT)]):
            args = downloader.parse_args()

        self.assertEqual(args.out, "research/itu-archive")

    def test_help_does_not_require_optional_dependencies(self):
        result = run_script("--help")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--include-test-signals", result.stdout)
        self.assertIn("--profile", result.stdout)
        self.assertIn("--allow-list", result.stdout)
        self.assertIn("--download-all", result.stdout)
        self.assertNotIn("ModuleNotFoundError", result.stdout + result.stderr)

    def test_missing_mode_error_does_not_require_optional_dependencies(self):
        result = run_script()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Select at least one", result.stdout + result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stdout + result.stderr)


class ItuArchiveDownloaderDependencyTests(unittest.TestCase):
    def test_http_dependency_error_has_run_command(self):
        with mock.patch("builtins.__import__", new=import_missing("httpx")):
            with self.assertRaisesRegex(downloader.MissingDependencyError, "httpx"):
                downloader.require_httpx()

    def test_beautiful_soup_dependency_error_has_run_command(self):
        with mock.patch("builtins.__import__", new=import_missing("bs4")):
            with self.assertRaisesRegex(downloader.MissingDependencyError, "beautifulsoup4"):
                downloader.require_beautiful_soup()

    def test_playwright_dependency_error_has_run_command(self):
        with mock.patch("builtins.__import__", new=import_missing("playwright")):
            with self.assertRaisesRegex(downloader.MissingDependencyError, "playwright"):
                downloader.require_playwright()


class ItuArchiveDownloaderUrlTests(unittest.TestCase):
    def test_allowed_urls_are_restricted_to_itu_hosts(self):
        self.assertTrue(downloader.is_allowed_url("https://www.itu.int/path"))
        self.assertTrue(downloader.is_allowed_url("https://handle.itu.int/path"))
        self.assertFalse(downloader.is_allowed_url("https://www.itu.int.example/path"))

    def test_navigation_html_is_not_a_download_candidate(self):
        self.assertFalse(
            downloader.is_download_candidate("https://www.itu.int/ITU-T/index.html", "ITU-T")
        )

    def test_dologin_item_ids_infer_artifact_type(self):
        self.assertEqual(
            downloader.infer_artifact_type(
                "https://www.itu.int/rec/dologin_pub.asp?id=T-REC-V.1-198811-I!!PDF-E",
                "",
            ),
            "recommendation",
        )
        self.assertEqual(
            downloader.infer_artifact_type(
                "https://www.itu.int/rec/dologin_pub.asp?id=T-REC-V.1-198811-I!!MSW-E",
                "",
            ),
            "documents",
        )
        self.assertEqual(
            downloader.infer_artifact_type(
                "https://www.itu.int/rec/dologin_pub.asp?id=T-REC-V.1-198811-I!!ZWD-E",
                "",
            ),
            "documents",
        )

    def test_dologin_item_ids_produce_stable_filenames(self):
        self.assertEqual(
            downloader.filename_from_itu_item_id(
                "https://www.itu.int/rec/dologin_pub.asp?lang=e&id=T-REC-V.1-198811-I!!PDF-E&type=items"
            ),
            "T-REC-V.1-198811-I-PDF-E.pdf",
        )
        self.assertEqual(
            downloader.filename_from_itu_item_id(
                "https://www.itu.int/rec/dologin_pub.asp?lang=e&id=T-REC-G.722-201209-I!!SOFT-ZST-E&type=items"
            ),
            "T-REC-G.722-201209-I-SOFT-ZST-E.zip",
        )

    def test_non_english_dologin_artifacts_are_not_download_candidates(self):
        self.assertTrue(
            downloader.is_download_candidate(
                "https://www.itu.int/rec/dologin_pub.asp?lang=e&id=T-REC-G.711-198811-I!!PDF-E",
                "PDF",
            )
        )
        self.assertFalse(
            downloader.is_download_candidate(
                "https://www.itu.int/rec/dologin_pub.asp?lang=f&id=T-REC-G.711-198811-I!!PDF-F",
                "PDF",
            )
        )


class ItuArchiveDownloaderPolicyTests(unittest.TestCase):
    def test_codec_recommendation_file_matches_builtin_profile(self):
        loaded = downloader.load_allow_list(str(ROOT / "codec-recommendations.txt"))

        self.assertEqual(loaded, downloader.CODEC_RECOMMENDATION_SET)
        self.assertNotIn("V.1", loaded)
        self.assertIn("G.711", loaded)
        self.assertIn("H.266", loaded)

    def test_allow_list_parser_normalizes_comments_and_commas(self):
        parsed = downloader.parse_recommendation_list(
            """
            G.711, H Suppl. 21 # codec supplement
            P.863
            """
        )

        self.assertEqual(parsed, {"G.711", "H-Suppl-21", "P.863"})

    def test_codec_profile_derives_series_from_allow_list(self):
        args = SimpleNamespace(series="", download_all=False, profile="codecs", allow_list="")

        series = downloader.series_values_for_publications(
            args,
            downloader.CODEC_RECOMMENDATION_SET,
        )

        self.assertEqual(series, ["G", "H", "P", "T"])
        self.assertNotIn("V", series)

    def test_download_all_uses_old_broad_series_policy(self):
        args = SimpleNamespace(series="", download_all=True, profile="codecs", allow_list="")
        allowed = downloader.selected_recommendations(args)

        self.assertIsNone(allowed)
        self.assertEqual(
            downloader.series_values_for_publications(args, allowed),
            list(downloader.BROAD_SERIES),
        )

    def test_filter_pages_by_recommendations_rejects_non_codec_pages(self):
        codec = downloader.PageRecord(
            collection="publications",
            url="https://www.itu.int/rec/T-REC-G.711-198811-I",
            title="Pulse code modulation",
            series="G",
            recommendation="G.711",
            edition="1988-11",
            page_id="g.711-1988-11",
        )
        unrelated = downloader.PageRecord(
            collection="publications",
            url="https://www.itu.int/rec/T-REC-G.801-201608-I",
            title="Digital transmission models",
            series="G",
            recommendation="G.801",
            edition="2016-08",
            page_id="g.801-2016-08",
        )

        accepted, rejected = downloader.filter_pages_by_recommendations(
            [unrelated, codec],
            downloader.CODEC_RECOMMENDATION_SET,
        )

        self.assertEqual([page.recommendation for page in accepted], ["G.711"])
        self.assertEqual(rejected[0]["recommendation"], "G.801")
        self.assertEqual(rejected[0]["reason"], "not-in-active-recommendation-allow-list")


class ItuArchiveDownloaderPathTests(unittest.TestCase):
    def test_publication_path_uses_codec_archive_domain(self):
        page = downloader.PageRecord(
            collection="publications",
            url="https://www.itu.int/rec/T-REC-G.711-199303-I",
            title="Pulse code modulation",
            series="G",
            recommendation="G.711",
            edition="1993-03",
            page_id="g.711-1993-03",
        )

        path = downloader.target_directory(Path("/archive"), page, "recommendation")

        self.assertEqual(
            path,
            Path("/archive") / "standards" / "audio-speech" / "G.711" / "latest" / "recommendation",
        )

    def test_publication_path_keeps_explicit_edition_bucket(self):
        page = downloader.PageRecord(
            collection="publications",
            url="https://www.itu.int/rec/T-REC-H.264-201906-I",
            title="Advanced video coding",
            series="H",
            recommendation="H.264",
            edition="2019-06",
            page_id="h.264-2019-06",
            edition_group="2019-06",
        )

        path = downloader.target_directory(Path("/archive"), page, "recommendation")

        self.assertEqual(
            path,
            Path("/archive") / "standards" / "video" / "H.264" / "2019-06" / "recommendation",
        )

    def test_test_signal_path_uses_recommendation_bucket(self):
        page = downloader.PageRecord(
            collection="test-signals",
            url="https://www.itu.int/myworkspace/t-signals/vectors?val=9",
            title="Coding of speech at 8 kbit/s",
            series="P",
            recommendation="P.50",
            edition="2001-01",
            page_id="ts-9",
        )

        path = downloader.target_directory(Path("/archive"), page, "test-vectors")

        self.assertEqual(
            path,
            Path("/archive") / "test-signals" / "by-recommendation" / "P.50" / "test-vectors",
        )


class ItuArchiveDownloaderResumeTests(unittest.TestCase):
    def test_completed_download_requires_existing_file_with_expected_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / "artifact.zip"
            artifact.write_bytes(b"abc")

            self.assertTrue(
                downloader.completed_download_available(
                    {
                        "status": "downloaded",
                        "output_path": str(artifact),
                        "size_bytes": 3,
                    }
                )
            )
            self.assertFalse(
                downloader.completed_download_available(
                    {
                        "status": "downloaded",
                        "output_path": str(artifact),
                        "size_bytes": 4,
                    }
                )
            )
            self.assertFalse(
                downloader.completed_download_available(
                    {
                        "status": "downloaded",
                        "output_path": str(Path(tmp) / "missing.zip"),
                        "size_bytes": 3,
                    }
                )
            )

    def test_load_completed_downloads_ignores_stale_manifest_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "artifact.zip"
            artifact.write_bytes(b"abc")
            manifest = root / "manifest.jsonl"
            rows = [
                {
                    "status": "downloaded",
                    "source_url": "https://www.itu.int/good.zip",
                    "output_path": str(artifact),
                    "size_bytes": 3,
                },
                {
                    "status": "downloaded",
                    "source_url": "https://www.itu.int/stale.zip",
                    "output_path": str(root / "missing.zip"),
                    "size_bytes": 3,
                },
                {
                    "status": "dry-run",
                    "source_url": "https://www.itu.int/dry.zip",
                    "output_path": str(root / "dry.zip"),
                    "size_bytes": 0,
                },
            ]
            manifest.write_text(
                "\n".join(json.dumps(row) for row in rows) + "\n",
                encoding="utf-8",
            )

            completed = downloader.load_completed_downloads(manifest)

            self.assertEqual(set(completed), {"https://www.itu.int/good.zip"})

    def test_dedupe_assets_keeps_one_download_per_source_url(self):
        page = downloader.PageRecord(
            collection="test-signals",
            url="https://www.itu.int/myworkspace/t-signals/vectors?val=1",
            title="G.722",
            series="G",
            recommendation="G.722",
            edition="2014-10",
            page_id="ts-1",
        )
        assets = [
            downloader.AssetRecord(page, "https://www.itu.int/file.zip", "one"),
            downloader.AssetRecord(page, "https://www.itu.int/file.zip", "duplicate"),
            downloader.AssetRecord(page, "https://www.itu.int/other.zip", "other"),
        ]

        deduped = downloader.dedupe_assets(assets)

        self.assertEqual(
            [asset.source_url for asset in deduped],
            [
                "https://www.itu.int/file.zip",
                "https://www.itu.int/other.zip",
            ],
        )

    def test_download_artifact_resumes_existing_part_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            page = downloader.PageRecord(
                collection="test-signals",
                url="https://www.itu.int/myworkspace/t-signals/vectors?val=1",
                title="G.722",
                series="G",
                recommendation="G.722",
                edition="2014-10",
                page_id="ts-1",
            )
            output_dir = downloader.target_directory(root, page, "test-vectors")
            output_dir.mkdir(parents=True)
            partial = output_dir / "file.zip.part"
            partial.write_bytes(b"abc")
            source_url = "https://www.itu.int/file.zip"
            client = FakeStreamClient(
                FakeStreamResponse(
                    source_url,
                    status_code=206,
                    headers={"content-type": "application/zip"},
                    chunks=[b"def"],
                )
            )

            record = downloader.download_artifact(
                client=client,
                root=root,
                page=page,
                source_url=source_url,
                link_text="test vectors",
                delay=0,
                dry_run=False,
            )

            self.assertEqual(client.request_headers, {"Range": "bytes=3-"})
            self.assertEqual(Path(record.output_path).read_bytes(), b"abcdef")
            self.assertFalse(partial.exists())
            self.assertEqual(record.size_bytes, 6)

    def test_download_artifact_honors_shutdown_before_start(self):
        event = threading.Event()
        event.set()
        page = downloader.PageRecord(
            collection="test-signals",
            url="https://www.itu.int/myworkspace/t-signals/vectors?val=1",
            title="G.722",
            series="G",
            recommendation="G.722",
            edition="2014-10",
            page_id="ts-1",
        )

        with self.assertRaises(downloader.DownloadInterrupted):
            downloader.download_artifact(
                client=object(),
                root=Path("/archive"),
                page=page,
                source_url="https://www.itu.int/file.zip",
                link_text="test vectors",
                delay=0,
                dry_run=False,
                shutdown_event=event,
            )


class ItuArchiveDownloaderApiDiscoveryTests(unittest.TestCase):
    def test_discovers_test_signal_pages_from_mws_api(self):
        client = FakeClient(
            {
                "/api/testsignals/allsignals": [
                    {
                        "Recommendation": "ITU-T P.50",
                        "Title": "Artificial voices",
                        "ts_id": 14,
                    }
                ]
            }
        )

        pages = downloader.discover_test_signal_pages(client)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].recommendation, "P.50")
        self.assertEqual(pages[0].page_id, "ts-14")
        self.assertIn("val=14", pages[0].url)

    def test_enriches_test_signal_page_from_mws_files_api(self):
        client = FakeClient(
            {
                "/api/testsignals/signalfiles": [
                    {
                        "Edition": "P.50 (09/1999)",
                        "File description": "Old speech test vectors",
                        "file_full_path": "https://www.itu.int/wftp3/public/t/testsignal/P50-old.zip",
                    },
                    {
                        "Edition": "P.50 (01/2001)",
                        "File description": "Latest speech test vectors",
                        "file_full_path": "https://www.itu.int/wftp3/public/t/testsignal/P50-latest.zip",
                    },
                ]
            }
        )
        page = downloader.PageRecord(
            collection="test-signals",
            url="https://www.itu.int/myworkspace/t-signals/vectors?val=14",
            title="Artificial voices",
            series="P",
            recommendation="P.50",
            edition="unknown-edition",
            page_id="ts-14",
        )

        enriched, downloads = downloader.enrich_page(page, 1_000, client)

        self.assertEqual(enriched.edition, "2001-01")
        self.assertEqual(
            downloads,
            [
                (
                    "https://www.itu.int/wftp3/public/t/testsignal/P50-latest.zip",
                    "Latest speech test vectors",
                )
            ],
        )

    def test_can_keep_all_test_signal_editions(self):
        client = FakeClient(
            {
                "/api/testsignals/signalfiles": [
                    {
                        "Edition": "P.50 (09/1999)",
                        "File description": "Old speech test vectors",
                        "file_full_path": "https://www.itu.int/wftp3/public/t/testsignal/P50-old.zip",
                    },
                    {
                        "Edition": "P.50 (01/2001)",
                        "File description": "Latest speech test vectors",
                        "file_full_path": "https://www.itu.int/wftp3/public/t/testsignal/P50-latest.zip",
                    },
                ]
            }
        )
        page = downloader.PageRecord(
            collection="test-signals",
            url="https://www.itu.int/myworkspace/t-signals/vectors?val=14",
            title="Artificial voices",
            series="P",
            recommendation="P.50",
            edition="unknown-edition",
            page_id="ts-14",
        )

        enriched, downloads = downloader.enrich_page(page, 1_000, client, latest_only=False)

        self.assertEqual(enriched.edition, "multiple-editions")
        self.assertEqual(len(downloads), 2)

    def test_discovers_publications_from_mws_recommendations_api(self):
        client = FakeClient(
            {
                "/api/recommendations/searchRecs": {
                    "Total": 2,
                    "Data": [
                        {
                            "rec_name": "G.711 (11/1988)",
                            "title": "Pulse code modulation",
                            "approval_date": "1988-11-25",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-198811-I",
                        },
                        {
                            "rec_name": "G.711 (03/1993)",
                            "title": "Pulse code modulation",
                            "approval_date": "1993-03-12",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-199303-I",
                        },
                    ],
                }
            }
        )

        pages = downloader.discover_publication_pages(["G"], client)

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].recommendation, "G.711")
        self.assertEqual(pages[0].edition, "1993-03")
        self.assertEqual(pages[0].url, "https://www.itu.int/rec/T-REC-G.711-199303-I")

    def test_can_keep_all_publication_editions(self):
        client = FakeClient(
            {
                "/api/recommendations/searchRecs": {
                    "Total": 2,
                    "Data": [
                        {
                            "rec_name": "G.711 (11/1988)",
                            "title": "Pulse code modulation",
                            "approval_date": "1988-11-25",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-198811-I",
                        },
                        {
                            "rec_name": "G.711 (03/1993)",
                            "title": "Pulse code modulation",
                            "approval_date": "1993-03-12",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-199303-I",
                        },
                    ],
                }
            }
        )

        pages = downloader.discover_publication_pages(["G"], client, latest_only=False)

        self.assertEqual([page.edition for page in pages], ["1988-11", "1993-03"])

    def test_skips_inactive_publication_records_by_default(self):
        client = FakeClient(
            {
                "/api/recommendations/searchRecs": {
                    "Total": 2,
                    "Data": [
                        {
                            "rec_name": "G.711 (11/1988)",
                            "title": "Pulse code modulation",
                            "approval_date": "1988-11-25",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-198811-I",
                            "status": "Superseded",
                        },
                        {
                            "rec_name": "G.722 (09/2012)",
                            "title": "7 kHz audio-coding",
                            "approval_date": "2012-09-13",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.722-201209-I",
                            "status": "In force",
                        },
                    ],
                }
            }
        )

        pages = downloader.discover_publication_pages(["G"], client)

        self.assertEqual([page.recommendation for page in pages], ["G.722"])

    def test_can_keep_inactive_publication_records_for_all_editions(self):
        client = FakeClient(
            {
                "/api/recommendations/searchRecs": {
                    "Total": 1,
                    "Data": [
                        {
                            "rec_name": "G.711 (11/1988)",
                            "title": "Pulse code modulation",
                            "approval_date": "1988-11-25",
                            "dms_link": "https://www.itu.int/rec/T-REC-G.711-198811-I",
                            "status": "Superseded",
                        },
                    ],
                }
            }
        )

        pages = downloader.discover_publication_pages(["G"], client, latest_only=False)

        self.assertEqual([page.recommendation for page in pages], ["G.711"])


if __name__ == "__main__":
    unittest.main()
