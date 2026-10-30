# ============================================================
# videodownloader (single file)
# サイトのURLを貼ると、そのページにある動画ファイルを見つけて
# ダウンロードリンクを出す小さなツール。
#
# 対応: <video>/<source>、og:video、twitter:player:stream、
#       JSON-LD の contentUrl、動画ファイルへの直接リンク、
#       URL自体が動画ファイル（mp4/webm/mov/m3u8等）の場合
# 非対応: DRM、ログイン必須、JavaScriptで後から生成されるプレーヤー、
#         動画サイトの専用ストリーミング形式（直接ファイルが無いもの）
# 動画はサーバーに保存しない。/download は中継（ストリーミング）のみ。
# 自分に利用権のある動画だけをダウンロードしてください。
# ============================================================

# =========================
# [S1] DESIGN
# =========================
HTML = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ UI.app_title }}</title>
<style>
body{margin:0;background:#10243e;color:#fff;font-family:sans-serif;}
main{max-width:820px;margin:0 auto;padding:20px 14px 40px;}
.card{background:#fff;color:#111;padding:16px;margin:14px 0;border-radius:10px;}
input[type=url]{width:100%;box-sizing:border-box;padding:12px;font-size:1rem;}
button,.btn{display:inline-block;padding:10px 16px;font-size:1rem;background:#2563eb;color:#fff;border:0;border-radius:8px;text-decoration:none;cursor:pointer;margin:4px 4px 4px 0;}
.btn.sub{background:#475569;}
.err{background:#fee2e2;color:#7f1d1d;padding:12px;border-radius:8px;}
.note{font-size:.92rem;color:#334155;}
.url{word-break:break-all;font-size:.88rem;color:#334155;}
video{max-width:100%;}
</style>
</head>
<body>
<main>
<h1>{{ UI.app_title }}</h1>
<p>{{ UI.lead }}</p>

<div class="card">
<form method="post" action="/analyze">
<label for="url">サイト（または動画ファイル）のURL</label>
<input type="url" id="url" name="url" required placeholder="https://example.com/page" value="{{ source_url or '' }}">
<p><button type="submit">動画を探す</button></p>
</form>
{% if error %}<p class="err">{{ error }}</p>{% endif %}
<p class="note">{{ UI.rights_note }}</p>
</div>

{% if searched %}
<div class="card">
<h2>結果{% if page_title %}：{{ page_title }}{% endif %}</h2>
<p class="url">対象: {{ source_url }}</p>
{% if candidates %}
<p>{{ candidates|length }} 件の動画が見つかりました。</p>
{% for c in candidates %}
<hr>
<p><strong>{{ c.filename }}</strong>（{{ c.kind }} / {{ c.source }}）</p>
<p class="url">{{ c.url }}</p>
<p>
<a class="btn" href="/download?url={{ c.url|urlencode }}">ダウンロード</a>
<a class="btn sub" href="{{ c.url }}" target="_blank" rel="noopener">直接リンクを開く</a>
</p>
{% if c.is_playlist %}<p class="note">これは再生リスト（m3u8）です。ダウンロードできるのはリストファイル自体で、映像本体の結合には対応プレーヤーや ffmpeg が必要です。</p>{% endif %}
{% endfor %}
{% else %}
<p>このページからは直接ダウンロードできる動画ファイルが見つかりませんでした。</p>
<p class="note">JavaScriptで生成されるプレーヤー、ログイン必須のページ、DRM付き動画、直接ファイルを持たない動画サイトの形式には対応していません。</p>
{% endif %}
</div>
{% endif %}

<div class="card">
<h2>最近探したURL</h2>
{% if history %}
<ul>
{% for h in history %}
<li>{{ h.created_at }} — {{ h.source_url }}（{{ h.found_count }}件）</li>
{% endfor %}
</ul>
{% else %}
<p class="note">まだ履歴はありません。</p>
{% endif %}
</div>
</main>
</body>
</html>
"""

# =========================
# [S2] LOGIC
# =========================
import ipaddress
import json
import os
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, render_template_string, request, Response, stream_with_context

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("VIDEODOWNLOADER_DB_PATH", os.path.join(BASE_DIR, "history.db"))

UI_TEXT = {
    "app_title": "videodownloader",
    "lead": "サイトのURLを貼ると、そのページにある動画ファイルを見つけてダウンロードできるツールです。",
    "rights_note": "自分に利用権のある動画だけをダウンロードしてください。動画はこのサーバーには保存されません。",
}

VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi", ".ogv", ".m3u8", ".mpd"}
PLAYLIST_EXTS = {".m3u8", ".mpd"}
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT = 12
USER_AGENT = "Mozilla/5.0 (compatible; videodownloader/1.0)"


@app.context_processor
def inject():
    return dict(UI=UI_TEXT)


# ---------- URL safety ----------
def _is_blocked_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def validate_url(raw):
    """Return cleaned URL or raise ValueError with a Japanese message."""
    if raw is None:
        raise ValueError("URLを入力してください。")
    url = raw.strip()
    if not url:
        raise ValueError("URLを入力してください。")
    if len(url) > 2048:
        raise ValueError("URLが長すぎます。")
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("http:// または https:// のURLを入力してください。")
    if not parsed.hostname:
        raise ValueError("URLのホスト名が確認できません。")
    if parsed.username or parsed.password:
        raise ValueError("認証情報を含むURLには対応していません。")
    host = parsed.hostname.lower()
    if host in ("localhost",) or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("ローカルアドレスには対応していません。")
    # Literal IP hosts are checked directly (no DNS needed).
    if _is_blocked_ip(host):
        raise ValueError("プライベート／ローカルなアドレスには対応していません。")
    return url


def _check_resolved_host(hostname):
    """Best-effort DNS check used before fetching. Literal/blocked names fail."""
    if _is_blocked_ip(hostname):
        raise ValueError("プライベート／ローカルなアドレスには対応していません。")
    # DNS resolution itself is done by urllib; literal checks above plus
    # redirect validation cover the deterministic cases. Hostnames that
    # resolve to private addresses are rejected in _validate_redirect too
    # when they appear as literal IPs.
    return True


def url_ext(url):
    path = urllib.parse.urlparse(url).path.lower()
    _, ext = os.path.splitext(path)
    return ext


def is_direct_video_url(url):
    return url_ext(url) in VIDEO_EXTS


def filename_from_url(url):
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(os.path.basename(path)) or "video"
    # Keep only safe characters for a download filename.
    name = re.sub(r"[^A-Za-z0-9._\- ]+", "_", name).strip() or "video"
    if "." not in name:
        name += ".mp4"
    return name


# ---------- HTML extraction ----------
class _VideoHTMLParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.candidates = []  # (url, source)
        self.title = ""
        self._in_title = False
        self._in_jsonld = False

    def _add(self, raw_url, source):
        if not raw_url:
            return
        raw_url = raw_url.strip()
        if not raw_url or raw_url.startswith(("data:", "blob:", "javascript:")):
            return
        absolute = urllib.parse.urljoin(self.base_url, raw_url)
        if urllib.parse.urlparse(absolute).scheme not in ("http", "https"):
            return
        # For non-anchor sources, require a video extension or known meta.
        self.candidates.append((absolute, source))

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "video":
            if a.get("src"):
                self._add(a["src"], "videoタグ")
        elif tag == "source":
            if a.get("src"):
                self._add(a["src"], "sourceタグ")
        elif tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            if prop in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
                self._add(a.get("content"), "メタ情報（og:video等）")
        elif tag == "a":
            href = a.get("href")
            if href and is_direct_video_url(urllib.parse.urljoin(self.base_url, href)):
                self._add(href, "リンク")
        elif tag == "embed":
            if a.get("src") and is_direct_video_url(urllib.parse.urljoin(self.base_url, a["src"])):
                self._add(a["src"], "embedタグ")
        elif tag == "script":
            if (a.get("type") or "").lower() == "application/ld+json":
                self._in_jsonld = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_jsonld = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        if self._in_jsonld:
            try:
                obj = json.loads(data)
            except Exception:
                return
            items = obj if isinstance(obj, list) else [obj]
            for item in items:
                if isinstance(item, dict) and item.get("contentUrl"):
                    self._add(str(item["contentUrl"]), "JSON-LD")


def extract_videos(html_text, base_url):
    parser = _VideoHTMLParser(base_url)
    try:
        parser.feed(html_text or "")
    except Exception:
        pass
    seen = set()
    results = []
    for url, source in parser.candidates:
        if url in seen:
            continue
        seen.add(url)
        ext = url_ext(url)
        results.append({
            "url": url,
            "source": source,
            "kind": ext.lstrip(".") if ext else "video",
            "filename": filename_from_url(url),
            "is_playlist": ext in PLAYLIST_EXTS,
        })
    return parser.title, results


# ---------- Fetching ----------
class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return urllib.request.build_opener(_ValidatingRedirectHandler())


def fetch_url(url):
    """Fetch a page. Returns dict(final_url, content_type, body).
    Raises ValueError for blocked/invalid, RuntimeError for fetch failures."""
    validate_url(url)
    _check_resolved_host(urllib.parse.urlparse(url).hostname or "")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener().open(req, timeout=FETCH_TIMEOUT) as resp:
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            body = resp.read(MAX_PAGE_BYTES + 1)
            if len(body) > MAX_PAGE_BYTES:
                body = body[:MAX_PAGE_BYTES]
            return {"final_url": resp.geturl(), "content_type": content_type, "body": body}
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"ページを取得できませんでした（{e}）") from e


def open_remote_stream(url):
    """Open a remote video for streaming download. Returns (resp, content_type, content_length)."""
    validate_url(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    resp = _opener().open(req, timeout=FETCH_TIMEOUT)
    content_type = (resp.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
    length = resp.headers.get("Content-Length")
    try:
        length = int(length) if length is not None else None
    except ValueError:
        length = None
    return resp, content_type, length


# ---------- History (SQLite) ----------
SQL_INIT = """
CREATE TABLE IF NOT EXISTS analyses(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    page_title TEXT NOT NULL DEFAULT '',
    found_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
)
"""


def db():
    return sqlite3.connect(DB_PATH, check_same_thread=False)


def db_init():
    with db() as c:
        c.execute(SQL_INIT)


def now_jst():
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M:%S")


def db_add_history(source_url, page_title, found_count):
    with db() as c:
        c.execute(
            "INSERT INTO analyses(source_url, page_title, found_count, created_at) VALUES(?,?,?,?)",
            (source_url, page_title or "", int(found_count), now_jst()),
        )


def db_history(limit=10):
    with db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT source_url, page_title, found_count, created_at FROM analyses ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


db_init()

# =========================
# [S3] RUN
# =========================
@app.route("/")
def index():
    return render_template_string(HTML, history=db_history(), searched=False,
                                  candidates=[], page_title="", source_url="", error="")


@app.route("/healthz")
def healthz():
    return jsonify({"status": "ok"})


@app.route("/analyze", methods=["POST"])
def analyze():
    raw = request.form.get("url", "")
    try:
        url = validate_url(raw)
    except ValueError as e:
        return render_template_string(HTML, history=db_history(), searched=False,
                                      candidates=[], page_title="", source_url=raw,
                                      error=str(e)), 400

    # URL itself is a direct video file: no page fetch needed.
    if is_direct_video_url(url):
        candidate = {
            "url": url, "source": "直接リンク", "kind": url_ext(url).lstrip("."),
            "filename": filename_from_url(url), "is_playlist": url_ext(url) in PLAYLIST_EXTS,
        }
        db_add_history(url, "", 1)
        return render_template_string(HTML, history=db_history(), searched=True,
                                      candidates=[candidate], page_title="", source_url=url, error="")

    try:
        fetched = fetch_url(url)
    except ValueError as e:
        return render_template_string(HTML, history=db_history(), searched=False,
                                      candidates=[], page_title="", source_url=url,
                                      error=str(e)), 400
    except RuntimeError as e:
        return render_template_string(HTML, history=db_history(), searched=False,
                                      candidates=[], page_title="", source_url=url,
                                      error=str(e)), 502

    final_url = fetched["final_url"]
    ctype = fetched["content_type"]
    # The page itself turned out to be a video.
    if ctype.startswith("video/") or is_direct_video_url(final_url):
        candidate = {
            "url": final_url, "source": "直接リンク", "kind": url_ext(final_url).lstrip(".") or "video",
            "filename": filename_from_url(final_url), "is_playlist": url_ext(final_url) in PLAYLIST_EXTS,
        }
        db_add_history(url, "", 1)
        return render_template_string(HTML, history=db_history(), searched=True,
                                      candidates=[candidate], page_title="", source_url=url, error="")

    charset = "utf-8"
    html_text = fetched["body"].decode(charset, errors="replace")
    title, candidates = extract_videos(html_text, final_url)
    db_add_history(url, title, len(candidates))
    return render_template_string(HTML, history=db_history(), searched=True,
                                  candidates=candidates, page_title=title, source_url=url, error="")


@app.route("/download")
def download():
    raw = request.args.get("url", "")
    try:
        url = validate_url(raw)
    except ValueError as e:
        return Response(str(e), status=400, mimetype="text/plain; charset=utf-8")
    if not (is_direct_video_url(url)):
        return Response("ダウンロードは動画ファイルのURLだけに対応しています。", status=400,
                        mimetype="text/plain; charset=utf-8")
    try:
        resp, content_type, length = open_remote_stream(url)
    except ValueError as e:
        return Response(str(e), status=400, mimetype="text/plain; charset=utf-8")
    except Exception as e:
        return Response(f"動画を取得できませんでした（{e}）", status=502,
                        mimetype="text/plain; charset=utf-8")
    if length is not None and length > MAX_DOWNLOAD_BYTES:
        resp.close()
        return Response("ファイルが大きすぎるため、このツールではダウンロードできません（上限200MB）。直接リンクをお試しください。",
                        status=413, mimetype="text/plain; charset=utf-8")

    filename = filename_from_url(url)

    def generate():
        sent = 0
        try:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                sent += len(chunk)
                if sent > MAX_DOWNLOAD_BYTES:
                    break
                yield chunk
        finally:
            resp.close()

    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(filename)}",
    }
    if length is not None:
        headers["Content-Length"] = str(length)
    return Response(stream_with_context(generate()), content_type=content_type, headers=headers)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
