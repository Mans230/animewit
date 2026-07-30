"""search-bot entry point: python bot.py

Runs PTB polling + the FastAPI watch server (uvicorn) in the same asyncio
loop (SPEC §6/§7). All bot UI texts are Arabic; code/comments in English.
"""

import asyncio
import collections
import contextlib
import logging
import os
import re
import sys
import tempfile
import time
import uuid

import requests
import uvicorn
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import resolvers
import scraper
from server import build_app, make_watch_url

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("search-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
BASE_PUBLIC_URL = os.environ.get("BASE_PUBLIC_URL", "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
WATCH_SECRET = os.environ.get("WATCH_SECRET", "").strip() or BOT_TOKEN

# Optional local Bot API server (telegram-bot-api --local, see Dockerfile):
# when TG_API_ID/TG_API_HASH (from my.telegram.org) are set, the container
# starts a local Bot API on :8081 and the bot talks to it -> 2GB file limit.
TG_API_ID = os.environ.get("TG_API_ID", "").strip()
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
LOCAL_BOT_API = bool(TG_API_ID and TG_API_HASH)
LOCAL_API_URL = "http://localhost:8081"

# ---------------------------------------------------------------------------
# In-memory caches (bounded, FIFO eviction)
# ---------------------------------------------------------------------------
MAX_CACHE = 5000
ANIME_TTL = 3600  # seconds; re-fetch anime info after an hour (new episodes)
URL_CACHE: "collections.OrderedDict[str, str]" = collections.OrderedDict()
SEARCH_CACHE: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()
ANIME_CACHE: "collections.OrderedDict[str, tuple]" = collections.OrderedDict()


def _bounded_put(cache: collections.OrderedDict, key: str, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > MAX_CACHE:
        cache.popitem(last=False)


def put_url(url: str) -> str:
    """Store a URL in memory and return a short token for callback_data."""
    token = uuid.uuid4().hex[:8]
    _bounded_put(URL_CACHE, token, url)
    return token


def get_url(token: str) -> str | None:
    return URL_CACHE.get(token)


# ---------------------------------------------------------------------------
# UI constants (Arabic)
# ---------------------------------------------------------------------------
ERR_FETCH = "حدث خطأ أثناء الجلب، حاول مرة أخرى 🙏"
ERR_EXPIRED = "انتهت صلاحية هذه الأزرار، ابحث من جديد 🔍"
MSG_PREPARING = "جاري تجهيز الفيديو... ⏳"
MSG_SENDING = "📤 جاري إرسال الفيديو إلى تليجرام..."
MSG_RESOLVE_FAIL = "تعذّر تجهيز الفيديو من هذا المصدر — جرّب جودة أخرى أو استخدم المشاهدة/التحميل 🎬⬇️"

RESULTS_PER_PAGE = 10
EPS_PER_PAGE = 20
# Defaults: cloud Bot API limits. Upgraded in main() if the local API is reachable.
MAX_VIDEO_BYTES = 45 * 1024 * 1024  # 45MB safety margin (cloud Bot API limit: 50MB)
VIDEO_TOTAL_TIMEOUT = 600  # seconds


def _wait_for_local_api(timeout_s: int = 30) -> bool:
    """Wait until the local telegram-bot-api (started by the Docker CMD) accepts
    TCP connections on :8081. Returns False on timeout so we can fall back to the
    cloud Bot API instead of crash-looping."""
    import socket
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 8081), timeout=2):
                return True
        except OSError:
            time.sleep(1.5)
    return False
# Limit concurrent video downloads (each writes to a temp file in /tmp).
VIDEO_SEND_SEM = asyncio.Semaphore(2)


# ---------------------------------------------------------------------------
# Callback-data helpers (must stay <= 64 bytes)
# ---------------------------------------------------------------------------
def slug_from_url(url: str) -> str | None:
    """Extract the slug from a witanime /anime/<slug>/ URL."""
    if "/anime/" not in url:
        return None
    slug = url.rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[A-Za-z0-9\-%.~_]+", slug):
        return None
    return slug


def anime_url_from_slug(slug: str) -> str:
    return f"{scraper.BASE}/anime/{slug}/"


def fits(data: str) -> bool:
    return len(data.encode("utf-8")) <= 64


def anime_cb(anime_url: str) -> str:
    """callback_data to open an anime page (slug preferred, token fallback)."""
    slug = slug_from_url(anime_url)
    if slug and fits(f"anime|{slug}"):
        return f"anime|{slug}"
    return f"a|{put_url(anime_url)}"


def eps_cb(anime_url: str, page: int, ep_url: str | None = None) -> str:
    """callback_data to open the episode list of an anime at a given page.

    If ep_url is given, jump to the page containing that episode.
    """
    if ep_url:
        entry = ANIME_CACHE.get(anime_url)
        if entry:
            for i, e in enumerate(entry[1]["episodes"]):
                if e["url"] == ep_url:
                    page = i // EPS_PER_PAGE
                    break
    slug = slug_from_url(anime_url)
    if slug and fits(f"eps|{slug}|{page}"):
        return f"eps|{slug}|{page}"
    return f"es|{put_url(anime_url)}|{page}"


def resolve_anime_ref(kind: str, ref: str) -> str | None:
    """Resolve an anime reference from callback_data to a full URL."""
    if kind in ("anime", "eps"):
        return anime_url_from_slug(ref)
    return get_url(ref)  # kinds "a" / "es" -> token


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------
def search_keyboard(token: str, results: list[dict], page: int) -> InlineKeyboardMarkup:
    start = page * RESULTS_PER_PAGE
    chunk = results[start : start + RESULTS_PER_PAGE]
    rows = []
    for r in chunk:
        label = r["title"]
        if len(label) > 45:
            label = label[:42] + "..."
        rows.append([InlineKeyboardButton(f"🎬 {label}", callback_data=anime_cb(r["url"]))])
    total_pages = (len(results) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"s|{token}|{page - 1}"))
    nav.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"s|{token}|{page + 1}"))
    if len(nav) > 1:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def episodes_keyboard(anime_url: str, info: dict, page: int) -> InlineKeyboardMarkup:
    episodes = info["episodes"]
    start = page * EPS_PER_PAGE
    chunk = episodes[start : start + EPS_PER_PAGE]
    rows = []
    row = []
    for ep in chunk:
        label = f"{ep['number']}" if ep["type"] == "الحلقة" else f"{ep['number']} ⭐"
        row.append(InlineKeyboardButton(label, callback_data=f"ep|{put_url(ep['url'])}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    total_pages = (len(episodes) + EPS_PER_PAGE - 1) // EPS_PER_PAGE
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=eps_cb(anime_url, page - 1)))
    nav.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=eps_cb(anime_url, page + 1)))
    if len(nav) > 1:
        rows.append(nav)
    if episodes:
        last = episodes[-1]
        rows.append(
            [InlineKeyboardButton("⏭ آخر حلقة", callback_data=f"ep|{put_url(last['url'])}")]
        )
    if not rows:  # Telegram rejects empty keyboards with HTTP 400
        rows.append([InlineKeyboardButton("📭 لا توجد حلقات بعد", callback_data="noop")])
    return InlineKeyboardMarkup(rows)


