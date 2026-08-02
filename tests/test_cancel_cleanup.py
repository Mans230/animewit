"""Regression test: _send_video_inner must clean up the /tmp file and stop
the orphaned download thread when the download is cancelled / times out.

Before the fix, `except Exception` did not catch CancelledError, so the
`os.unlink(tmp_path)` calls were skipped and the blocking download thread
kept running in the background with no way to stop it.
"""

import asyncio
import os
import sys
import threading
import time

import pytest

os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("BASE_PUBLIC_URL", "https://x.test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402
import resolvers  # noqa: E402


class _FakeMessage:
    def __init__(self):
        self.chat_id = 123
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class _FakeProgress:
    async def edit_text(self, *a, **kw):
        return None


class _FakeQuery:
    def __init__(self):
        self.message = _FakeMessage()

    def get_bot(self):  # pragma: no cover - never reached in this test
        raise AssertionError("send_video should not be reached")


@pytest.mark.asyncio
async def test_timeout_deletes_tmp_file_and_stops_download_thread(monkeypatch):
    captured = {"finished": threading.Event()}

    async def fake_fetch_episode(ep_url):
        return {
            "anime_title": "Test Anime",
            "number": "1",
            "url": ep_url,
        }

    def slow_download(mp4_url, path, state, cancel=None):
        """Blocking download that only stops when `cancel()` fires."""
        captured["path"] = path
        captured["state"] = state
        try:
            with open(path, "wb") as fh:
                while not (cancel and cancel()):
                    fh.write(b"x" * 4096)
                    fh.flush()
                    state["done"] = state.get("done", 0) + 4096
                    time.sleep(0.01)
            raise resolvers.DownloadCancelled()
        finally:
            # signals the worker function returned (the executor thread
            # itself stays alive in the pool, so we can't join() it)
            captured["finished"].set()

    monkeypatch.setattr(bot, "fetch_episode", fake_fetch_episode)
    monkeypatch.setattr(
        resolvers, "resolve_drive_mp4", lambda url: "https://drive.example/dl.mp4"
    )
    monkeypatch.setattr(bot, "_download_to_temp", slow_download)

    query = _FakeQuery()
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            bot._send_video_inner(
                query, _FakeProgress(), 123,
                "https://witanime.example/ep-1", "drive",
                "https://drive.google.com/file/d/X/preview",
            ),
            timeout=0.3,
        )

    # tmp file removed even though the download never finished
    assert captured["path"].startswith(tempfile_dir())
    assert not os.path.exists(captured["path"])
    # the orphan thread was told to stop ...
    assert captured["state"]["aborted"] is True
    # ... and it actually stopped at the next chunk (DownloadCancelled)
    assert captured["finished"].wait(timeout=5)
    done_at_stop = captured["state"]["done"]
    time.sleep(0.1)
    assert captured["state"]["done"] == done_at_stop  # no further writes


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()
