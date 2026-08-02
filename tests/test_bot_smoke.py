"""Smoke tests for bot.py wiring (SPEC §tests — S3).

Sets the required env vars BEFORE importing bot, then exercises the keyboard
builders, callback-data length limits, env parsing, the force-sub markup, and
Application construction with all handlers registered (no polling).
"""

import importlib
import os
import sys

import pytest

os.environ.setdefault("BOT_TOKEN", "123:ABC")
os.environ.setdefault("BASE_PUBLIC_URL", "https://x.test")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


def _fake_info(n: int = 25) -> dict:
    return {
        "title": "Test Anime",
        "episodes": [
            {
                "number": str(i),
                "url": f"https://witanime.life/episode/test-{i}/",
                "type": "الحلقة",
            }
            for i in range(1, n + 1)
        ],
    }


def _all_callbacks(markup) -> list[str]:
    return [
        b.callback_data
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data
    ]


def _all_labels(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# episodes_keyboard
# ---------------------------------------------------------------------------

def test_episodes_keyboard_has_season_and_community_rows():
    kb = bot.episodes_keyboard("https://witanime.life/anime/test-anime/", _fake_info(), 0)
    labels = _all_labels(kb)
    assert "📦 الموسم كامل" in labels
    assert "🎯 مدى حلقات" in labels
    assert "⭐ مفضلة" in labels
    assert "🔔 متابعة" in labels
    # season/fav/fol callbacks point at the anime via slug
    cbs = _all_callbacks(kb)
    assert "sdl|test-anime" in cbs
    assert "sdr|test-anime" in cbs
    assert "fav|test-anime" in cbs
    assert "fol|test-anime" in cbs


def test_episodes_keyboard_callbacks_fit_64_bytes():
    long_slug = "a" * 80  # slug too long -> token fallback must be used
    url = f"https://witanime.life/anime/{long_slug}/"
    kb = bot.episodes_keyboard(url, _fake_info(), 0)
    for cb in _all_callbacks(kb):
        assert len(cb.encode("utf-8")) <= 64, cb
    # token fallback for the long slug
    assert any(cb.startswith("fav|") and len(cb) < 20 for cb in _all_callbacks(kb))


def test_episodes_keyboard_last_page():
    kb = bot.episodes_keyboard("https://witanime.life/anime/test-anime/", _fake_info(), 1)
    for cb in _all_callbacks(kb):
        assert len(cb.encode("utf-8")) <= 64, cb


# ---------------------------------------------------------------------------
# search_keyboard
# ---------------------------------------------------------------------------

def test_search_keyboard():
    results = [
        {"title": f"Anime {i}", "url": f"https://witanime.life/anime/anime-{i}/"}
        for i in range(15)
    ]
    kb = bot.search_keyboard("tok12345", results, 0)
    labels = _all_labels(kb)
    assert any("Anime 0" in lbl for lbl in labels)
    assert "التالي ▶️" in labels
    for cb in _all_callbacks(kb):
        assert len(cb.encode("utf-8")) <= 64, cb


# ---------------------------------------------------------------------------
# latest / range / quality / confirm keyboards
# ---------------------------------------------------------------------------

def test_latest_keyboard():
    items = [
        {
            "anime_title": f"Anime {i}",
            "ep_title": f"الحلقة {i}",
            "ep_url": f"https://witanime.life/episode/a-{i}/",
            "anime_url": f"https://witanime.life/anime/a-{i}/",
            "screenshot": "",
        }
        for i in range(25)
    ]
    kb = bot.latest_keyboard(items, 0)
    labels = _all_labels(kb)
    assert len([lbl for lbl in labels if "—" in lbl]) == bot.LATEST_PER_PAGE
    assert all(len(lbl.replace("...", "")) <= 40 for lbl in labels if "—" in lbl)
    for cb in _all_callbacks(kb):
        assert len(cb.encode("utf-8")) <= 64, cb


def test_range_grid_keyboard_paged():
    url = "https://witanime.life/anime/test-anime/"
    kb1 = bot.range_grid_keyboard(_fake_info(45)["episodes"], url, step=1, page=0)
    kb2 = bot.range_grid_keyboard(_fake_info(45)["episodes"], url, step=1, page=1)
    kb3 = bot.range_grid_keyboard(_fake_info(45)["episodes"], url, step=1, page=2)
    assert any(cb.startswith("sdr1|") for cb in _all_callbacks(kb1))
    assert any(cb.startswith("sdr1p|1|") for cb in _all_callbacks(kb1))  # next page
    assert any(cb.startswith("sdr1p|0|") for cb in _all_callbacks(kb2))  # prev page
    assert "التالي ▶️" not in _all_labels(kb3)  # last page
    for kb in (kb1, kb2, kb3):
        for cb in _all_callbacks(kb):
            assert len(cb.encode("utf-8")) <= 64, cb


def test_range_grid_step2_has_back_button():
    tok = "abcd1234"
    bot.RANGE_TOKENS[tok] = {
        "first_url": "https://witanime.life/episode/test-1/",
        "first_num": 1.0,
        "anime_url": "https://witanime.life/anime/test-anime/",
    }
    kb = bot.range_grid_keyboard(
        _fake_info(10)["episodes"], "https://witanime.life/anime/test-anime/", step=2, page=0,
        first_tok=tok,
    )
    cbs = _all_callbacks(kb)
    assert any(cb.startswith("sdr2|") for cb in cbs)
    assert f"sdrre|{tok}" in cbs


def test_season_quality_keyboard_only_available():
    scan = {
        "eps": [{"url": "u", "number": "1", "type": "الحلقة", "sources": {}}] * 12,
        "counts": {"FHD": 12, "HD": 0},
        "truncated": False,
        "total": 12,
    }
    kb = bot.season_quality_keyboard(scan, "tok12345")
    labels = _all_labels(kb)
    assert labels == ["FHD (متاحة في 12/12)"]  # HD hidden (count 0)
    assert "sdq|tok12345|FHD" in _all_callbacks(kb)


def test_season_confirm_keyboard():
    kb = bot.season_confirm_keyboard("tok12345", "FHD")
    cbs = _all_callbacks(kb)
    assert "sgo|tok12345|FHD" in cbs
    assert "scl|tok12345" in cbs


# ---------------------------------------------------------------------------
# config parsing / builders
# ---------------------------------------------------------------------------

def test_admin_ids_parsing():
    assert bot._parse_admin_ids("") == set()
    assert bot._parse_admin_ids("1, 22 ,333") == {1, 22, 333}
    assert bot._parse_admin_ids("abc,5,, 7x") == {5}


def test_force_sub_keyboard():
    old = bot.FORCE_SUB_CHANNEL
    try:
        bot.FORCE_SUB_CHANNEL = "@mychan"
        kb = bot.force_sub_keyboard()
        labels = _all_labels(kb)
        assert "📢 انضم للقناة" in labels
        assert "✅ تحققت" in labels
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        assert urls == ["https://t.me/mychan"]
        assert "fsub|check" in _all_callbacks(kb)
    finally:
        bot.FORCE_SUB_CHANNEL = old


def test_start_keyboard():
    cbs = _all_callbacks(bot.start_keyboard())
    assert "late|0" in cbs
    assert "favlist" in cbs
    assert "follist" in cbs


def test_anime_list_keyboard():
    kb = bot.anime_list_keyboard(
        [{"url": "https://witanime.life/anime/test-anime/", "title": "Test Anime"}]
    )
    assert "anime|test-anime" in _all_callbacks(kb)


# ---------------------------------------------------------------------------
# application wiring
# ---------------------------------------------------------------------------

def test_build_application_registers_handlers():
    app = bot.build_application()
    assert app is not None
    handlers = [h for group in app.handlers.values() for h in group]
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

    commands = {
        cmd
        for h in handlers
        if isinstance(h, CommandHandler)
        for cmd in h.commands
    }
    assert {"start", "favorites", "follows", "stats", "ban", "unban", "broadcast"} <= commands
    assert any(isinstance(h, MessageHandler) for h in handlers)
    assert any(isinstance(h, CallbackQueryHandler) for h in handlers)
    assert app.post_init is bot.post_init
    assert app.post_shutdown is bot.post_shutdown
