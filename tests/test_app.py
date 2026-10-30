import gzip
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

APP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py"
)

FIXTURE_HTML = """<!DOCTYPE html>
<html><head>
<title>サンプルページ</title>
<meta property="og:video" content="https://cdn.example.com/og.mp4">
<meta name="twitter:player:stream" content="https://cdn.example.com/tw.webm">
<script type="application/ld+json">{"@type":"VideoObject","contentUrl":"https://cdn.example.com/ld.mp4"}</script>
</head><body>
<video src="/media/main.mp4" controls></video>
<video controls><source src="media/sub.webm" type="video/webm"></video>
<video src="/media/main.mp4"></video>
<a href="https://example.com/files/linked.mov">動画リンク</a>
<a href="https://example.com/page.html">普通のリンク</a>
</body></html>
"""


def load_app(db_path):
    os.environ["VIDEODOWNLOADER_DB_PATH"] = db_path
    spec = importlib.util.spec_from_file_location("videodownloader_app", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VideodownloaderTest(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.mod = load_app(self.db_path)
        self.client = self.mod.app.test_client()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_index_ok(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("videodownloader", body)
        self.assertIn("動画を探す", body)

    def test_healthz(self):
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json(), {"status": "ok"})

    def test_validate_rejects_bad_urls(self):
        for bad in ["", "   ", "ftp://example.com/a.mp4", "example.com/a.mp4",
                    "https://", "https://user:pass@example.com/a.mp4"]:
            with self.assertRaises(ValueError, msg=bad):
                self.mod.validate_url(bad)

    def test_validate_rejects_private_hosts(self):
        for bad in ["http://localhost/a.mp4", "http://127.0.0.1/a.mp4",
                    "http://192.168.1.10/a.mp4", "http://10.0.0.5/a.mp4",
                    "http://[::1]/a.mp4", "http://printer.local/a.mp4"]:
            with self.assertRaises(ValueError, msg=bad):
                self.mod.validate_url(bad)

    def test_validate_accepts_public(self):
        self.assertEqual(self.mod.validate_url(" https://example.com/a.mp4 "),
                         "https://example.com/a.mp4")

    def test_extract_videos(self):
        title, cands = self.mod.extract_videos(FIXTURE_HTML, "https://example.com/watch/1")
        self.assertEqual(title, "サンプルページ")
        urls = [c["url"] for c in cands]
        # og, twitter, json-ld, video src, source src, anchor = 6, duplicate removed
        self.assertEqual(len(urls), 6)
        self.assertIn("https://cdn.example.com/og.mp4", urls)
        self.assertIn("https://cdn.example.com/tw.webm", urls)
        self.assertIn("https://cdn.example.com/ld.mp4", urls)
        self.assertIn("https://example.com/media/main.mp4", urls)
        self.assertIn("https://example.com/watch/media/sub.webm", urls)
        self.assertIn("https://example.com/files/linked.mov", urls)
        self.assertNotIn("https://example.com/page.html", urls)

    def test_extract_none(self):
        title, cands = self.mod.extract_videos("<html><head><title>x</title></head><body>hi</body></html>",
                                               "https://example.com/")
        self.assertEqual(cands, [])

    def test_analyze_direct_url_no_fetch(self):
        with mock.patch.object(self.mod, "fetch_url") as fetch:
            res = self.client.post("/analyze", data={"url": "https://example.com/movie.mp4"})
            fetch.assert_not_called()
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("1 件の動画が見つかりました", body)
        self.assertIn("movie.mp4", body)
        self.assertEqual(self.mod.db_history()[0]["found_count"], 1)

    def test_analyze_page_with_mock_fetch(self):
        fake = {"final_url": "https://example.com/watch/1", "content_type": "text/html",
                "body": FIXTURE_HTML.encode("utf-8")}
        with mock.patch.object(self.mod, "fetch_url", return_value=fake):
            res = self.client.post("/analyze", data={"url": "https://example.com/watch/1"})
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn("6 件の動画が見つかりました", body)
        self.assertIn("サンプルページ", body)

    def test_analyze_invalid_url_400(self):
        res = self.client.post("/analyze", data={"url": "http://127.0.0.1/secret"})
        self.assertEqual(res.status_code, 400)

    def test_analyze_fetch_failure_502(self):
        with mock.patch.object(self.mod, "fetch_url", side_effect=RuntimeError("取得失敗")):
            res = self.client.post("/analyze", data={"url": "https://example.com/watch"})
        self.assertEqual(res.status_code, 502)

    def test_download_streams_attachment(self):
        payload = b"\x00\x01fakevideo" * 100
        fake_resp = io.BytesIO(payload)

        class Resp(io.BytesIO):
            pass

        with mock.patch.object(self.mod, "open_remote_stream",
                               return_value=(fake_resp, "video/mp4", len(payload))):
            res = self.client.get("/download?url=https://example.com/movie.mp4")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, payload)
        self.assertIn("attachment", res.headers.get("Content-Disposition", ""))
        self.assertEqual(res.content_type, "video/mp4")

    def test_download_rejects_non_video_and_private(self):
        res = self.client.get("/download?url=https://example.com/page.html")
        self.assertEqual(res.status_code, 400)
        res = self.client.get("/download?url=http://192.168.0.1/movie.mp4")
        self.assertEqual(res.status_code, 400)

    def test_friendly_fetch_error_messages(self):
        for code, needle in [(403, "403"), (404, "404"), (429, "429")]:
            err = urllib.error.HTTPError("https://example.com/", code, "x", hdrs=None, fp=None)
            self.assertIn(needle, self.mod._friendly_fetch_error(err))
            self.assertIn("取得できません", self.mod._friendly_fetch_error(
                urllib.error.HTTPError("https://example.com/", 403, "x", hdrs=None, fp=None)))

    def test_decode_body_gzip(self):
        raw = "<html>hello</html>".encode("utf-8")
        self.assertEqual(self.mod._decode_body(gzip.compress(raw), "gzip"), raw)
        self.assertEqual(self.mod._decode_body(raw, ""), raw)
        self.assertEqual(self.mod._decode_body(b"not-gzip", "gzip"), b"not-gzip")

    def test_fetch_retries_on_503_then_succeeds(self):
        payload = b"<html><head><title>t</title></head></html>"

        class FakeResp(io.BytesIO):
            headers = {"Content-Type": "text/html; charset=utf-8", "Content-Encoding": ""}
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def geturl(self):
                return "https://example.com/page"

        class FakeOpener:
            def __init__(self):
                self.calls = 0
            def open(self, req, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise urllib.error.HTTPError(req.full_url, 503, "busy", hdrs=None, fp=None)
                return FakeResp(payload)

        opener = FakeOpener()
        with mock.patch.object(self.mod, "_opener", return_value=opener), \
             mock.patch("time.sleep", return_value=None):
            got = self.mod.fetch_url("https://example.com/page")
        self.assertEqual(opener.calls, 2)
        self.assertEqual(got["body"], payload)
        self.assertEqual(got["content_type"], "text/html")

    def test_fetch_403_raises_friendly(self):
        class FakeOpener:
            def open(self, req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 403, "forbidden", hdrs=None, fp=None)

        with mock.patch.object(self.mod, "_opener", return_value=FakeOpener()):
            with self.assertRaises(RuntimeError) as cm:
                self.mod.fetch_url("https://example.com/page")
        self.assertIn("403", str(cm.exception))

    def test_download_too_large_413(self):
        fake_resp = mock.Mock()
        with mock.patch.object(self.mod, "open_remote_stream",
                               return_value=(fake_resp, "video/mp4", 201 * 1024 * 1024)):
            res = self.client.get("/download?url=https://example.com/big.mp4")
        self.assertEqual(res.status_code, 413)


if __name__ == "__main__":
    unittest.main()