def episode_panel_keyboard(ep: dict) -> InlineKeyboardMarkup:
    tok = put_url(ep["_ep_url"])
    rows = [
        [
            InlineKeyboardButton("🎬 مشاهدة (سيرفرات)", callback_data=f"srv|{tok}"),
            InlineKeyboardButton("⬇️ تحميل", callback_data=f"dl|{tok}"),
        ],
        [InlineKeyboardButton("📤 إرسال الفيديو هنا", callback_data=f"vid|{tok}")],
    ]
    nav = []
    if ep.get("prev_url"):
        nav.append(InlineKeyboardButton("◀️ السابقة", callback_data=f"ep|{put_url(ep['prev_url'])}"))
    if ep.get("next_url"):
        nav.append(InlineKeyboardButton("التالية ▶️", callback_data=f"ep|{put_url(ep['next_url'])}"))
    if nav:
        rows.append(nav)
    if ep.get("anime_url"):
        rows.append(
            [InlineKeyboardButton("🔙 رجوع للحلقات", callback_data=eps_cb(ep["anime_url"], 0, ep["_ep_url"]))]
        )
    return InlineKeyboardMarkup(rows)


def episode_panel_text(ep: dict) -> str:
    return (
        f"🎬 {ep['anime_title'] or ep['title']}\n"
        f"📺 الحلقة {ep['number']}\n\n"
        "اختر من الأسفل 👇"
    )


