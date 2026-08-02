"""Per-episode watchdog tests: a stalled/throttled/hung source must be
skipped — never freeze the whole season batch (production incident: a
throttled Mega CDN transfer hung a 6-episode batch forever while the bot
stayed responsive)."""
import asyncio
import base64
import threading
import time

import pytest
import requests

import resolvers
import season

from test_season import FakeBot, FakeResp, make_ep, make_job  # noqa: F401


def _eps(n):
    return [{"url": f"https://witanime.website/episode/x-{i}/", "number": str(i)}
            for i in range(1, n + 1)]


def _patch_sources(monkeypatch):
    """Every episode offers one HD drive source (no network)."""
    monkeypatch.setattr(
        season,
        "episode_quality_sources",
        lambda ep: asyncio.sleep(
            0, {"HD": [{"kind": "drive", "url": "https://drive.example/v",
                        "host": "drive"}]}
        ),
    )


@pytest.mark.asyncio
async def test_hung_episode_times_out_and_batch_continues(monkeypatch):
    """An episode whose download never returns is failed after ep_timeout;
    the next episode is still processed and the summary is sent."""
    _patch_sources(monkeypatch)
    calls = {"n": 0}

    async def fake_dl(self, src, path, state):
        calls["n"] += 1
        if calls["n"] == 1:
            state["done"] = 10  # some progress, then a total hang
            await asyncio.sleep(60)
            return 10
        return 100  # episode 2 downloads instantly

    monkeypatch.setattr(season.SeasonJob, "_download_one", fake_dl)
    bot = FakeBot()
    job = make_job(bot, _eps(2))
    job.ep_timeout = 0.4
    job.stall_secs = 60.0       # isolate the hard deadline
    job.watchdog_interval = 0.05

    started = time.monotonic()
    await job.run()
    elapsed = time.monotonic() - started

    assert elapsed < 10, "batch must not freeze on a hung episode"
    assert job.results[0][0] == "1"
    assert job.results[0][1] == "failed"
    assert "انتهت المهلة" in job.results[0][3]
    assert job.results[1][1] == "sent"
    assert len(bot.sent_videos) == 1          # only episode 2 sent a video
    summary = bot.sent_messages[-1].text
    assert "انتهت المهلة" in summary and "✅" in summary


@pytest.mark.asyncio
async def test_stalled_download_is_aborted(monkeypatch):
    """Zero byte-progress for stall_secs during the download phase aborts
    the episode even when the hard deadline is far away."""
    _patch_sources(monkeypatch)

    async def fake_dl(self, src, path, state):
        state["done"] = 5
        await asyncio.sleep(60)
        return 5

    monkeypatch.setattr(season.SeasonJob, "_download_one", fake_dl)
    bot = FakeBot()
    job = make_job(bot, _eps(1))
    job.ep_timeout = 60.0
    job.stall_secs = 0.3
    job.watchdog_interval = 0.05

    await job.run()

    assert job.results[0][1] == "failed"
    assert "تجمّد" in job.results[0][3]


@pytest.mark.asyncio
async def test_progressing_download_is_not_aborted(monkeypatch):
    """A slow-but-moving download (progress faster than stall_secs) must
    survive the stall detector and complete."""
    _patch_sources(monkeypatch)

    async def fake_dl(self, src, path, state):
        for chunk in range(20):          # ~1s of steady progress
            state["done"] = chunk + 1
            await asyncio.sleep(0.05)
        return 100

    monkeypatch.setattr(season.SeasonJob, "_download_one", fake_dl)
    bot = FakeBot()
    job = make_job(bot, _eps(1))
    job.ep_timeout = 10.0
    job.stall_secs = 0.3
    job.watchdog_interval = 0.05

    await job.run()

    assert job.results[0][1] == "sent"
    assert len(bot.sent_videos) == 1


