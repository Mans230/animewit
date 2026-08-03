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
        assert chunk_size == 256 * 1024  # 256KB chunks — fast cancel/abort reaction
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
# resolve_gofile — full API flow mocked (gofile is blocked from this sandbox)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_gofile_token():
    resolvers._GOFILE_TOKEN = None
    yield
    resolvers._GOFILE_TOKEN = None


class _FakeGofileResp:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise resolvers.requests.HTTPError(f"http {self.status_code}")

    def json(self):
        return self._payload


class _FakeGofileSession:
    def __init__(self, post_resps, get_resps):
        self._post = list(post_resps)
        self._get = list(get_resps)
        self.post_calls = 0
        self.get_calls = 0
        self.last_get_kwargs = None

    def post(self, url, **kwargs):
        self.post_calls += 1
        return self._post.pop(0)

    def get(self, url, **kwargs):
        self.get_calls += 1
        self.last_get_url = url
        self.last_get_kwargs = kwargs
        return self._get.pop(0)


def _token_resp(token):
    return _FakeGofileResp({"status": "ok", "data": {"token": token}})


def _content_resp(children):
    return _FakeGofileResp({"status": "ok", "data": {"children": children}})


def test_resolve_gofile_success(monkeypatch):
    session = _FakeGofileSession(
        post_resps=[_token_resp("tok1")],
        get_resps=[_content_resp({
            "a": {"type": "folder", "name": "sub"},
            "b": {"type": "file", "link": "https://dl.gofile.example/f.zip"},
        })],
    )
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    out = resolvers.resolve_gofile("https://gofile.io/d/CEK8fx")
    assert out == (
        "https://dl.gofile.example/f.zip",
        {"Cookie": "accountToken=tok1"},
    )
    # content id parsed from /d/{id} and Bearer token sent on the content GET
    assert "contents/CEK8fx" in session.last_get_url
    assert session.last_get_kwargs["headers"]["Authorization"] == "Bearer tok1"


def test_resolve_gofile_bad_url_no_network(monkeypatch):
    def boom():
        raise AssertionError("network must not be touched")

    monkeypatch.setattr(resolvers, "get_session", boom)
    assert resolvers.resolve_gofile("https://gofile.io/folder/x") is None


def test_resolve_gofile_no_file_child(monkeypatch):
    session = _FakeGofileSession(
        post_resps=[_token_resp("tok1")],
        get_resps=[_content_resp({"a": {"type": "folder", "name": "sub"}})],
    )
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    assert resolvers.resolve_gofile("https://gofile.io/d/EMPTY1") is None


def test_resolve_gofile_network_failure(monkeypatch):
    class BrokenSession:
        def post(self, url, **kwargs):
            raise resolvers.requests.ConnectionError("blocked")

    monkeypatch.setattr(resolvers, "get_session", lambda: BrokenSession())
    assert resolvers.resolve_gofile("https://gofile.io/d/DEAD99") is None


def test_resolve_gofile_token_refresh_on_401(monkeypatch):
    session = _FakeGofileSession(
        post_resps=[_token_resp("old"), _token_resp("new")],
        get_resps=[
            _FakeGofileResp(status_code=401),
            _content_resp({
                "b": {"type": "file", "link": "https://dl.gofile.example/x.zip"},
            }),
        ],
    )
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    out = resolvers.resolve_gofile("https://gofile.io/d/RE401x")
    assert out == (
        "https://dl.gofile.example/x.zip",
        {"Cookie": "accountToken=new"},
    )
    assert session.post_calls == 2  # token regenerated after the 401
    assert session.get_calls == 2


def test_resolve_gofile_cache_6h(monkeypatch):
    session = _FakeGofileSession(
        post_resps=[_token_resp("tok1"), _token_resp("tok2")],
        get_resps=[
            _content_resp({
                "b": {"type": "file", "link": "https://dl.gofile.example/f.zip"},
            }),
            _content_resp({
                "b": {"type": "file", "link": "https://dl.gofile.example/f.zip"},
            }),
        ],
    )
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    url = "https://gofile.io/d/CACHE1"
    first = resolvers.resolve_gofile(url)
    second = resolvers.resolve_gofile(url)
    assert first == second
    assert session.get_calls == 1  # second call served from the 6h cache
    # age the cached entry beyond 6h -> resolves again
    key = f"gofile|{url}"
    ts, val = resolvers._RESOLVE_CACHE[key]
    resolvers._RESOLVE_CACHE[key] = (ts - 7 * 3600, val)
    assert resolvers.resolve_gofile(url) == first
    assert session.get_calls == 2


def test_resolve_gofile_failure_cached_10min(monkeypatch):
    session = _FakeGofileSession(
        post_resps=[_token_resp("tok1")],
        get_resps=[_content_resp({})],
    )
    monkeypatch.setattr(resolvers, "get_session", lambda: session)
    url = "https://gofile.io/d/NOFILE"
    assert resolvers.resolve_gofile(url) is None
    calls_after_first = session.get_calls
    assert resolvers.resolve_gofile(url) is None
    assert session.get_calls == calls_after_first  # negative result cached too