def servers_keyboard(entries: list[tuple[str, str]], tok: str) -> InlineKeyboardMarkup:
    """entries: [(button label, embed_url)] — each opens our /watch page."""
    rows = []
    for label, embed_url in entries:
        url = make_watch_url(BASE_PUBLIC_URL, embed_url, WATCH_SECRET)
        rows.append([InlineKeyboardButton(f"▶️ {label}", url=url)])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"panel|{tok}")])
    return InlineKeyboardMarkup(rows)


def video_qualities_keyboard(options: list[dict], tok: str) -> InlineKeyboardMarkup:
    """options: [{"label", "kind", "url"}] — each starts the video-send flow."""
    rows = []
    for opt in options:
        qtok = put_url(f"{tok}|{opt['kind']}|{opt['url']}")
        rows.append([InlineKeyboardButton(f"📤 {opt['label']}", callback_data=f"vq|{qtok}")])
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"panel|{tok}")])
    return InlineKeyboardMarkup(rows)


def downloads_keyboard(ep: dict, tok: str | None) -> InlineKeyboardMarkup:
    """One row per quality: quality label (noop) + host URL buttons."""
    rows = []
    current_quality = None
    row: list[InlineKeyboardButton] = []
    for d in ep["downloads"]:
        if d["quality"] != current_quality:
            if row:
                rows.append(row)
            current_quality = d["quality"]
            row = [InlineKeyboardButton(f"💾 {current_quality}", callback_data="noop")]
        if len(row) < 5:
            row.append(InlineKeyboardButton(d["host"], url=d["url"]))
        else:  # very unlikely: overflow to a fresh row for the same quality
            rows.append(row)
            row = [InlineKeyboardButton(f"💾 {current_quality}", callback_data="noop"),
                   InlineKeyboardButton(d["host"], url=d["url"])]
    if row:
        rows.append(row)
    if tok is not None:
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"panel|{tok}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Scraper wrappers (blocking requests -> thread, friendly errors)
# ---------------------------------------------------------------------------
async def fetch_anime_info(anime_url: str) -> dict:
    entry = ANIME_CACHE.get(anime_url)
    if entry is not None:
        ts, info = entry
        if time.monotonic() - ts < ANIME_TTL:
            return info
    info = await asyncio.to_thread(scraper.get_anime_info, anime_url)
    _bounded_put(ANIME_CACHE, anime_url, (time.monotonic(), info))
    return info


async def fetch_episode(ep_url: str) -> dict:
    ep = await asyncio.to_thread(scraper.get_episode, ep_url)
    ep["_ep_url"] = ep_url
    return ep


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "أهلاً بك في بوت ويت أنمي 🎌\n"
        "ابعت اسم الأنمي للبحث، واختر من النتائج لتصفح الحلقات والمشاهدة والتحميل 🍿"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        return
    try:
        results = await asyncio.to_thread(scraper.search_anime, query)
    except scraper.ScraperError:
        log.exception("search failed for %r", query)
        await update.message.reply_text(ERR_FETCH)
        return
    if not results:
        await update.message.reply_text(f"لم يتم العثور على نتائج لـ «{query}» 😕 جرّب اسمًا آخر")
        return
    token = uuid.uuid4().hex[:8]
    _bounded_put(SEARCH_CACHE, token, (query, results))
    text = f"🔍 نتائج البحث عن «{query}» — عدد النتائج: {len(results)}"
    await update.message.reply_text(text, reply_markup=search_keyboard(token, results, 0))


