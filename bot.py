"""search-bot entry point: python bot.py

Runs PTB polling + the FastAPI watch server (uvicorn) in the same asyncio
loop (SPEC §6/§7). All bot UI texts are Arabic; code/comments in English.
"""

import asyncio
import collections
import contextlib
import io
import logging
import os
import re
import sys
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
MSG_RESOLVE_FAIL = "تعذّر الاستخراج المباشر — استخدم المشاهدة أو التحميل 🎬⬇️"

RESULTS_PER_PAGE = 10
EPS_PER_PAGE = 20
MAX_VIDEO_BYTES = 45 * 1024 * 1024  # 45MB safety margin (Bot API limit: 50MB)
VIDEO_TOTAL_TIMEOUT = 120  # seconds
# Limit concurrent in-memory video downloads (each may hold up to 45MB).
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


def servers_keyboard(ep: dict, tok: str) -> InlineKeyboardMarkup:
    rows = []
    for s in ep["servers"]:
        url = make_watch_url(BASE_PUBLIC_URL, s["embed_url"], WATCH_SECRET)
        rows.append([InlineKeyboardButton(f"▶️ {s['name']}", url=url)])
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
    await query_obj.edit_message_text(
        f"🎬 سيرفرات المشاهدة — {ep['anime_title']} الحلقة {ep['number']}\n"
        "اختر سيرفرًا (يفتح صفحة المشاهدة):",
        reply_markup=servers_keyboard(ep, tok),
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


async def send_video_flow(query_obj, tok: str) -> None:
    """Best-effort direct video sending (SPEC §8)."""
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
                _send_video_inner(query_obj, chat_id, ep_url), timeout=VIDEO_TOTAL_TIMEOUT
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


async def _send_video_inner(query_obj, chat_id: int, ep_url: str) -> None:
    ep = await fetch_episode(ep_url)

    mp4_url = None
    used_server = None
    for s in ep["servers"]:
        resolved = await asyncio.to_thread(resolvers.resolve_mp4, s["embed_url"])
        if resolved:
            mp4_url = resolved
            used_server = s
            break
    if not mp4_url:
        await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
        return

    # Probe the size (streamed GET, abort early if too large)
    def probe_and_download():
        with requests.get(
            mp4_url,
            stream=True,
            timeout=30,
            headers={"User-Agent": resolvers.MOBILE_UA},
        ) as r:
            r.raise_for_status()
            length = r.headers.get("Content-Length")
            if length and int(length) > MAX_VIDEO_BYTES:
                return None, int(length)  # too big -> don't download
            buf = io.BytesIO()
            for chunk in r.iter_content(chunk_size=512 * 1024):
                buf.write(chunk)
                if buf.tell() > MAX_VIDEO_BYTES:
                    return None, buf.tell()
            return buf.getvalue(), buf.tell()

    data, size = await asyncio.to_thread(probe_and_download)
    if data is None:
        mb = size / (1024 * 1024) if size else 0
        rows = []
        if used_server:
            watch = make_watch_url(BASE_PUBLIC_URL, used_server["embed_url"], WATCH_SECRET)
            rows.append([InlineKeyboardButton(f"🎬 مشاهدة ({used_server['name']})", url=watch)])
        dl_rows = downloads_keyboard(ep, None).inline_keyboard
        rows.extend(dl_rows)
        await query_obj.message.reply_text(
            f"حجم الفيديو كبير ({mb:.0f} ميجا) ولا يمكن إرساله هنا 😅\n"
            "يمكنك المشاهدة مباشرة أو التحميل من الروابط:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return

    caption = f"🎬 {ep['anime_title']} — الحلقة {ep['number']}"
    await query_obj.bot.send_video(
        chat_id=chat_id,
        video=data,
        filename=f"episode-{ep['number']}.mp4",
        caption=caption,
        supports_streaming=True,
        read_timeout=VIDEO_TOTAL_TIMEOUT,
        write_timeout=VIDEO_TOTAL_TIMEOUT,
        connect_timeout=30,
    )


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
            await q.answer()
            await send_video_flow(q, parts[1])
        else:
            await q.answer()
    except Exception:
        log.exception("callback handler failed: %s", data)
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

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.post_init = post_init
    app.post_shutdown = post_shutdown

    log.info("starting search-bot (polling + watch server on port %d)", PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
