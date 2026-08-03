"""Flood-control send retries + the "🔁 إعادة الحلقات الفاشلة" button.

Covers: telegram.error.RetryAfter handled with retry_after+0.5s sleeps up to
SEND_MAX_ATTEMPTS, the retry button appearing on the batch summary ONLY when
episodes failed (never on full success / all-cancelled), the rf:<token>
callback guards (expired token, stranger, active job) and the re-offered
button after a retry batch that still has failures.
"""

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest
from telegram.error import RetryAfter

os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("BASE_PUBLIC_URL", "https://x.test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot as botmod  # noqa: E402
import season  # noqa: E402

from test_season import (  # noqa: E402
    FakeBot,
    FakeResp,
    make_ep,
    make_job,
    patch_drive_download,
)


@pytest.fixture(autouse=True)
def _clean_state():
    botmod.FAILED_RETRIES.clear()
    botmod.ACTIVE_JOBS.clear()
    season._SOURCES_CACHE.clear()
    yield
    botmod.FAILED_RETRIES.clear()
    botmod.ACTIVE_JOBS.clear()
    season._SOURCES_CACHE.clear()


@pytest.fixture
def sleep_log(monkeypatch):
    """Mock asyncio.sleep inside the tested module and record the delays."""
    log = []

    async def fake_sleep(delay, result=None):
        log.append(delay)
        return result

    monkeypatch.setattr(season.asyncio, "sleep", fake_sleep)
    return log


async def _fake_sources(ep):
    """Every episode offers one HD drive source (no network)."""
    return {"FHD": [], "SD": [],
            "HD": [{"kind": "drive", "url": "https://drive.example/v",
                    "host": "drive"}]}


async def _no_sources(ep):
    return {"FHD": [], "HD": [], "SD": []}


def _eps(*numbers):
    return [
        {"number": str(n), "url": f"https://witanime.example/ep-{n}", "type": "حلقة"}
        for n in numbers
    ]


def _rf_buttons(markup):
    if markup is None:
        return []
    return [
        b for row in markup.inline_keyboard for b in row
        if (b.callback_data or "").startswith("rf:")
    ]


class FloodBot(FakeBot):
    """send_video raises RetryAfter on the first `fail_times` calls."""

    def __init__(self, fail_times, retry_after=8):
        super().__init__()
        self.fail_times = fail_times
        self.retry_after = retry_after
        self.video_calls = 0

    async def send_video(self, chat_id=None, video=None, caption=None, **kw):
        self.video_calls += 1
        if self.video_calls <= self.fail_times:
            raise RetryAfter(self.retry_after)
        return await super().send_video(
            chat_id=chat_id, video=video, caption=caption, **kw
        )


class RecBot(FakeBot):
    """FakeBot that also records the reply_markup of every send_message."""

    async def send_message(self, chat_id=None, text=None, reply_markup=None, **kw):
        msg = await super().send_message(
            chat_id=chat_id, text=text, reply_markup=reply_markup, **kw
        )
        msg.sent_markup = reply_markup
        return msg


class FakeQueryMessage:
    def __init__(self, chat_id=123):
        self.chat_id = chat_id
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


class FakeCallbackQuery:
    def __init__(self, data, user_id, bot_obj, chat_id=123):
        self.data = data
        self._bot = bot_obj
        self.message = FakeQueryMessage(chat_id)
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append({"text": text, "show_alert": show_alert})

    def get_bot(self):
        return self._bot


def make_update(data, user_id, bot_obj):
    q = FakeCallbackQuery(data, user_id, bot_obj)
    update = SimpleNamespace(
        callback_query=q,
        effective_user=SimpleNamespace(id=user_id, full_name="U", username="u"),
    )
    return update, q


def seed_token(token="tok123", user_id=42, chat_id=555, numbers=("3", "7")):
    botmod.FAILED_RETRIES[token] = {
        "chat_id": chat_id,
        "message_thread_id": None,
        "user_id": user_id,
        "anime_title": "Test Anime",
        "wanted_quality": "FHD",
        "episodes": _eps(*numbers),
    }


# ---------------------------------------------------------------------------
# 1+2. RetryAfter wrapper on the batch send path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_after_once_then_success(monkeypatch, sleep_log):
    monkeypatch.setattr(season, "episode_quality_sources", _fake_sources)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = FloodBot(fail_times=1, retry_after=8)
    job = make_job(bot, _eps(1))
    await job.run()

    assert job.results[0][1] == "sent"
    assert bot.video_calls == 2        # one RetryAfter + one successful send
    assert sleep_log == [8.5]          # retry_after (8s) + 0.5s grace
    assert len(bot.sent_videos) == 1


@pytest.mark.asyncio
async def test_retry_after_exhausted_marks_failed(monkeypatch, sleep_log):
    monkeypatch.setattr(season, "episode_quality_sources", _fake_sources)
    monkeypatch.setattr(season, "SEND_MAX_ATTEMPTS", 2)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = FloodBot(fail_times=99, retry_after=2)
    job = make_job(bot, _eps(1))
    await job.run()

    assert bot.video_calls == 2        # SEND_MAX_ATTEMPTS total attempts
    assert sleep_log == [2.5]          # slept once, then gave up
    assert job.results[0][1] == "failed"
    assert "Flood control" in job.results[0][3]
    assert not bot.sent_videos


# ---------------------------------------------------------------------------
# 3. Summary retry button appears ONLY when there are failed episodes
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_has_retry_button_with_failed_eps_only(monkeypatch):
    async def sources(ep):  # ep-1 has nothing, ep-2 has a drive source
        if ep["url"].endswith("ep-1"):
            return await _no_sources(ep)
        return await _fake_sources(ep)

    monkeypatch.setattr(season, "episode_quality_sources", sources)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = RecBot()
    job = make_job(bot, _eps(1, 2))
    job.summary_markup = botmod._retry_failed_markup
    await job.run()

    assert [r[1] for r in job.results] == ["failed", "sent"]
    summary = job.summary_message
    assert summary is bot.sent_messages[-1]
    buttons = _rf_buttons(summary.sent_markup)
    assert len(buttons) == 1
    assert buttons[0].text == "🔁 إعادة الحلقات الفاشلة (1)"
    assert len(buttons[0].callback_data.encode()) <= 64
    token = buttons[0].callback_data[len("rf:"):]
    ctx = botmod.FAILED_RETRIES[token]
    assert ctx["user_id"] == 42 and ctx["chat_id"] == 123
    assert ctx["wanted_quality"] == "HD"
    assert ctx["anime_title"] == "Test Anime"
    assert [e["number"] for e in ctx["episodes"]] == ["1"]  # failed only


@pytest.mark.asyncio
async def test_summary_no_retry_button_when_all_sent(monkeypatch):
    monkeypatch.setattr(season, "episode_quality_sources", _fake_sources)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = RecBot()
    job = make_job(bot, _eps(1, 2))
    job.summary_markup = botmod._retry_failed_markup
    await job.run()

    assert [r[1] for r in job.results] == ["sent", "sent"]
    assert job.summary_message.sent_markup is None
    assert not botmod.FAILED_RETRIES


@pytest.mark.asyncio
async def test_summary_no_retry_button_when_all_cancelled(monkeypatch):
    monkeypatch.setattr(season, "episode_quality_sources", _fake_sources)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    bot = RecBot()
    job = make_job(bot, _eps(1, 2))
    job.summary_markup = botmod._retry_failed_markup
    job.cancel()  # cancel before the run -> every episode marked cancelled
    await job.run()

    assert [r[1] for r in job.results] == ["cancelled", "cancelled"]
    assert job.summary_message.sent_markup is None  # cancelled != retryable
    assert not botmod.FAILED_RETRIES


# ---------------------------------------------------------------------------
# 4+5. rf:<token> callback
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_callback_launches_batch_with_failed_eps_only(monkeypatch):
    monkeypatch.setattr(season, "episode_quality_sources", _fake_sources)
    patch_drive_download(monkeypatch, FakeResp([b"x" * 64]))

    async def fake_fetch(ep_url):
        return make_ep(ep_url.rsplit("-", 1)[-1])

    monkeypatch.setattr(botmod, "fetch_episode", fake_fetch)

    launched = []
    orig_launch = botmod._launch_batch

    def spy(*args, **kwargs):
        job = orig_launch(*args, **kwargs)
        launched.append(job)
        return job

    monkeypatch.setattr(botmod, "_launch_batch", spy)
    seed_token("tok123", user_id=42, chat_id=555, numbers=("3", "7"))

    bot = RecBot()
    update, q = make_update("rf:tok123", 42, bot)
    context = SimpleNamespace(bot_data={"failed_retries": botmod.FAILED_RETRIES})

    await botmod.retry_failed_cb(update, context)

    assert len(launched) == 1
    job = launched[0]
    assert [e["number"] for e in job.episodes] == ["3", "7"]  # failed only
    assert job.user_id == 42 and job.chat_id == 555
    assert job.wanted_quality == "FHD"           # same quality as before
    assert "tok123" not in botmod.FAILED_RETRIES  # token consumed
    assert q.answers[-1]["text"].startswith("🔁")

    await asyncio.gather(*list(botmod.BATCH_TASKS))
    assert [r[1] for r in job.results] == ["sent", "sent"]
    assert len(bot.sent_videos) == 2


@pytest.mark.asyncio
async def test_retry_callback_rejects_stranger_and_unknown_token(monkeypatch):
    launched = []
    monkeypatch.setattr(
        botmod, "_launch_batch", lambda *a, **k: launched.append((a, k))
    )
    seed_token("tok123", user_id=42)
    context = SimpleNamespace(bot_data={"failed_retries": botmod.FAILED_RETRIES})

    # a stranger pressing the button -> refusal, nothing starts, token kept
    update, q = make_update("rf:tok123", 999, RecBot())
    await botmod.retry_failed_cb(update, context)
    assert q.answers[-1] == {"text": "مش أنت صاحب التحميل ده", "show_alert": True}
    assert "tok123" in botmod.FAILED_RETRIES
    assert not launched

    # unknown / consumed token -> expired message
    update, q = make_update("rf:deadbeef00", 42, RecBot())
    await botmod.retry_failed_cb(update, context)
    assert q.answers[-1]["show_alert"] is True
    assert "انتهت صلاحية الزرار" in q.answers[-1]["text"]
    assert not launched


@pytest.mark.asyncio
async def test_retry_callback_blocked_while_job_active(monkeypatch):
    launched = []
    monkeypatch.setattr(
        botmod, "_launch_batch", lambda *a, **k: launched.append((a, k))
    )
    seed_token("tok123", user_id=42)
    botmod.ACTIVE_JOBS[42] = object()  # a batch is already running
    context = SimpleNamespace(bot_data={"failed_retries": botmod.FAILED_RETRIES})

    update, q = make_update("rf:tok123", 42, RecBot())
    await botmod.retry_failed_cb(update, context)
    assert q.answers[-1]["show_alert"] is True
    assert "فيه تحميل شغال دلوقتي" in q.answers[-1]["text"]
    assert "tok123" in botmod.FAILED_RETRIES  # not consumed
    assert not launched


# ---------------------------------------------------------------------------
# 6. A retry batch that still has failures re-offers a fresh button/token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retry_batch_reoffers_button_with_new_token(monkeypatch):
    monkeypatch.setattr(season, "episode_quality_sources", _no_sources)

    async def fake_fetch(ep_url):
        return make_ep(ep_url.rsplit("-", 1)[-1])

    monkeypatch.setattr(botmod, "fetch_episode", fake_fetch)

    # original batch: both episodes fail (no sources) -> button with token1
    bot = RecBot()
    job = make_job(bot, _eps(1, 2))
    job.summary_markup = botmod._retry_failed_markup
    await job.run()
    assert [r[1] for r in job.results] == ["failed", "failed"]
    btn1 = _rf_buttons(job.summary_message.sent_markup)[0]
    token1 = btn1.callback_data[len("rf:"):]
    assert token1 in botmod.FAILED_RETRIES

    # press it -> the retry batch fails again -> a NEW token must be offered
    update, q = make_update(f"rf:{token1}", 42, bot)
    context = SimpleNamespace(bot_data={"failed_retries": botmod.FAILED_RETRIES})
    await botmod.retry_failed_cb(update, context)
    await asyncio.gather(*list(botmod.BATCH_TASKS))

    assert token1 not in botmod.FAILED_RETRIES   # consumed
    assert len(botmod.FAILED_RETRIES) == 1
    token2 = next(iter(botmod.FAILED_RETRIES))
    assert token2 != token1
    btn2 = _rf_buttons(bot.sent_messages[-1].sent_markup)[0]
    assert btn2.callback_data == f"rf:{token2}"
    assert btn2.text == "🔁 إعادة الحلقات الفاشلة (2)"


# ---------------------------------------------------------------------------
# wiring
# ---------------------------------------------------------------------------

def test_rf_handler_registered_before_generic_dispatcher():
    from telegram.ext import CallbackQueryHandler

    app = botmod.build_application()
    cbs = [
        h for h in app.handlers[min(app.handlers)]
        if isinstance(h, CallbackQueryHandler)
    ]
    assert cbs[0].callback is botmod.retry_failed_cb
    assert cbs[1].callback is botmod.on_callback