async def open_anime(query_obj, anime_url: str) -> None:
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.message.reply_text(ERR_FETCH)
        return
    story = info["story"]
    if len(story) > 400:
        story = story[:400].rstrip() + "..."
    caption = f"🎬 {info['title']}\n\n{story}"
    if info["genres"]:
        caption += "\n\n🏷 " + "، ".join(info["genres"])
    caption += f"\n\n📺 عدد الحلقات: {len(info['episodes'])}"
    if info["episodes"]:
        markup: InlineKeyboardMarkup | None = episodes_keyboard(anime_url, info, 0)
    else:
        # Never pass an empty keyboard to Telegram (API rejects it with 400)
        caption += "\n\n📭 لا توجد حلقات متاحة لهذا الأنمي بعد."
        markup = None
    try:
        if info["poster"]:
            await query_obj.message.reply_photo(
                photo=info["poster"], caption=caption, reply_markup=markup
            )
        else:
            await query_obj.message.reply_text(caption, reply_markup=markup)
    except Exception as exc:  # poster URL broken etc. -> fall back to text
        log.warning("send photo failed (%s), falling back to text", exc)
        await query_obj.message.reply_text(caption, reply_markup=markup)


async def show_episodes_page(query_obj, anime_url: str, page: int) -> None:
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    total_pages = max(1, (len(info["episodes"]) + EPS_PER_PAGE - 1) // EPS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    try:
        await query_obj.edit_message_reply_markup(
            reply_markup=episodes_keyboard(anime_url, info, page)
        )
    except Exception as exc:
        log.debug("edit markup failed: %s", exc)


async def _ensure_anime_cached(ep: dict) -> None:
    """Make sure anime info is cached so the 'back to episodes' button can
    jump to the page containing this episode."""
    anime_url = ep.get("anime_url")
    if anime_url and anime_url not in ANIME_CACHE:
        with contextlib.suppress(scraper.ScraperError):
            await fetch_anime_info(anime_url)


async def open_episode(query_obj, ep_url: str) -> None:
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.message.reply_text(ERR_FETCH)
        return
    await _ensure_anime_cached(ep)
    await query_obj.message.reply_text(
        episode_panel_text(ep), reply_markup=episode_panel_keyboard(ep)
    )


# --- yonaplay players cache (avoids re-fetching on every menu open) ----------
_YONAPLAY_CACHE: collections.OrderedDict[str, tuple[float, list[dict]]] = collections.OrderedDict()
_YONAPLAY_CACHE_TTL = 6 * 3600  # seconds
_YONAPLAY_CACHE_MAX = 500


async def _yonaplay_players_cached(embed_url: str) -> list[dict]:
    """get_yonaplay_players with a small TTL cache (thread-safe enough for our
    single-loop usage)."""
    now = time.time()
    entry = _YONAPLAY_CACHE.get(embed_url)
    if entry and now - entry[0] < _YONAPLAY_CACHE_TTL:
        _YONAPLAY_CACHE.move_to_end(embed_url)
        return entry[1]
    players = await asyncio.to_thread(scraper.get_yonaplay_players, embed_url)
    _YONAPLAY_CACHE[embed_url] = (now, players)
    _YONAPLAY_CACHE.move_to_end(embed_url)
    while len(_YONAPLAY_CACHE) > _YONAPLAY_CACHE_MAX:
        _YONAPLAY_CACHE.popitem(last=False)
    return players


async def _expand_yonaplay_all(servers: list[dict]) -> dict[str, list[dict]]:
    """Fetch yonaplay players for ALL yonaplay servers in parallel (never
    sequentially — a slow/blocked host must not freeze the menu)."""
    yona = [s for s in servers if "yonaplay" in s["name"].lower()]
    if not yona:
        return {}
    results = await asyncio.gather(
        *(_yonaplay_players_cached(s["embed_url"]) for s in yona),
        return_exceptions=True,
    )
    out = {}
    for s, r in zip(yona, results):
        out[s["embed_url"]] = r if isinstance(r, list) else []
    return out


# Watch-menu ordering: clean direct players first, ad-heavy hosts last.
_WATCH_PRIORITY = [
    ("drive.google", 0),
    ("mega.nz", 1),
    ("4shared", 2),
    ("dailymotion", 3),
    ("ok.ru", 4),
    ("videa.hu", 5),
    ("videas.fr", 6),
    ("hgcloud", 8), ("streamwish", 8),
]


def _watch_rank(embed_url: str) -> int:
    u = embed_url.lower()
    for needle, rank in _WATCH_PRIORITY:
        if needle in u:
            return rank
    return 7


async def _watch_entries(ep: dict) -> list[tuple[str, str]]:
    """(label, embed_url) pairs for the watch menu. yonaplay servers are
    expanded into their inner players (e.g. 'Google Drive - HD'); every other
    server stays a single button. Entries are de-duplicated and ordered
    clean-players-first (streamwish/ads last)."""
    players_map = await _expand_yonaplay_all(ep["servers"])
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, url: str) -> None:
        if url and url not in seen:
            seen.add(url)
            entries.append((label, url))

    for s in ep["servers"]:
        if "yonaplay" in s["name"].lower():
            for p in players_map.get(s["embed_url"], []):
                add(f"{p['host']} - {p['quality']}", p["url"])
            # raw yonaplay is dropped on purpose: it 404s without a witanime
            # Referer (black screen) — its inner players replace it.
            continue
        add(s["name"], s["embed_url"])

    entries.sort(key=lambda e: _watch_rank(e[1]))
    return entries


