"""Tests for season.py — fully self-contained (no conftest dependency).

Everything is mocked: no live network (mega.nz is blocked from this sandbox
anyway). Run: python -m pytest -q tests/test_season.py
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import resolvers  # noqa: E402
import scraper  # noqa: E402
import season  # noqa: E402


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clear_sources_cache():
    season._SOURCES_CACHE.clear()
    yield
    season._SOURCES_CACHE.clear()


def drive_src(quality="HD", fid="FILEID"):
    return {
        "host": "Google Drive",
        "quality": quality,
        "url": f"https://drive.google.com/file/d/{fid}/preview",
    }


def mega_src(quality="HD", fid="MID"):
    return {
        "host": "Mega",
        "quality": quality,
        "url": f"https://mega.nz/file/{fid}#KEY{quality}",
    }


def make_ep(number, embed_url=None):
    embed_url = embed_url or f"https://yona.example/embed/{number}"
    return {
        "title": f"Test Anime الحلقة {number}",
        "anime_title": "Test Anime",
        "anime_url": "https://witanime.example/anime",
        "url": f"https://witanime.example/ep-{number}",
        "number": str(number),
        "type": "حلقة",
        "prev_url": None,
        "next_url": None,
        "servers": [
            {"name": "yonaplay - multi", "embed_url": embed_url},
            {"name": "ok.ru", "embed_url": "https://ok.ru/videoembed/1"},
        ],
        "downloads": [],
    }


def patch_players(monkeypatch, players_map, calls=None):
    def fake_players(embed_url):
        if calls is not None:
            calls.append(embed_url)
        return players_map.get(embed_url, [])

    monkeypatch.setattr(scraper, "get_yonaplay_players", fake_players)


class FakeResp:
    def __init__(self, chunks, total=None):
        self._chunks = list(chunks)
        if total is None:
            total = sum(len(c) for c in self._chunks)
        self.headers = {"Content-Length": str(total)}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size=1):
        yield from self._chunks


class FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, url, **kwargs):
        return self._resp


def patch_drive_download(monkeypatch, resp):
    monkeypatch.setattr(
        resolvers, "resolve_drive_mp4", lambda url: "https://drive.example/dl.mp4"
    )
    monkeypatch.setattr(
        resolvers, "get_session", lambda: FakeSession(resp), raising=False
    )


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.edits = []
        self.markups = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)
        self.markups.append(reply_markup)
        self.text = text
        return self


class FakeBot:
    """Records every call so tests can assert ordering."""

    def __init__(self, on_send_video=None):
        self.sent_messages = []
        self.sent_videos = []
        self._on_send_video = on_send_video

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        msg = FakeMessage(text or "")
        self.sent_messages.append(msg)
        return msg

    async def send_video(self, chat_id=None, video=None, caption=None, **kw):
        if video is not None and hasattr(video, "read"):
            video.read()
        self.sent_videos.append({"caption": caption, "kw": kw})
        if self._on_send_video:
            self._on_send_video()
        return FakeMessage(caption or "")


def make_job(bot, episodes, *, wanted="HD", max_bytes=10 * 1024 * 1024,
             fetch_episode=None, too_big_calls=None, video_timeout=60):
    async def default_fetch(ep_url):
        return make_ep(ep_url.rsplit("-", 1)[-1])

    async def too_big_reply(ep, size):
        if too_big_calls is not None:
            too_big_calls.append((ep, size))

    return season.SeasonJob(
        bot=bot,
        chat_id=123,
        user_id=42,
        anime_title="Test Anime",
        episodes=episodes,
        wanted_quality=wanted,
        max_video_bytes=max_bytes,
        fetch_episode=fetch_episode or default_fetch,
        too_big_reply=too_big_reply,
        video_timeout=video_timeout,
    )


# ---------------------------------------------------------------------------
# nearest_quality — full matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "available,wanted,expected",
    [
        (["FHD", "HD"], "FHD", "FHD"),   # exact
        (["FHD", "HD"], "HD", "HD"),     # exact
        (["HD"], "HD", "HD"),            # exact single
        (["FHD"], "FHD", "FHD"),         # exact single
        (["FHD"], "HD", "FHD"),          # wanted missing -> better quality
        (["HD"], "FHD", "HD"),           # wanted missing -> worse quality
        ([], "FHD", None),               # empty
        ([], "HD", None),                # empty
        (["SD"], "FHD", None),           # unknown qualities only
    ],
)
def test_nearest_quality_matrix(available, wanted, expected):
    assert season.nearest_quality(available, wanted) == expected


# ---------------------------------------------------------------------------
# episode_quality_sources / pick_source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_episode_quality_sources_drive_first_and_filtered(monkeypatch):
    embed = "https://yona.example/embed/1"
    patch_players(monkeypatch, {
        embed: [
            mega_src("FHD"),                                # mega listed first
            drive_src("FHD"),
            {"host": "4shared", "quality": "FHD",
             "url": "https://4shared.example/f.mp4"},       # excluded
            drive_src("HD"),
        ],
    })
    ep = make_ep(1, embed)
    ep["servers"].append(
        {"name": "yonaplay - FHD", "embed_url": embed}  # dup expansion -> deduped
    )
    sources = await season.episode_quality_sources(ep)
    assert [s["kind"] for s in sources["FHD"]] == ["drive", "mega"]
    assert [s["kind"] for s in sources["HD"]] == ["drive"]
    assert all("4shared" not in s["host"] for srcs in sources.values() for s in srcs)


@pytest.mark.asyncio
async def test_episode_quality_sources_cache(monkeypatch):
    embed = "https://yona.example/embed/9"
    calls = []
    patch_players(monkeypatch, {embed: [drive_src("HD")]}, calls=calls)
    ep = make_ep(9, embed)
    first = await season.episode_quality_sources(ep)
    second = await season.episode_quality_sources(ep)
    assert first == second
    assert len(calls) == 1  # second call served from the 6h TTL cache


def test_pick_source_prefers_drive():
    sources = {
        "FHD": [
            {"kind": "drive", "url": "d", "host": "Google Drive"},
            {"kind": "mega", "url": "m", "host": "Mega"},
        ],
        "HD": [{"kind": "mega", "url": "m2", "host": "Mega"}],
    }
    quality, src = season.pick_source(sources, "FHD")
    assert quality == "FHD"
    assert src["kind"] == "drive"


def test_pick_source_nearest_fallback_and_none():
    sources = {"FHD": [{"kind": "drive", "url": "d", "host": "Google Drive"}],
               "HD": []}
    quality, _ = season.pick_source(sources, "HD")
    assert quality == "FHD"  # HD wanted but missing -> nearest (better) FHD
    assert season.pick_source({"FHD": [], "HD": []}, "HD") is None


# ---------------------------------------------------------------------------
# scan_season — counts + truncation + numeric sort (fake fetch_episode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_season_counts_truncate_sort(monkeypatch):
    players = {
        "https://yona.example/embed/1": [drive_src("FHD", "A"), drive_src("HD", "B")],
        "https://yona.example/embed/2": [mega_src("HD")],
        "https://yona.example/embed/3": [],
    }
    patch_players(monkeypatch, players)

    async def fake_fetch(ep_url):
        return make_ep(ep_url.rsplit("-", 1)[-1])

    episodes = [
        {"number": n, "url": f"https://witanime.example/ep-{n}", "type": "حلقة"}
        for n in ("3", "1", "5", "2", "4")  # deliberately unsorted
    ]
    result = await season.scan_season(
        episodes, max_eps=3, concurrency=2, fetch_episode=fake_fetch
    )
    assert result["truncated"] is True
    assert result["total"] == 5
    assert [e["number"] for e in result["eps"]] == ["1", "2", "3"]  # sorted + capped
    assert result["counts"] == {"FHD": 1, "HD": 2}
    ep1 = result["eps"][0]
    assert ep1["sources"]["FHD"][0]["kind"] == "drive"


@pytest.mark.asyncio
async def test_scan_season_no_truncation(monkeypatch):
    patch_players(monkeypatch, {"https://yona.example/embed/1": [drive_src("HD")]})

    async def fake_fetch(ep_url):
        return make_ep(1)

    result = await season.scan_season(
        [{"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"}],
        max_eps=24,
        fetch_episode=fake_fetch,
    )
    assert result["truncated"] is False
    assert result["total"] == 1
    assert result["counts"] == {"FHD": 0, "HD": 1}


# ---------------------------------------------------------------------------
# SeasonJob with a recording fake bot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_job_sends_in_order_and_summary(monkeypatch):
    players = {
        "https://yona.example/embed/1": [drive_src("HD")],
        "https://yona.example/embed/2": [drive_src("HD")],
    }
    patch_players(monkeypatch, players)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 1024]))

    bot = FakeBot()
    episodes = [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
        {"number": "2", "url": "https://witanime.example/ep-2", "type": "حلقة"},
    ]
    job = make_job(bot, episodes)
    await job.run()

    # first message = status with cancel button
    status = bot.sent_messages[0]
    assert status.text.startswith("📦 Test Anime")
    # videos sent in episode order
    assert len(bot.sent_videos) == 2
    assert "الحلقة 1" in bot.sent_videos[0]["caption"]
    assert "الحلقة 2" in bot.sent_videos[1]["caption"]
    assert bot.sent_videos[0]["kw"]["supports_streaming"] is True
    # final summary always sent
    summary = bot.sent_messages[-1].text
    assert "ملخص تحميل الموسم" in summary
    assert "✅ 2" in summary and "❌ 0" in summary
    assert [r[1] for r in job.results] == ["sent", "sent"]


@pytest.mark.asyncio
async def test_job_too_big_sends_links_and_continues(monkeypatch):
    players = {
        "https://yona.example/embed/1": [drive_src("HD")],
        "https://yona.example/embed/2": [drive_src("HD")],
    }
    patch_players(monkeypatch, players)
    # ep1 reports a huge Content-Length -> TooBig before any chunk is written
    big = FakeResp([b"x" * 64], total=10 * 1024 * 1024)
    small = FakeResp([b"x" * 64], total=64)
    resps = iter([big, small])
    monkeypatch.setattr(
        resolvers, "resolve_drive_mp4", lambda url: "https://drive.example/dl.mp4"
    )
    monkeypatch.setattr(
        resolvers, "get_session",
        lambda: FakeSession(next(resps)),
        raising=False,
    )

    too_big_calls = []
    bot = FakeBot()
    episodes = [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
        {"number": "2", "url": "https://witanime.example/ep-2", "type": "حلقة"},
    ]
    job = make_job(bot, episodes, max_bytes=1024, too_big_calls=too_big_calls)
    await job.run()

    assert len(too_big_calls) == 1
    assert too_big_calls[0][1] == int(big.headers["Content-Length"])
    assert len(bot.sent_videos) == 1  # ep2 still sent
    assert "الحلقة 2" in bot.sent_videos[0]["caption"]
    assert [r[1] for r in job.results] == ["links", "sent"]
    summary = bot.sent_messages[-1].text
    assert "🔗 1" in summary and "✅ 1" in summary


@pytest.mark.asyncio
async def test_job_cancel_mid_run_stops_and_summarizes(monkeypatch):
    players = {
        f"https://yona.example/embed/{n}": [drive_src("HD")] for n in (1, 2, 3)
    }
    patch_players(monkeypatch, players)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    holder = {}

    def cancel_after_first_video():
        holder["job"].cancel()

    bot = FakeBot(on_send_video=cancel_after_first_video)
    episodes = [
        {"number": str(n), "url": f"https://witanime.example/ep-{n}", "type": "حلقة"}
        for n in (1, 2, 3)
    ]
    job = make_job(bot, episodes)
    holder["job"] = job
    await job.run()

    assert len(bot.sent_videos) == 1  # stopped right after the first video
    assert [r[1] for r in job.results] == ["sent", "cancelled", "cancelled"]
    summary = bot.sent_messages[-1].text
    assert "ملخص تحميل الموسم" in summary
    assert "⛔ 2" in summary and "✅ 1" in summary
    # status message edited to the cancelled state
    assert "إلغاء" in job.status_msg.edits[-1]


@pytest.mark.asyncio
async def test_job_cancel_during_drive_download(monkeypatch):
    patch_players(monkeypatch, {"https://yona.example/embed/1": [drive_src("HD")]})
    holder = {}

    class CancellingResp(FakeResp):
        def iter_content(self, chunk_size=1):
            yield b"first-chunk"
            holder["job"].cancel()      # cancel lands between chunks
            yield b"second-chunk"

    monkeypatch.setattr(
        resolvers, "resolve_drive_mp4", lambda url: "https://drive.example/dl.mp4"
    )
    monkeypatch.setattr(
        resolvers, "get_session",
        lambda: FakeSession(CancellingResp([], total=32)),
        raising=False,
    )

    bot = FakeBot()
    job = make_job(bot, [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
    ])
    holder["job"] = job
    await job.run()

    assert len(bot.sent_videos) == 0
    assert [r[1] for r in job.results] == ["cancelled"]
    assert "⛔ 1" in bot.sent_messages[-1].text


@pytest.mark.asyncio
async def test_job_missing_quality_uses_nearest(monkeypatch):
    # wanted FHD but the episode only exposes HD -> nearest quality used
    patch_players(monkeypatch, {"https://yona.example/embed/1": [drive_src("HD")]})
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = FakeBot()
    job = make_job(bot, [
        {"number": "7", "url": "https://witanime.example/ep-1", "type": "حلقة"},
    ], wanted="FHD")
    await job.run()

    assert len(bot.sent_videos) == 1
    assert "جودة HD" in bot.sent_videos[0]["caption"]
    assert job.results == [("7", "sent", "HD", "")]


@pytest.mark.asyncio
async def test_job_no_suitable_quality_marks_failed_and_continues(monkeypatch):
    players = {
        "https://yona.example/embed/1": [],  # nothing at all
        "https://yona.example/embed/2": [drive_src("HD")],
    }
    patch_players(monkeypatch, players)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = FakeBot()
    job = make_job(bot, [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
        {"number": "2", "url": "https://witanime.example/ep-2", "type": "حلقة"},
    ])
    await job.run()

    assert [r[1] for r in job.results] == ["failed", "sent"]
    assert job.results[0][3] == "لا توجد جودة مناسبة"
    assert len(bot.sent_videos) == 1
    assert "❌ 1" in bot.sent_messages[-1].text


@pytest.mark.asyncio
async def test_job_mega_size_guard_sends_links(monkeypatch):
    patch_players(monkeypatch, {"https://yona.example/embed/1": [mega_src("HD")]})
    monkeypatch.setattr(resolvers, "get_mega_size", lambda url: 999_999_999)

    def forbidden_download(*a, **kw):  # must never start (guard fires first)
        raise AssertionError("download_mega should not be called")

    monkeypatch.setattr(resolvers, "download_mega", forbidden_download)

    too_big_calls = []
    bot = FakeBot()
    job = make_job(bot, [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
    ], max_bytes=1024, too_big_calls=too_big_calls)
    await job.run()

    assert len(too_big_calls) == 1
    assert too_big_calls[0][1] == 999_999_999
    assert [r[1] for r in job.results] == ["links"]
    assert len(bot.sent_videos) == 0
    assert "🔗 1" in bot.sent_messages[-1].text


@pytest.mark.asyncio
async def test_job_mega_inflight_oversize_marks_links(monkeypatch):
    # get_mega_size returns None -> the old code would download the whole
    # file and then throw it away. The in-flight guard must abort the mega
    # download as soon as the file on disk exceeds max_video_bytes and mark
    # the episode "links" (TooBig), NOT "cancelled".
    patch_players(monkeypatch, {"https://yona.example/embed/1": [mega_src("HD")]})
    monkeypatch.setattr(resolvers, "get_mega_size", lambda url: None)  # unknown size

    def fake_download_mega(url, path, state, cancel=None):
        written = 0
        with open(path, "wb") as fh:
            while True:
                if cancel and cancel():
                    raise resolvers.DownloadCancelled()
                chunk = b"x" * 512
                fh.write(chunk)
                fh.flush()
                written += len(chunk)
                state["done"] = written

    monkeypatch.setattr(resolvers, "download_mega", fake_download_mega)

    too_big_calls = []
    bot = FakeBot()
    job = make_job(bot, [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
    ], max_bytes=1024, too_big_calls=too_big_calls)
    await job.run()

    assert [r[1] for r in job.results] == ["links"]  # not "cancelled"
    assert len(too_big_calls) == 1
    assert too_big_calls[0][1] > 1024  # the on-disk size at abort time
    assert len(bot.sent_videos) == 0
    assert "🔗 1" in bot.sent_messages[-1].text


@pytest.mark.asyncio
async def test_job_final_status_edit_clears_cancel_button(monkeypatch):
    patch_players(monkeypatch, {"https://yona.example/embed/1": [drive_src("HD")]})
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = FakeBot()
    job = make_job(bot, [
        {"number": "1", "url": "https://witanime.example/ep-1", "type": "حلقة"},
    ])
    await job.run()

    assert job.status_msg.markups[-1] is not None
    assert not job.status_msg.markups[-1].inline_keyboard  # button cleared


def test_status_markup_cancel_button():
    job = make_job(FakeBot(), [])
    markup = job.build_status_markup()
    btn = markup.inline_keyboard[0][0]
    assert btn.text == "❌ إلغاء التحميل"
    assert btn.callback_data == "sdc|42"
    assert len(btn.callback_data.encode()) <= 64