class HangingResp:
    """A response whose stream blocks until close() is called — mimics a
    throttled CDN connection that never delivers and never errors."""

    def __init__(self):
        self.headers = {"Content-Length": "1000000"}
        self.closed = False
        self._release = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def close(self):
        self.closed = True
        self._release.set()

    def iter_content(self, chunk_size=1):
        self._release.wait(10)
        raise RuntimeError("connection closed by watchdog")
        yield  # pragma: no cover - marks this a generator


@pytest.mark.asyncio
async def test_watchdog_force_closes_stalled_stream(monkeypatch):
    """End-to-end through the real drive stream path: the watchdog sets the
    abort event AND closes the live response so the blocked thread exits."""
    _patch_sources(monkeypatch)
    resp = HangingResp()

    class Session:
        def get(self, url, **kw):
            return resp

    monkeypatch.setattr(
        resolvers, "resolve_drive_mp4", lambda url: "https://drive.example/dl"
    )
    monkeypatch.setattr(
        resolvers, "get_session", lambda: Session(), raising=False
    )

    bot = FakeBot()
    job = make_job(bot, _eps(1))
    job.ep_timeout = 60.0
    job.stall_secs = 0.3
    job.watchdog_interval = 0.05

    await job.run()

    assert resp.closed, "watchdog must force-close the stalled response"
    assert job.results[0][1] == "failed"
    assert "تجمّد" in job.results[0][3]
    assert not bot.sent_videos


# ---------------------------------------------------------------------------
# resolvers — Mega API host handling
# ---------------------------------------------------------------------------

def test_mega_bad_host_skipped_after_ssl_failure(monkeypatch):
    """A host whose TLS identity is broken (stale fallback IP serving the
    wrong certificate) is remembered and skipped on later calls."""
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"g": "https://cdn.example/f", "s": 5}]

    class Http:
        def post(self, url, **kw):
            host = url.split("//", 1)[1].split("/", 1)[0]
            calls.append(host)
            if host == "g.api.mega.co.nz":
                raise requests.exceptions.SSLError("hostname mismatch")
            return Resp()

    monkeypatch.setattr(resolvers, "_http_for", lambda host: Http())
    resolvers._MEGA_BAD_HOSTS.clear()
    try:
        item = resolvers._mega_api({"a": "g", "g": 1, "p": "x"})
        assert item["s"] == 5
        assert calls == ["g.api.mega.co.nz", "g.api.mega.nz"]

        calls.clear()
        resolvers._mega_api({"a": "g", "g": 1, "p": "x"})
        assert calls == ["g.api.mega.nz"], "broken host must be skipped"
    finally:
        resolvers._MEGA_BAD_HOSTS.clear()


def test_download_mega_exposes_resp_and_honours_cancel(monkeypatch, tmp_path):
    """download_mega publishes the live response in state['resp'] while
    streaming (so the watchdog can close it), removes it afterwards, and
    stops mid-stream when the cancel callable fires."""
    fid = "AbCdEfGh"
    key = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
    url = f"https://mega.nz/file/{fid}#{key}"
    monkeypatch.setattr(
        resolvers, "_mega_api",
        lambda payload: {"g": "https://cdn.example/f", "s": 16},
    )

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=1):
            yield b"\x00" * 8
            yield b"\x00" * 8

    class Http:
        def get(self, url, **kw):
            return Resp()

    monkeypatch.setattr(resolvers, "_http_for", lambda host: Http())

    state = {}
    resp_seen = []

    def cancel():
        resp_seen.append(state.get("resp") is not None)
        return len(resp_seen) > 2  # cancel after the first chunk

    with pytest.raises(resolvers.DownloadCancelled):
        resolvers.download_mega(url, str(tmp_path / "f.bin"), state,
                                cancel=cancel)

    assert resp_seen[0] is False            # before the GET starts
    assert any(resp_seen[1:])               # exposed while streaming
    assert "resp" not in state              # cleaned up afterwards