async def _video_options(ep: dict) -> list[dict]:
    """Quality choices for 'send video here': Google Drive links (HD/FHD)
    extracted from yonaplay players + ok.ru (best quality on resolve)."""
    players_map = await _expand_yonaplay_all(ep["servers"])
    options = []
    seen_drive = set()
    seen_mega = set()
    for s in ep["servers"]:
        name = s["name"].lower()
        if "yonaplay" in name:
            for p in players_map.get(s["embed_url"], []):
                if "drive.google" in p["url"] and p["url"] not in seen_drive:
                    seen_drive.add(p["url"])
                    options.append(
                        {
                            "label": f"Google Drive - {p['quality']}",
                            "kind": "drive",
                            "url": p["url"],
                        }
                    )
                elif "mega.nz" in p["url"] and p["url"] not in seen_mega:
                    seen_mega.add(p["url"])
                    options.append(
                        {
                            "label": f"Mega - {p['quality']}",
                            "kind": "mega",
                            "url": p["url"],
                        }
                    )
        elif "ok.ru" in name or "odnoklassniki" in name or "ok.ru" in s["embed_url"]:
            options.append(
                {"label": f"{s['name']} - أفضل جودة", "kind": "okru", "url": s["embed_url"]}
            )
        elif "mega.nz" in s["embed_url"] and s["embed_url"] not in seen_mega:
            seen_mega.add(s["embed_url"])
            options.append(
                {"label": s["name"], "kind": "mega", "url": s["embed_url"]}
            )
    return options


async def show_servers(query_obj, tok: str) -> None:
    ep_url = get_url(tok)
    if not ep_url:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    if not ep["servers"]:
        await query_obj.answer("لا توجد سيرفرات مشاهدة متاحة لهذه الحلقة 😕", show_alert=True)
        return
    entries = await _watch_entries(ep)
    await query_obj.edit_message_text(
        f"🎬 سيرفرات المشاهدة — {ep['anime_title']} الحلقة {ep['number']}\n"
        "اختر سيرفرًا (يفتح صفحة المشاهدة):",
        reply_markup=servers_keyboard(entries, tok),
    )


async def show_downloads(query_obj, tok: str) -> None:
    ep_url = get_url(tok)
    if not ep_url:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    if not ep["downloads"]:
        await query_obj.answer("لا توجد روابط تحميل متاحة لهذه الحلقة 😕", show_alert=True)
        return
    await query_obj.edit_message_text(
        f"⬇️ روابط التحميل — {ep['anime_title']} الحلقة {ep['number']}:",
        reply_markup=downloads_keyboard(ep, tok),
    )


async def show_panel(query_obj, tok: str) -> None:
    ep_url = get_url(tok)
    if not ep_url:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    await _ensure_anime_cached(ep)
    await query_obj.edit_message_text(
        episode_panel_text(ep), reply_markup=episode_panel_keyboard(ep)
    )


