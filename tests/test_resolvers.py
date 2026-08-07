import base64
import os

import pytest

import resolvers


@pytest.fixture(autouse=True)
def _clear_resolve_cache():
    resolvers._RESOLVE_CACHE.clear()
    yield
    resolvers._RESOLVE_CACHE.clear()


class _FakeResp:
    def __init__(self, ok=True, headers=None):
        self.ok = ok
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# resolve cache
# ---------------------------------------------------------------------------

def test_resolve_cache_hit(monkeypatch):
    calls = {"n": 0}

    def fake_head(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=True, headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    url = "https://drive.google.com/file/d/ABC123/preview"
    first = resolvers.resolve_drive_mp4(url)
    second = resolvers.resolve_drive_mp4(url)
    assert first is not None and "id=ABC123" in first
    assert second == first
    assert calls["n"] == 1  # second call served from cache


def test_resolve_cache_miss_distinct_urls(monkeypatch):
    calls = {"n": 0}

    def fake_head(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=True, headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    resolvers.resolve_drive_mp4("https://drive.google.com/file/d/ID1/preview")
    resolvers.resolve_drive_mp4("https://drive.google.com/file/d/ID2/preview")
    assert calls["n"] == 2


def test_resolve_cache_stores_none(monkeypatch):
    calls = {"n": 0}

    def fake_head(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=False, headers={"Content-Type": "text/html"})

    def fake_get(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=False, headers={"Content-Type": "text/html"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    url = "https://drive.google.com/file/d/NOPE/preview"
    assert resolvers.resolve_drive_mp4(url) is None
    calls_after_first = calls["n"]
    assert resolvers.resolve_drive_mp4(url) is None
    assert calls["n"] == calls_after_first  # None result was cached too


def test_resolve_mp4_cache(monkeypatch):
    calls = {"n": 0}

    def fake_head(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=True, headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    url = "https://drive.google.com/file/d/XYZ9/preview"
    assert resolvers.resolve_mp4(url) == resolvers.resolve_mp4(url)
    assert calls["n"] == 1


def test_resolve_cache_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_head(url, **kwargs):
        calls["n"] += 1
        return _FakeResp(ok=True, headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    url = "https://drive.google.com/file/d/OLD1/preview"
    resolvers.resolve_drive_mp4(url)
    # age the cached entry beyond the 6h TTL
    ts, val = resolvers._RESOLVE_CACHE[url]
    resolvers._RESOLVE_CACHE[url] = (ts - 7 * 3600, val)
    resolvers.resolve_drive_mp4(url)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# drive preview-url regex
# ---------------------------------------------------------------------------

def test_drive_preview_url_regex_rejects_non_file_urls(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(resolvers.requests, "head", boom)
    monkeypatch.setattr(resolvers.requests, "get", boom)
    assert resolvers.resolve_drive_mp4("https://drive.google.com/drive/folders/x") is None
    assert resolvers.resolve_drive_mp4("https://example.com/file") is None


def test_drive_direct_url_uses_file_id(monkeypatch):
    seen = {}

    def fake_head(url, **kwargs):
        seen["url"] = url
        return _FakeResp(ok=True, headers={"Content-Type": "video/mp4"})

    monkeypatch.setattr(resolvers.requests, "head", fake_head)
    out = resolvers.resolve_drive_mp4("https://drive.google.com/file/d/AbC-_123/preview")
    assert "id=AbC-_123" in out
    assert "drive.usercontent.google.com" in seen["url"]


# ---------------------------------------------------------------------------
# download_mega cancel
# ---------------------------------------------------------------------------

def _fake_mega_url() -> str:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("=")
    return f"https://mega.nz/file/TESTID12#{key}"


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 2048  # 2MB chunks (SPEC perf fix)
        yield from self._chunks


class _FakeHttp:
    def __init__(self, chunks):
        self._chunks = chunks

    def get(self, url, stream=False, timeout=None):
        return _FakeStream(self._chunks)


@pytest.fixture()
def fake_mega(monkeypatch):
    monkeypatch.setattr(
        resolvers, "_mega_api", lambda payload: {"g": "https://dl.example/f", "s": 64}
    )
    monkeypatch.setattr(
        resolvers, "_http_for", lambda host: _FakeHttp([b"x" * 16, b"y" * 16])
    )


def test_download_mega_cancel_raises(fake_mega, tmp_path):
    with pytest.raises(resolvers.DownloadCancelled):
        resolvers.download_mega(
            _fake_mega_url(), str(tmp_path / "f.mp4"), cancel=lambda: True
        )


def test_download_mega_cancel_mid_stream(fake_mega, tmp_path):
    state = {}
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1  # first check passes, second cancels

    with pytest.raises(resolvers.DownloadCancelled):
        resolvers.download_mega(_fake_mega_url(), str(tmp_path / "f.mp4"), state=state, cancel=cancel)
    assert state.get("total") == 64  # total recorded before streaming


def test_download_mega_success_without_cancel(fake_mega, tmp_path):
    state = {}
    out = tmp_path / "f.mp4"
    written = resolvers.download_mega(_fake_mega_url(), str(out), state=state)
    assert written == 32
    assert state["done"] == 32
    assert out.stat().st_size == 32


# ---------------------------------------------------------------------------
# gofile resolver
# ---------------------------------------------------------------------------

_GOFILE_URL = "https://gofile.io/d/AbC123"


@pytest.fixture(autouse=True)
def _clear_gofile_token():
    resolvers._GOFILE_TOKEN = None
    yield
    resolvers._GOFILE_TOKEN = None


class _FakeJsonResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise resolvers.requests.HTTPError(str(self.status_code))


def _gofile_api(children, status="ok"):
    """Fake api.gofile.io: POST /accounts + GET /contents/{id}."""
    calls = {"posts": 0, "gets": [], "headers": []}

    def fake_post(url, **kwargs):
        calls["posts"] += 1
        assert url.endswith("/accounts")
        return _FakeJsonResp({"status": "ok", "data": {"token": "TOK123"}})

    def fake_get(url, **kwargs):
        calls["gets"].append(url)
        calls["headers"].append(kwargs.get("headers") or {})
        assert "/contents/AbC123" in url
        return _FakeJsonResp(
            {"status": status, "data": {"children": children, "name": "fld"}}
        )

    return fake_post, fake_get, calls


def test_gofile_wt_signature_format():
    wt = resolvers._gofile_wt("TOK123", bucket=124036)
    assert len(wt) == 64
    assert all(c in "0123456789abcdef" for c in wt)
    # deterministic per bucket, changes across buckets
    assert wt == resolvers._gofile_wt("TOK123", bucket=124036)
    assert wt != resolvers._gofile_wt("TOK123", bucket=124037)


def test_resolve_gofile_picks_largest_video(monkeypatch):
    children = {
        "a": {"type": "file", "name": "small.mp4", "size": 100,
              "link": "https://cdn.example/s.mp4"},
        "b": {"type": "file", "name": "big.mkv", "size": 900,
              "link": "https://cdn.example/b.mkv"},
        "c": {"type": "file", "name": "notes.txt", "size": 99999,
              "link": "https://cdn.example/n.txt"},
        "d": {"type": "folder", "name": "sub"},
    }
    fake_post, fake_get, calls = _gofile_api(children)
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)

    info = resolvers.resolve_gofile(_GOFILE_URL)
    assert info["url"] == "https://cdn.example/b.mkv"
    assert info["size"] == 900
    assert info["token"] == "TOK123"
    # signed headers, like the web app
    sent = calls["headers"][0]
    assert sent["Authorization"] == "Bearer TOK123"
    assert len(sent["X-Website-Token"]) == 64
    assert sent["User-Agent"] == resolvers._GOFILE_UA
    # cached: second call does no more network
    assert resolvers.resolve_gofile(_GOFILE_URL) == info
    assert calls["posts"] == 1 and len(calls["gets"]) == 1


def test_resolve_gofile_children_as_list(monkeypatch):
    children = [
        {"type": "file", "name": "ep.mp4", "size": 50,
         "link": "https://cdn.example/ep.mp4"},
    ]
    fake_post, fake_get, _ = _gofile_api(children)
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    info = resolvers.resolve_gofile(_GOFILE_URL)
    assert info["url"] == "https://cdn.example/ep.mp4"


def test_resolve_gofile_no_video_returns_none(monkeypatch):
    children = {
        "a": {"type": "file", "name": "ep.zip", "size": 100,
              "link": "https://cdn.example/e.zip"},
    }
    fake_post, fake_get, _ = _gofile_api(children)
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    assert resolvers.resolve_gofile(_GOFILE_URL) is None


def test_resolve_gofile_error_status_returns_none(monkeypatch):
    # deleted / password-protected content must not break the batch
    fake_post, fake_get, _ = _gofile_api({}, status="error-notFound")
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    assert resolvers.resolve_gofile(_GOFILE_URL) is None


def test_resolve_gofile_not_premium_retries_bucket_then_none(monkeypatch):
    fake_post, fake_get, calls = _gofile_api({}, status="error-notPremium")
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    assert resolvers.resolve_gofile(_GOFILE_URL) is None
    assert len(calls["gets"]) == 2  # current bucket + bucket-1, then give up


def test_resolve_gofile_rate_limit_drops_cached_token(monkeypatch):
    fake_post, fake_get, _ = _gofile_api({}, status="error-rateLimit")
    monkeypatch.setattr(resolvers.requests, "post", fake_post)
    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    assert resolvers.resolve_gofile(_GOFILE_URL) is None
    # the (possibly banned) guest token must be dropped so the next resolve
    # creates a fresh guest account instead of reusing it for 12h
    assert resolvers._GOFILE_TOKEN is None
    # next call: a brand-new account is created
    resolvers._RESOLVE_CACHE.clear()
    assert resolvers.resolve_gofile(_GOFILE_URL) is None
    assert resolvers._GOFILE_TOKEN is None


def test_resolve_gofile_account_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise resolvers.requests.ConnectionError("rate limited")

    monkeypatch.setattr(resolvers.requests, "post", boom)
    assert resolvers.resolve_gofile(_GOFILE_URL) is None


def test_resolve_gofile_bad_url_no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(resolvers.requests, "post", boom)
    monkeypatch.setattr(resolvers.requests, "get", boom)
    assert resolvers.resolve_gofile("https://gofile.io/terms") is None
    assert resolvers.resolve_gofile("https://example.com/d/abc") is None


def test_get_gofile_size(monkeypatch):
    monkeypatch.setattr(
        resolvers, "resolve_gofile",
        lambda url: {"url": "https://cdn.example/b.mkv", "size": 900, "token": "T"},
    )
    assert resolvers.get_gofile_size(_GOFILE_URL) == 900
    monkeypatch.setattr(resolvers, "resolve_gofile", lambda url: None)
    assert resolvers.get_gofile_size(_GOFILE_URL) is None


class _FakeGofileStream:
    def __init__(self, chunks):
        self._chunks = chunks
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        assert chunk_size == 1024 * 2048  # same 2MB chunks as download_mega
        yield from self._chunks


class _FakeGofileSession:
    def __init__(self, chunks):
        self._chunks = chunks
        self.seen_headers = None

    def get(self, url, stream=False, timeout=None, headers=None):
        self.seen_headers = headers
        return _FakeGofileStream(self._chunks)


@pytest.fixture()
def fake_gofile(monkeypatch):
    monkeypatch.setattr(
        resolvers, "resolve_gofile",
        lambda url: {"url": "https://cdn.example/v.mp4", "size": 64,
                     "token": "TOK123"},
    )
    session = _FakeGofileSession([b"x" * 16, b"y" * 16])
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    return session


def test_download_gofile_success_sends_cookie(fake_gofile, tmp_path):
    state = {}
    out = tmp_path / "v.mp4"
    written = resolvers.download_gofile(_GOFILE_URL, str(out), state=state)
    assert written == 32
    assert state["done"] == 32 and state["total"] == 64
    assert out.stat().st_size == 32
    assert fake_gofile.seen_headers["Cookie"] == "accountToken=TOK123"


def test_download_gofile_cancel_raises(fake_gofile, tmp_path):
    with pytest.raises(resolvers.DownloadCancelled):
        resolvers.download_gofile(
            _GOFILE_URL, str(tmp_path / "v.mp4"), cancel=lambda: True
        )


def test_download_gofile_cancel_mid_stream(fake_gofile, tmp_path):
    calls = {"n": 0}

    def cancel():
        calls["n"] += 1
        return calls["n"] > 1

    with pytest.raises(resolvers.DownloadCancelled):
        resolvers.download_gofile(
            _GOFILE_URL, str(tmp_path / "v.mp4"), state={}, cancel=cancel
        )


def test_download_gofile_unresolvable_raises(fake_gofile, monkeypatch, tmp_path):
    monkeypatch.setattr(resolvers, "resolve_gofile", lambda url: None)
    with pytest.raises(RuntimeError):
        resolvers.download_gofile(_GOFILE_URL, str(tmp_path / "v.mp4"))