async def show_video_qualities(query_obj, tok: str) -> None:
    """'📤 إرسال الفيديو هنا' -> quality menu (Google Drive HD/FHD + ok.ru)."""
    log.info("vid flow: pressed (tok=%s)", tok)
    ep_url = get_url(tok)
    if not ep_url:
        log.warning("vid flow: expired token %s", tok)
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    log.info("vid flow: episode fetched: %s ep %s (%d servers)",
             ep["anime_title"], ep["number"], len(ep["servers"]))
    try:
        options = await _video_options(ep)
        log.info("vid flow: %d send options", len(options))
        if not options:
            await query_obj.answer(
                "لا توجد مصادر إرسال مباشر متاحة لهذه الحلقة 😕 استخدم المشاهدة أو التحميل",
                show_alert=True,
            )
            return
        limit = "2 جيجا" if LOCAL_BOT_API else "45 ميجا"
        try:
            await query_obj.edit_message_text(
                f"📤 إرسال الفيديو — {ep['anime_title']} الحلقة {ep['number']}\n"
                f"اختر الجودة (الحد الأقصى للإرسال: {limit}):",
                reply_markup=video_qualities_keyboard(options, tok),
            )
        except Exception:
            # message too old to edit (>48h) or already replaced -> send a new one
            log.info("vid flow: edit failed, sending fresh message instead")
            await query_obj.message.reply_text(
                f"📤 إرسال الفيديو — {ep['anime_title']} الحلقة {ep['number']}\n"
                f"اختر الجودة (الحد الأقصى للإرسال: {limit}):",
                reply_markup=video_qualities_keyboard(options, tok),
            )
    except Exception:
        log.exception("video qualities menu failed: %s", ep_url)
        with contextlib.suppress(Exception):
            await query_obj.answer("حدث خطأ أثناء تجهيز الجودات ⚠️ حاول مرة أخرى", show_alert=True)
        with contextlib.suppress(Exception):
            await query_obj.message.reply_text("حدث خطأ أثناء تجهيز الجودات ⚠️ حاول مرة أخرى")


async def start_video_send(query_obj, qtok: str) -> None:
    """Quality button pressed -> resolve + download + send (best-effort)."""
    payload = get_url(qtok)
    if not payload:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    try:
        tok, kind, source_url = payload.split("|", 2)
    except ValueError:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    ep_url = get_url(tok)
    if not ep_url:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    progress = await query_obj.message.reply_text(MSG_PREPARING)
    chat_id = query_obj.message.chat_id
    try:
        # Bounded concurrency: waiters just queue here while the "جاري
        # تجهيز الفيديو..." progress message is already shown.
        async with VIDEO_SEND_SEM:
            await asyncio.wait_for(
                _send_video_inner(query_obj, progress, chat_id, ep_url, kind, source_url),
                timeout=VIDEO_TOTAL_TIMEOUT,
            )
    except asyncio.TimeoutError:
        log.warning("video flow timed out for %s", ep_url)
        await query_obj.message.reply_text(
            "استغرق تجهيز الفيديو وقتًا طويلاً ⏳ حاول مرة أخرى أو استخدم المشاهدة/التحميل"
        )
    except Exception:
        log.exception("video flow failed for %s", ep_url)
        await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
    finally:
        with contextlib.suppress(Exception):
            await progress.delete()


def _download_to_temp(mp4_url: str, path: str, state: dict) -> int:
    """Stream mp4_url into `path`. Returns bytes written; raises TooBigError
    (carrying the known size) when the file exceeds MAX_VIDEO_BYTES.
    `state["done"]`/`state["total"]` are updated for progress reporting."""
    with requests.get(
        mp4_url,
        stream=True,
        timeout=60,
        headers={"User-Agent": resolvers.MOBILE_UA},
    ) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        state["total"] = total
        if total > MAX_VIDEO_BYTES:
            raise TooBigError(total)
        written = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
                written += len(chunk)
                state["done"] = written
                if written > MAX_VIDEO_BYTES:
                    raise TooBigError(written)
    return written


class TooBigError(Exception):
    def __init__(self, size: int):
        super().__init__(f"video too big: {size} bytes")
        self.size = size


async def _progress_updater(progress, state: dict) -> None:
    """Edit the progress message every ~5s with the downloaded MB."""
    while True:
        await asyncio.sleep(5)
        done = state["done"]
        path = state.get("path")
        if path and os.path.isfile(path):
            done = os.path.getsize(path)
        mb = done / (1024 * 1024)
        total = state.get("total") or 0
        text = f"{MSG_PREPARING}\n⬇️ تم تنزيل {mb:.0f} ميجا"
        if total:
            text += f" من {total / (1024 * 1024):.0f}"
        with contextlib.suppress(Exception):
            await progress.edit_text(text)


async def _send_video_inner(query_obj, progress, chat_id: int, ep_url: str,
                            kind: str, source_url: str) -> None:
    ep = await fetch_episode(ep_url)

    tmp = tempfile.NamedTemporaryFile(prefix="witanime-", suffix=".mp4", delete=False)
    tmp_path = tmp.name
    tmp.close()
    state = {"done": 0, "total": 0, "path": None}

    mega_url = None
    if kind == "mega":
        mega_url = resolvers.normalize_mega_url(source_url) or source_url
        size = await asyncio.to_thread(resolvers.get_mega_size, mega_url)
        if size:
            state["total"] = size
            if size > MAX_VIDEO_BYTES:
                os.unlink(tmp_path)
                await _reply_too_big(query_obj, ep, source_url, size)
                return
        state["path"] = tmp_path  # progress tracked by polling file size
        log.info("mega source ready (%s MB known)", (size or 0) // (1024 * 1024))
        mp4_url = None
    elif kind == "drive":
        mp4_url = await asyncio.to_thread(resolvers.resolve_drive_mp4, source_url)
    else:  # okru
        mp4_url = await asyncio.to_thread(resolvers.resolve_mp4, source_url)
    if kind != "mega" and not mp4_url:
        os.unlink(tmp_path)
        await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
        return

    updater = asyncio.create_task(_progress_updater(progress, state))
    try:
        try:
            if kind == "mega":
                await asyncio.to_thread(resolvers.download_mega, mega_url, tmp_path, state)
                if state["done"] > MAX_VIDEO_BYTES:
                    raise TooBigError(state["done"])
            else:
                await asyncio.to_thread(_download_to_temp, mp4_url, tmp_path, state)
        except TooBigError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            await _reply_too_big(query_obj, ep, source_url, exc.size)
            return
        except Exception:
            log.exception("download failed (kind=%s)", kind)
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
            return
    finally:
        updater.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await updater

    with contextlib.suppress(Exception):
        await progress.edit_text(MSG_SENDING)
    try:
        caption = f"🎬 {ep['anime_title']} — الحلقة {ep['number']}"
        with open(tmp_path, "rb") as video_file:
            await query_obj.bot.send_video(
                chat_id=chat_id,
                video=video_file,
                filename=f"episode-{ep['number']}.mp4",
                caption=caption,
                supports_streaming=True,
                read_timeout=VIDEO_TOTAL_TIMEOUT,
                write_timeout=VIDEO_TOTAL_TIMEOUT,
                connect_timeout=30,
            )
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)


async def _reply_too_big(query_obj, ep: dict, embed_url: str, size: int) -> None:
    mb = size / (1024 * 1024) if size else 0
    rows = []
    watch = make_watch_url(BASE_PUBLIC_URL, embed_url, WATCH_SECRET)
    rows.append([InlineKeyboardButton("🎬 مشاهدة مباشرة", url=watch)])
    dl_rows = downloads_keyboard(ep, None).inline_keyboard
    rows.extend(dl_rows)
    text = (
        f"حجم الفيديو كبير ({mb:.0f} ميجا) ولا يمكن إرساله هنا 😅\n"
        "يمكنك المشاهدة مباشرة أو التحميل من الروابط:"
    )
    if not LOCAL_BOT_API:
        text += (
            "\n\n💡 ملاحظة: حد تليجرام للبوتات 50 ميجا. يمكن رفعه إلى 2 جيجا "
            "بتفعيل TG_API_ID و TG_API_HASH (سيرفر Bot API محلي)."
        )
    await query_obj.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    parts = data.split("|")
    kind = parts[0]

    try:
        if kind == "noop":
            await q.answer()
        elif kind == "s" and len(parts) == 3:  # search pagination
            entry = SEARCH_CACHE.get(parts[1])
            if not entry:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            _, results = entry
            page = max(0, int(parts[2]))
            await q.answer()
            await q.edit_message_reply_markup(
                reply_markup=search_keyboard(parts[1], results, page)
            )
        elif kind in ("anime", "a"):  # open anime
            anime_url = resolve_anime_ref(kind, parts[1])
            if not anime_url:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await q.answer()
            await open_anime(q, anime_url)
        elif kind in ("eps", "es") and len(parts) == 3:  # episode list page
            anime_url = resolve_anime_ref(kind, parts[1])
            if not anime_url:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await q.answer()
            await show_episodes_page(q, anime_url, int(parts[2]))
        elif kind == "ep":  # open episode panel
            ep_url = get_url(parts[1])
            if not ep_url:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await q.answer()
            await open_episode(q, ep_url)
        elif kind == "panel":
            await q.answer()
            await show_panel(q, parts[1])
        elif kind == "srv":
            await q.answer()
            await show_servers(q, parts[1])
        elif kind == "dl":
            await q.answer()
            await show_downloads(q, parts[1])
        elif kind == "vid":
            with contextlib.suppress(Exception):
                await q.answer()
            await show_video_qualities(q, parts[1])
        elif kind == "vq":  # video quality chosen -> start the send flow
            await q.answer()
            await start_video_send(q, parts[1])
        else:
            log.warning("unknown callback data: %r", data)
            with contextlib.suppress(Exception):
                await q.answer("الزر ده من رسالة قديمة — ابحث من جديد 🔍", show_alert=True)
    except Exception:
        log.exception("callback handler failed: %s", data)
        # never leave the user with a dead button — always give feedback
        with contextlib.suppress(Exception):
            await q.answer("حدث خطأ مؤقت ⚠️ حاول مرة أخرى", show_alert=True)
        with contextlib.suppress(Exception):
            await q.message.reply_text("حدث خطأ مؤقت ⚠️ حاول مرة أخرى")
        with contextlib.suppress(Exception):
            await q.message.reply_text(ERR_FETCH)


# ---------------------------------------------------------------------------
# Web server lifecycle (uvicorn inside the PTB asyncio loop, SPEC §7)
# ---------------------------------------------------------------------------
class QuietServer(uvicorn.Server):
    """uvicorn server that does not hijack process signal handlers
    (PTB manages graceful shutdown)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # older uvicorn versions
        pass


async def post_init(application: Application) -> None:
    config = uvicorn.Config(
        build_app(WATCH_SECRET), host="0.0.0.0", port=PORT, log_level="warning"
    )
    server = QuietServer(config)
    application.bot_data["uvicorn_server"] = server
    application.bot_data["uvicorn_task"] = asyncio.create_task(server.serve())
    log.info("watch server listening on 0.0.0.0:%d", PORT)


async def post_shutdown(application: Application) -> None:
    server = application.bot_data.get("uvicorn_server")
    task = application.bot_data.get("uvicorn_task")
    if server:
        server.should_exit = True
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN is required (set it in the environment / .env)")
    if not BASE_PUBLIC_URL:
        sys.exit("BASE_PUBLIC_URL is required (your public Railway domain)")

    builder = Application.builder().token(BOT_TOKEN)
    use_local_api = False
    if LOCAL_BOT_API:
        # Talk to the local telegram-bot-api started by the Docker CMD — but only
        # if it is actually up; otherwise fall back to the cloud API gracefully.
        log.info("waiting for local Bot API on %s ...", LOCAL_API_URL)
        use_local_api = _wait_for_local_api()
        if use_local_api:
            global MAX_VIDEO_BYTES, VIDEO_TOTAL_TIMEOUT
            MAX_VIDEO_BYTES = 1990 * 1024 * 1024  # ~2GB (local Bot API limit)
            VIDEO_TOTAL_TIMEOUT = 3600  # seconds — 2GB downloads need time
            builder = builder.base_url(f"{LOCAL_API_URL}/bot").base_file_url(
                f"{LOCAL_API_URL}/file/bot"
            )
            log.info("using local Bot API at %s (2GB send limit)", LOCAL_API_URL)
        else:
            log.warning(
                "local Bot API unreachable on %s — falling back to cloud Bot API "
                "(50MB send limit). Check TG_API_ID/TG_API_HASH and Dockerfile.",
                LOCAL_API_URL,
            )
    app = builder.build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.post_init = post_init
    app.post_shutdown = post_shutdown

    log.info("starting search-bot (polling + watch server on port %d)", PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
