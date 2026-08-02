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
import season
import store
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

# Community / batch features (SPEC: new env vars)
def _parse_admin_ids(raw: str) -> set[int]:
    ids = set()
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


ADMIN_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""))
FORCE_SUB_CHANNEL = os.environ.get("FORCE_SUB_CHANNEL", "").strip()
FOLLOW_CHECK_HOURS = max(1, int(os.environ.get("FOLLOW_CHECK_HOURS", "6") or 6))
BATCH_MAX_EPS = int(os.environ.get("BATCH_MAX_EPS", "24") or 24)
BATCH_GLOBAL_MAX = int(os.environ.get("BATCH_GLOBAL_MAX", "2") or 2)
# Per-episode watchdog for season batches: hard cap per episode and max
# seconds with zero download progress before the episode is skipped (Mega's
# CDN throttles stalled transfers without ever raising an error).
BATCH_EP_TIMEOUT_MIN = float(os.environ.get("BATCH_EP_TIMEOUT_MIN", "20") or 20)
BATCH_EP_STALL_SECS = float(os.environ.get("BATCH_EP_STALL_SECS", "120") or 120)

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


# --- batch / community feature state ------------------------------------------
ACTIVE_JOBS: dict[int, season.SeasonJob] = {}   # one active batch per user
BATCH_TASKS: set[asyncio.Task] = set()          # running _batch_runner tasks
BATCH_GLOBAL_SEM = asyncio.Semaphore(BATCH_GLOBAL_MAX)
# token -> {"anime_url", "title", "eps", "counts", "truncated", "total"}
SEASON_SCAN_CACHE: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
PENDING_RANGE: dict[int, tuple] = {}  # user_id -> (anime_url, first_url, first_num)
# token -> {"first_url", "first_num", "anime_url"} for range pickers
RANGE_TOKENS: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
LATEST_CACHE: dict = {"ts": 0.0, "items": []}   # 10-min cache for latest episodes
LATEST_TTL = 600
LATEST_PER_PAGE = 10
RANGE_PER_PAGE = 20


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


def _an_ref(prefix: str, anime_url: str) -> str:
    """callback_data '<prefix>|<slug-or-token>' (slug preferred, token fallback)."""
    slug = slug_from_url(anime_url)
    if slug and fits(f"{prefix}|{slug}"):
        return f"{prefix}|{slug}"
    return f"{prefix}|{put_url(anime_url)}"


def _resolve_an_ref(ref: str) -> str:
    """Resolve a _an_ref reference back to a full anime URL."""
    if "/anime/" in ref:
        return ref  # already a full URL (token fallback was used for put_url)
    resolved = get_url(ref)
    if resolved:
        return resolved
    return anime_url_from_slug(ref)


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
        rows.append(
            [
                InlineKeyboardButton("📦 الموسم كامل", callback_data=_an_ref("sdl", anime_url)),
                InlineKeyboardButton("🎯 مدى حلقات", callback_data=_an_ref("sdr", anime_url)),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("⭐ مفضلة", callback_data=_an_ref("fav", anime_url)),
            InlineKeyboardButton("🔔 متابعة", callback_data=_an_ref("fol", anime_url)),
        ]
    )
    if not rows:  # Telegram rejects empty keyboards with HTTP 400
        rows.append([InlineKeyboardButton("📭 لا توجد حلقات بعد", callback_data="noop")])
    return InlineKeyboardMarkup(rows)


def latest_keyboard(items: list[dict], page: int) -> InlineKeyboardMarkup:
    """Grid of the latest released episodes (each row opens the episode)."""
    start = page * LATEST_PER_PAGE
    chunk = items[start : start + LATEST_PER_PAGE]
    rows = []
    for it in chunk:
        label = f"{it['anime_title']} — {it['ep_title']}"
        if len(label) > 40:
            label = label[:37] + "..."
        rows.append(
            [InlineKeyboardButton(label, callback_data=f"ep|{put_url(it['ep_url'])}")]
        )
    total_pages = max(1, (len(items) + LATEST_PER_PAGE - 1) // LATEST_PER_PAGE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ السابق", callback_data=f"late|{page - 1}"))
    nav.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("التالي ▶️", callback_data=f"late|{page + 1}"))
    if len(nav) > 1:
        rows.append(nav)
    return InlineKeyboardMarkup(rows)


def anime_list_keyboard(entries: list[dict]) -> InlineKeyboardMarkup:
    """Grid of anime buttons (favorites / follows lists)."""
    rows = []
    for e in entries:
        title = e.get("title") or "بدون اسم"
        if len(title) > 45:
            title = title[:42] + "..."
        rows.append([InlineKeyboardButton(f"🎬 {title}", callback_data=anime_cb(e["url"]))])
    if not rows:  # never hand Telegram an empty keyboard
        rows.append([InlineKeyboardButton("🔍 ابحث عن أنمي", callback_data="noop")])
    return InlineKeyboardMarkup(rows)


def _ep_sort_key(ep: dict) -> float:
    try:
        return float(ep.get("number") or 0)
    except (TypeError, ValueError):
        return 0.0


def range_grid_keyboard(
    episodes: list[dict], anime_url: str, step: int, page: int, first_tok: str | None = None
) -> InlineKeyboardMarkup:
    """Episode-number grid for the range picker (20/page, 5 per row).

    step: 1 = pick first episode (cb 'sdr1'), 2 = pick last episode (cb 'sdr2').
    first_tok: RANGE_TOKENS token of the first pick (only for step 2)."""
    ordered = sorted(episodes, key=_ep_sort_key)
    total_pages = max(1, (len(ordered) + RANGE_PER_PAGE - 1) // RANGE_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    chunk = ordered[page * RANGE_PER_PAGE : (page + 1) * RANGE_PER_PAGE]
    prefix = "sdr1" if step == 1 else "sdr2"
    rows = []
    row = []
    for ep in chunk:
        label = f"{ep['number']}" if ep.get("type") == "الحلقة" else f"{ep['number']} ⭐"
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}|{put_url(ep['url'])}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    nav = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ السابق", callback_data=_an_ref(f"{prefix}p|{page - 1}", anime_url)
            )
        )
    nav.append(InlineKeyboardButton(f"صفحة {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(
            InlineKeyboardButton(
                "التالي ▶️", callback_data=_an_ref(f"{prefix}p|{page + 1}", anime_url)
            )
        )
    if len(nav) > 1:
        rows.append(nav)
    if first_tok:
        rows.append([InlineKeyboardButton("🔙 إعادة اختيار أول حلقة", callback_data=f"sdrre|{first_tok}")])
    return InlineKeyboardMarkup(rows)


def season_quality_keyboard(scan: dict, tok: str) -> InlineKeyboardMarkup:
    """Quality menu for a season batch — only qualities with counts > 0."""
    n = len(scan["eps"])
    rows = []
    for q in season.QUALITIES:
        count = scan["counts"].get(q, 0)
        if count > 0:
            rows.append(
                [InlineKeyboardButton(f"{q} (متاحة في {count}/{n})", callback_data=f"sdq|{tok}|{q}")]
            )
    if not rows:
        rows.append([InlineKeyboardButton("📭 لا توجد مصادر متاحة", callback_data="noop")])
    return InlineKeyboardMarkup(rows)


def season_confirm_keyboard(tok: str, quality: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ ابدأ التحميل", callback_data=f"sgo|{tok}|{quality}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"scl|{tok}")],
        ]
    )


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
WELCOME_TEXT = (
    "أهلاً بك في بوت ويت أنمي 🎌\n"
    "ابعت اسم الأنمي للبحث، واختر من النتائج لتصفح الحلقات والمشاهدة والتحميل 🍿\n\n"
    "🆕 مميزات جديدة:\n"
    "• 📦 تحميل موسم كامل (أو مدى حلقات) كفيديوهات هنا\n"
    "• 🔔 متابعة أنمي — توصلك الحلقات الجديدة فور نزولها\n"
    "• ⭐ مفضلة تحفظ فيها أنمياتك\n"
    "• 🆕 آخر الحلقات المنزلة على الموقع"
)

BANNED_TEXT = "🚫 تم حظرك من استخدام البوت."


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🆕 آخر الحلقات", callback_data="late|0")],
            [
                InlineKeyboardButton("⭐ مفضلتي", callback_data="favlist"),
                InlineKeyboardButton("🔔 متابعتي", callback_data="follist"),
            ],
        ]
    )


def force_sub_keyboard() -> InlineKeyboardMarkup:
    """Join-channel button + re-check button (only used when FORCE_SUB_CHANNEL
    is a public @channel)."""
    channel = FORCE_SUB_CHANNEL.lstrip("@")
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 انضم للقناة", url=f"https://t.me/{channel}")],
            [InlineKeyboardButton("✅ تحققت", callback_data="fsub|check")],
        ]
    )


async def check_force_sub(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """True when the user may proceed (no channel configured, is a member, or
    fail-open because the bot can't check)."""
    if not FORCE_SUB_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(FORCE_SUB_CHANNEL, user_id)
    except Exception as exc:
        msg = str(exc).lower()
        if "bot is not a member" in msg or "chat not found" in msg:
            # misconfiguration on our side — never lock users out for it
            log.error("force-sub check impossible (bot not admin in %s?): %s",
                      FORCE_SUB_CHANNEL, exc)
            return True
        log.warning("force-sub check failed for %s: %s — treating as not member",
                    user_id, exc)
        return False
    return member.status not in ("left", "kicked")


def _touch(user) -> None:
    with contextlib.suppress(Exception):
        store.touch_user(user.id, name=user.full_name or "", username=user.username or "")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _touch(user)
    if store.is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    if not await check_force_sub(context, user.id):
        await update.message.reply_text(
            "🔒 لازم تشترك في قناتنا الأول عشان تستخدم البوت.\n"
            "اشترك من الزر بالأسفل ثم اضغط «✅ تحققت».",
            reply_markup=force_sub_keyboard(),
        )
        return
    await update.message.reply_text(WELCOME_TEXT, reply_markup=start_keyboard())


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = (update.message.text or "").strip()
    if not query:
        return
    user = update.effective_user
    _touch(user)
    if store.is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    if not await check_force_sub(context, user.id):
        await update.message.reply_text(
            "🔒 لازم تشترك في قناتنا الأول عشان تستخدم البوت.\n"
            "اشترك من الزر بالأسفل ثم اضغط «✅ تحققت».",
            reply_markup=force_sub_keyboard(),
        )
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
        # edit can fail (old message, API hiccup) — fall back to a fresh
        # message so pagination never feels dead
        log.info("episodes edit failed (%s) — sending fresh page", exc)
        kb = episodes_keyboard(anime_url, info, page)
        total_pages = max(1, (len(info["episodes"]) + EPS_PER_PAGE - 1) // EPS_PER_PAGE)
        await query_obj.message.reply_text(
            f"📺 {info['title']}\n📑 صفحة {page + 1}/{total_pages} — اختر الحلقة:",
            reply_markup=kb,
        )


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
        # reflect the REAL active limit (local API may have failed to start)
        limit = "2 جيجا" if MAX_VIDEO_BYTES > 100 * 1024 * 1024 else "45 ميجا"
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


def _download_to_temp(mp4_url: str, path: str, state: dict, cancel=None) -> int:
    """Stream mp4_url into `path`. Returns bytes written; raises TooBigError
    (carrying the known size) when the file exceeds MAX_VIDEO_BYTES.
    `state["done"]`/`state["total"]` are updated for progress reporting.
    Uses the shared keep-alive session with 4MB chunks; `cancel` (optional
    callable) aborts the transfer with resolvers.DownloadCancelled."""
    session = resolvers.get_session()
    with session.get(
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
            for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                if cancel and cancel():
                    raise resolvers.DownloadCancelled()
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


async def _upload_ticker(progress) -> None:
    """While send_video is uploading, edit the progress message every 10s."""
    start = time.monotonic()
    while True:
        await asyncio.sleep(10)
        elapsed = int(time.monotonic() - start)
        with contextlib.suppress(Exception):
            await progress.edit_text(f"📤 جاري الإرسال… (مرّت {elapsed} ث)")


async def _send_video_inner(query_obj, progress, chat_id: int, ep_url: str,
                            kind: str, source_url: str) -> None:
    ep = await fetch_episode(ep_url)

    tmp = tempfile.NamedTemporaryFile(prefix="witanime-", suffix=".mp4", delete=False)
    tmp_path = tmp.name
    tmp.close()
    state = {"done": 0, "total": 0, "path": None, "aborted": False}

    try:
        mega_url = None
        if kind == "mega":
            mega_url = resolvers.normalize_mega_url(source_url) or source_url
            size = await asyncio.to_thread(resolvers.get_mega_size, mega_url)
            if size:
                state["total"] = size
                if size > MAX_VIDEO_BYTES:
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
            await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
            return

        updater = asyncio.create_task(_progress_updater(progress, state))
        try:
            try:
                if kind == "mega":
                    await asyncio.to_thread(
                        resolvers.download_mega, mega_url, tmp_path, state,
                        cancel=lambda: state["aborted"],
                    )
                    if state["done"] > MAX_VIDEO_BYTES:
                        raise TooBigError(state["done"])
                else:
                    await asyncio.to_thread(
                        _download_to_temp, mp4_url, tmp_path, state,
                        cancel=lambda: state["aborted"],
                    )
            except TooBigError as exc:
                await _reply_too_big(query_obj, ep, source_url, exc.size)
                return
            except asyncio.TimeoutError:
                state["aborted"] = True  # stop the orphaned download thread
                raise
            except asyncio.CancelledError:
                state["aborted"] = True  # stop the orphaned download thread
                raise
            except resolvers.DownloadCancelled:
                # the worker thread saw state["aborted"] and stopped — nothing
                # left to report on these paths
                state["aborted"] = True
                log.info("download aborted (kind=%s)", kind)
                return
            except Exception:
                state["aborted"] = True  # stop the orphaned download thread
                log.exception("download failed (kind=%s)", kind)
                await query_obj.message.reply_text(MSG_RESOLVE_FAIL)
                return
        finally:
            updater.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await updater

        with contextlib.suppress(Exception):
            await progress.edit_text(MSG_SENDING)
        ticker = asyncio.create_task(_upload_ticker(progress))
        try:
            caption = f"🎬 {ep['anime_title']} — الحلقة {ep['number']}"
            with open(tmp_path, "rb") as video_file:
                await query_obj.get_bot().send_video(
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
            ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticker
    finally:
        # covers every stage above (resolve, download, send, cancel/timeout)
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
    if MAX_VIDEO_BYTES < 100 * 1024 * 1024:
        text += (
            "\n\n💡 ملاحظة: سيرفر البوت API المحلي مش شغال حالياً — الحد 45 ميجا. "
            "بعد إصلاحه هيتم الإرسال حتى 2 جيجا تلقائياً."
        )
    await query_obj.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))


# ---------------------------------------------------------------------------
# Season batch flows (full season + episode range)
# ---------------------------------------------------------------------------
def _season_too_big_reply(chat_id: int, bot):
    """Build a too_big_reply callable for SeasonJob: sends the episode's
    direct-download links (like the single-video _reply_too_big)."""

    async def _reply(ep: dict, size: int) -> None:
        mb = size / (1024 * 1024) if size else 0
        rows = downloads_keyboard(ep, None).inline_keyboard
        text = (
            f"⚠️ الحلقة {ep.get('number', '')} حجمها كبير ({mb:.0f} ميجا) "
            "ومش هتنفع تتبعت كفيديو.\nحمّلها من الروابط دي:"
        )
        with contextlib.suppress(Exception):
            await bot.send_message(
                chat_id=chat_id, text=text, reply_markup=InlineKeyboardMarkup(list(rows))
            )

    return _reply


async def _run_scan_and_show_qualities(query_obj, anime_url: str, title: str,
                                       episodes: list[dict]) -> None:
    """Shared scan → quality-menu path for both season flows."""
    scan = await season.scan_season(
        episodes, BATCH_MAX_EPS, fetch_episode=fetch_episode
    )
    if not any(scan["counts"].values()):
        await query_obj.message.reply_text(
            "😕 مفيش مصادر تحميل (Google Drive / Mega) متاحة للحلقات دي حاليًا."
        )
        return
    tok = uuid.uuid4().hex[:8]
    _bounded_put(
        SEASON_SCAN_CACHE,
        tok,
        {
            "anime_url": anime_url,
            "title": title,
            "eps": scan["eps"],
            "counts": scan["counts"],
            "truncated": scan["truncated"],
            "total": scan["total"],
        },
    )
    text = f"📦 {title}\nاختر الجودة المطلوبة:"
    if scan["truncated"]:
        text += (
            f"\n⚠️ سيتم تحميل أول {len(scan['eps'])} حلقة فقط من أصل {scan['total']}"
        )
    await query_obj.message.reply_text(
        text, reply_markup=season_quality_keyboard(scan, tok)
    )


async def season_full_start(query_obj, anime_url: str) -> None:
    """'📦 الموسم كامل' pressed."""
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    if not info["episodes"]:
        await query_obj.answer("لا توجد حلقات لهذا الأنمي بعد 😕", show_alert=True)
        return
    with contextlib.suppress(Exception):
        await query_obj.answer()
    await query_obj.message.reply_text("🔍 جاري فحص الجودات المتاحة للموسم…")
    try:
        await _run_scan_and_show_qualities(
            query_obj, anime_url, info["title"], info["episodes"]
        )
    except Exception:
        log.exception("season scan failed: %s", anime_url)
        await query_obj.message.reply_text(ERR_FETCH)


async def range_pick_start(query_obj, anime_url: str) -> None:
    """'🎯 مدى حلقات' pressed — show the 'pick first episode' grid."""
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    if not info["episodes"]:
        await query_obj.answer("لا توجد حلقات لهذا الأنمي بعد 😕", show_alert=True)
        return
    with contextlib.suppress(Exception):
        await query_obj.answer()
    await query_obj.message.reply_text(
        f"🎯 {info['title']}\nاختر أول حلقة في المدى:",
        reply_markup=range_grid_keyboard(info["episodes"], anime_url, step=1, page=0),
    )


async def range_pick_first(query_obj, user_id: int, ep_url: str) -> None:
    """First episode picked — remember it and show the 'pick last episode' grid."""
    try:
        ep = await fetch_episode(ep_url)
    except scraper.ScraperError:
        log.exception("episode fetch failed: %s", ep_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    anime_url = ep.get("anime_url") or ""
    if not anime_url:
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    PENDING_RANGE[user_id] = (anime_url, ep_url, _ep_sort_key(ep))
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    with contextlib.suppress(Exception):
        await query_obj.answer()
    first_tok = uuid.uuid4().hex[:8]
    _bounded_put(
        RANGE_TOKENS,
        first_tok,
        {"first_url": ep_url, "first_num": _ep_sort_key(ep), "anime_url": anime_url},
    )
    await query_obj.message.reply_text(
        f"🎯 اخترت الحلقة {ep['number']} كبداية.\nدلوقتي اختر آخر حلقة في المدى:",
        reply_markup=range_grid_keyboard(
            info["episodes"], anime_url, step=2, page=0, first_tok=first_tok
        ),
    )


async def range_pick_last(query_obj, user_id: int, ep_url: str) -> None:
    """Last episode picked — slice the range and go through scan→quality→confirm."""
    pending = PENDING_RANGE.pop(user_id, None)
    if not pending:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    anime_url, first_url, first_num = pending
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    ordered = sorted(info["episodes"], key=_ep_sort_key)
    urls = {e["url"]: i for i, e in enumerate(ordered)}
    if first_url not in urls or ep_url not in urls:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    lo, hi = sorted((urls[first_url], urls[ep_url]))
    selected = ordered[lo : hi + 1]
    with contextlib.suppress(Exception):
        await query_obj.answer()
    await query_obj.message.reply_text(
        f"🎯 تم اختيار {len(selected)} حلقة (من {ordered[lo]['number']} إلى {ordered[hi]['number']}).\n"
        "🔍 جاري فحص الجودات المتاحة…"
    )
    try:
        await _run_scan_and_show_qualities(query_obj, anime_url, info["title"], selected)
    except Exception:
        log.exception("range scan failed: %s", anime_url)
        await query_obj.message.reply_text(ERR_FETCH)


async def _season_confirm_screen(query_obj, scan: dict, tok: str, quality: str) -> None:
    n = len(scan["eps"])
    text = f"سيتم تحميل {n} حلقة بجودة {quality} كفيديوهات هنا. متابعة؟"
    if scan.get("truncated"):
        text += f"\n⚠️ سيتم تحميل أول {n} حلقة فقط من أصل {scan['total']}"
    await query_obj.edit_message_text(
        text, reply_markup=season_confirm_keyboard(tok, quality)
    )


async def _batch_runner(user_id: int, job: season.SeasonJob) -> None:
    """Run a SeasonJob under the global batch semaphore; always unregisters."""
    try:
        async with BATCH_GLOBAL_SEM:
            store.incr_batches(user_id)
            await job.run()
            sent = sum(1 for r in job.results if r[1] == "sent")
            if sent:
                store.incr_videos_sent(user_id, sent)
    except Exception:
        log.exception("batch job crashed for user %s", user_id)
    finally:
        if ACTIVE_JOBS.get(user_id) is job:
            ACTIVE_JOBS.pop(user_id, None)


async def season_go(query_obj, user_id: int, chat_id: int, tok: str, quality: str) -> None:
    """'✅ ابدأ التحميل' — create the SeasonJob and launch it as a task."""
    if user_id in ACTIVE_JOBS:
        await query_obj.answer(
            "عندك تحميل شغال بالفعل — ألغِه أولًا من زر ❌ في رسالة الحالة",
            show_alert=True,
        )
        return
    scan = SEASON_SCAN_CACHE.get(tok)
    if not scan:
        await query_obj.answer(ERR_EXPIRED, show_alert=True)
        return
    await query_obj.answer()
    job = season.SeasonJob(
        bot=query_obj.get_bot(),
        chat_id=chat_id,
        user_id=user_id,
        anime_title=scan["title"],
        episodes=scan["eps"],
        wanted_quality=quality,
        max_video_bytes=MAX_VIDEO_BYTES,
        fetch_episode=fetch_episode,
        too_big_reply=_season_too_big_reply(chat_id, query_obj.get_bot()),
        video_timeout=VIDEO_TOTAL_TIMEOUT,
        ep_timeout=BATCH_EP_TIMEOUT_MIN * 60,
        stall_secs=BATCH_EP_STALL_SECS,
    )
    ACTIVE_JOBS[user_id] = job
    task = asyncio.create_task(_batch_runner(user_id, job))
    BATCH_TASKS.add(task)
    task.add_done_callback(BATCH_TASKS.discard)
    with contextlib.suppress(Exception):
        await query_obj.edit_message_text(
            f"📦 {scan['title']}\n▶️ بدأ التحميل — هتلاقي رسالة الحالة بالأسفل "
            "وفيها زر الإلغاء."
        )


# ---------------------------------------------------------------------------
# Latest episodes / favorites / follows
# ---------------------------------------------------------------------------
async def get_latest_cached() -> list[dict]:
    if time.time() - LATEST_CACHE["ts"] < LATEST_TTL and LATEST_CACHE["items"]:
        return LATEST_CACHE["items"]
    items = await asyncio.to_thread(scraper.get_latest_episodes, 40)
    LATEST_CACHE["ts"] = time.time()
    LATEST_CACHE["items"] = items
    return items


async def show_latest(query_obj, page: int) -> None:
    try:
        items = await get_latest_cached()
    except scraper.ScraperError:
        log.exception("latest episodes fetch failed")
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    if not items:
        await query_obj.answer("مفيش حلقات جديدة حاليًا 😕", show_alert=True)
        return
    with contextlib.suppress(Exception):
        await query_obj.answer()
    total_pages = max(1, (len(items) + LATEST_PER_PAGE - 1) // LATEST_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    await query_obj.message.reply_text(
        "🆕 آخر الحلقات المنزلة على ويت أنمي:",
        reply_markup=latest_keyboard(items, page),
    )


async def show_favorites(target, user_id: int) -> None:
    """`target` is a message-like object (Message or CallbackQuery.message)."""
    favs = store.get_favorites(user_id)
    if not favs:
        await target.reply_text("مفيش مفضلة لسه — دوس ⭐ على أي أنمي")
        return
    await target.reply_text(
        "⭐ مفضلتك:", reply_markup=anime_list_keyboard(favs)
    )


async def show_follows(target, user_id: int) -> None:
    follows = store.get_follows(user_id)
    if not follows:
        await target.reply_text("مفيش متابعات لسه — دوس 🔔 على أي أنمي")
        return
    entries = [
        {"url": url, "title": f"{f.get('title', '')} ({f.get('ep_count', 0)} حلقة)"}
        for url, f in follows.items()
    ]
    await target.reply_text(
        "🔔 متابعاتك:", reply_markup=anime_list_keyboard(entries)
    )


async def toggle_favorite_cb(query_obj, user_id: int, anime_url: str) -> None:
    try:
        info = await fetch_anime_info(anime_url)
        title = info["title"]
    except Exception:
        title = anime_url.rstrip("/").split("/")[-1]
    added = store.toggle_favorite(user_id, anime_url, title)
    await query_obj.answer("⭐ اتضافت للمفضلة" if added else "اتشالت من المفضلة")


async def toggle_follow_cb(query_obj, user_id: int, anime_url: str) -> None:
    follows = store.get_follows(user_id)
    if anime_url in follows:
        store.remove_follow(user_id, anime_url)
        await query_obj.answer("🔕 اتوقفت المتابعة")
        return
    try:
        info = await fetch_anime_info(anime_url)
    except scraper.ScraperError:
        log.exception("anime info failed: %s", anime_url)
        await query_obj.answer(ERR_FETCH, show_alert=True)
        return
    last_ep = info["episodes"][-1]["url"] if info["episodes"] else ""
    store.set_follow(
        user_id, anime_url, info["title"], len(info["episodes"]), last_ep
    )
    await query_obj.answer("🔔 هتوصلك الحلقات الجديدة")


async def cmd_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _touch(user)
    if store.is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    await show_favorites(update.message, user.id)


async def cmd_follows(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    _touch(user)
    if store.is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    await show_follows(update.message, user.id)


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return  # non-admin: ignore silently
    _touch(user)
    s = store.get_stats()
    await update.message.reply_text(
        "📊 إحصائيات البوت:\n"
        f"👥 المستخدمون: {s['users']}\n"
        f"🚫 المحظورون: {s['banned']}\n"
        f"🔔 المتابعات: {s['follows']}\n"
        f"⭐ المفضلات: {s['favorites']}\n"
        f"📤 فيديوهات أُرسلت: {s['videos_sent']}\n"
        f"📦 دفعات مواسم: {s['batches']}"
    )


async def _ban_cmd(update: Update, banned: bool) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    args = (update.message.text or "").split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit():
        await update.message.reply_text("⚠️ الاستخدام: /ban <id> أو /unban <id>")
        return
    target = int(args[1])
    store.set_banned(target, banned)
    await update.message.reply_text(
        f"🚫 تم حظر المستخدم {target}" if banned else f"✅ تم فك حظر المستخدم {target}"
    )


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_cmd(update, True)


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ban_cmd(update, False)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return
    _touch(user)
    text = (update.message.text or "").partition(" ")[2].strip()
    source_msg = update.message
    if not text and source_msg.reply_to_message:
        source_msg = source_msg.reply_to_message
    elif not text:
        await update.message.reply_text(
            "⚠️ الاستخدام: /broadcast <نص> أو رد على رسالة بـ /broadcast"
        )
        return
    user_ids = store.all_user_ids()
    ok = 0
    for uid in user_ids:
        try:
            if text:
                await context.bot.send_message(chat_id=uid, text=text)
            else:
                await context.bot.copy_message(
                    chat_id=uid,
                    from_chat_id=source_msg.chat_id,
                    message_id=source_msg.message_id,
                )
            ok += 1
        except Exception as exc:
            log.info("broadcast to %s failed: %s", uid, exc)
        await asyncio.sleep(0.05)
    await update.message.reply_text(f"📢 تم الإرسال لـ {ok} من {len(user_ids)}")


# ---------------------------------------------------------------------------
# Follow checker (new-episode notifications)
# ---------------------------------------------------------------------------
async def _follow_check_pass(bot) -> None:
    """One pass over all followed animes; notify followers of new episodes."""
    index = store.follows_index()
    for anime_url, user_ids in index.items():
        try:
            info = await asyncio.to_thread(scraper.get_anime_info, anime_url)
        except Exception as exc:
            log.warning("follow check: fetch failed for %s: %s", anime_url, exc)
            await asyncio.sleep(5)
            continue
        _bounded_put(ANIME_CACHE, anime_url, (time.monotonic(), info))
        eps = info["episodes"]
        last_ep = eps[-1]["url"] if eps else ""
        for uid in user_ids:
            try:
                follow = store.get_follows(uid).get(anime_url)
                if not follow:
                    continue
                if len(eps) > int(follow.get("ep_count") or 0):
                    await bot.send_message(
                        chat_id=uid,
                        text=(
                            f"🚨 نزلت حلقة جديدة من {info['title']}!\n"
                            f"📺 أصبح عدد الحلقات: {len(eps)}"
                        ),
                        reply_markup=InlineKeyboardMarkup(
                            [[InlineKeyboardButton("🎬 فتح الأنمي",
                                                    callback_data=anime_cb(anime_url))]]
                        ),
                    )
                    store.update_follow(uid, anime_url, len(eps), last_ep)
            except Exception as exc:
                log.info("follow notify %s failed: %s", uid, exc)
            await asyncio.sleep(1)
        await asyncio.sleep(5)


async def _follow_check_loop(bot) -> None:
    while True:
        try:
            await _follow_check_pass(bot)
        except Exception:
            log.exception("follow checker pass failed")
        await asyncio.sleep(FOLLOW_CHECK_HOURS * 3600)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    data = q.data or ""
    parts = data.split("|")
    kind = parts[0]
    user = update.effective_user
    _touch(user)
    if store.is_banned(user.id):
        with contextlib.suppress(Exception):
            await q.answer(BANNED_TEXT, show_alert=True)
        return

    try:
        if kind == "fsub" and len(parts) == 2 and parts[1] == "check":
            if await check_force_sub(context, user.id):
                await q.answer("✅ تم التحقق — أهلًا بيك!")
                with contextlib.suppress(Exception):
                    await q.message.delete()
                await q.message.reply_text(WELCOME_TEXT, reply_markup=start_keyboard())
            else:
                await q.answer("لسه مشتركتش في القناة — اشترك الأول ثم اضغط «✅ تحققت»",
                               show_alert=True)
        elif not await check_force_sub(context, user.id):
            await q.answer(
                "🔒 لازم تشترك في قناتنا الأول عشان تستخدم البوت.", show_alert=True
            )
            with contextlib.suppress(Exception):
                await q.message.reply_text(
                    "اشترك من الزر بالأسفل ثم اضغط «✅ تحققت».",
                    reply_markup=force_sub_keyboard(),
                )
        elif kind == "noop":
            await q.answer()
        elif kind == "s" and len(parts) == 3:  # search pagination
            entry = SEARCH_CACHE.get(parts[1])
            if not entry:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            _, results = entry
            page = max(0, int(parts[2]))
            await q.answer()
            try:
                await q.edit_message_reply_markup(
                    reply_markup=search_keyboard(parts[1], results, page)
                )
            except Exception as exc:
                log.info("search page edit failed (%s) — sending fresh page", exc)
                await q.message.reply_text(
                    "📑 صفحة جديدة من النتائج:",
                    reply_markup=search_keyboard(parts[1], results, page),
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
        elif kind == "sdl":  # full season download
            await season_full_start(q, _resolve_an_ref(parts[1]))
        elif kind == "sdr":  # episode range: pick first episode
            await range_pick_start(q, _resolve_an_ref(parts[1]))
        elif kind == "sdr1" and len(parts) == 2:  # first episode picked
            ep_url = get_url(parts[1])
            if not ep_url:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await range_pick_first(q, user.id, ep_url)
        elif kind == "sdr2" and len(parts) == 2:  # last episode picked
            ep_url = get_url(parts[1])
            if not ep_url:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await range_pick_last(q, user.id, ep_url)
        elif kind in ("sdr1p", "sdr2p") and len(parts) == 3:  # range grid paging
            anime_url = _resolve_an_ref(parts[2])
            try:
                info = await fetch_anime_info(anime_url)
            except scraper.ScraperError:
                await q.answer(ERR_FETCH, show_alert=True)
                return
            step = 1 if kind == "sdr1p" else 2
            first_tok = None
            if step == 2:
                pending = PENDING_RANGE.get(user.id)
                if pending:
                    first_tok = uuid.uuid4().hex[:8]
                    _bounded_put(
                        RANGE_TOKENS,
                        first_tok,
                        {"first_url": pending[1], "first_num": pending[2],
                         "anime_url": pending[0]},
                    )
            with contextlib.suppress(Exception):
                await q.answer()
            page = max(0, int(parts[1]))
            try:
                await q.edit_message_reply_markup(
                    reply_markup=range_grid_keyboard(
                        info["episodes"], anime_url, step, page, first_tok
                    )
                )
            except Exception as exc:
                log.info("range grid edit failed (%s) — sending fresh grid", exc)
                await q.message.reply_text(
                    "اختر أول حلقة:" if step == 1 else "اختر آخر حلقة:",
                    reply_markup=range_grid_keyboard(
                        info["episodes"], anime_url, step, page, first_tok
                    ),
                )
        elif kind == "sdrre" and len(parts) == 2:  # back to first-episode grid
            rt = RANGE_TOKENS.get(parts[1])
            if not rt:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            PENDING_RANGE.pop(user.id, None)
            try:
                info = await fetch_anime_info(rt["anime_url"])
            except scraper.ScraperError:
                await q.answer(ERR_FETCH, show_alert=True)
                return
            with contextlib.suppress(Exception):
                await q.answer()
            await q.edit_message_text(
                "🎯 اختر أول حلقة في المدى:",
                reply_markup=range_grid_keyboard(
                    info["episodes"], rt["anime_url"], step=1, page=0
                ),
            )
        elif kind == "sdq" and len(parts) == 3:  # batch quality chosen -> confirm
            if parts[2] not in season.QUALITIES:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            scan = SEASON_SCAN_CACHE.get(parts[1])
            if not scan:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await q.answer()
            await _season_confirm_screen(q, scan, parts[1], parts[2])
        elif kind == "sgo" and len(parts) == 3:  # confirmed -> start the batch
            if parts[2] not in season.QUALITIES:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            await season_go(q, user.id, q.message.chat_id, parts[1], parts[2])
        elif kind == "scl" and len(parts) == 2:  # batch cancelled at confirm screen
            await q.answer("اتلغى ✅")
            with contextlib.suppress(Exception):
                await q.edit_message_text("❌ اتلغى تحميل الموسم.")
        elif kind == "sdc" and len(parts) == 2:  # cancel a running batch
            try:
                owner = int(parts[1])
            except ValueError:
                await q.answer(ERR_EXPIRED, show_alert=True)
                return
            if owner != user.id:
                await q.answer("مش أنت صاحب التحميل ده", show_alert=True)
                return
            job = ACTIVE_JOBS.get(user.id)
            if not job:
                await q.answer("مفيش تحميل شغال حاليًا", show_alert=True)
                return
            job.cancel()
            await q.answer("⛔ جاري الإلغاء…")
        elif kind == "late" and len(parts) == 2:  # latest episodes
            await show_latest(q, int(parts[1]))
        elif kind == "fav" and len(parts) == 2:  # favorite toggle
            await toggle_favorite_cb(q, user.id, _resolve_an_ref(parts[1]))
        elif kind == "favlist":
            await q.answer()
            await show_favorites(q.message, user.id)
        elif kind == "fol" and len(parts) == 2:  # follow toggle
            await toggle_follow_cb(q, user.id, _resolve_an_ref(parts[1]))
        elif kind == "follist":
            await q.answer()
            await show_follows(q.message, user.id)
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
    application.bot_data["follow_task"] = asyncio.create_task(
        _follow_check_loop(application.bot)
    )
    log.info("follow checker started (every %dh)", FOLLOW_CHECK_HOURS)


async def post_shutdown(application: Application) -> None:
    follow_task = application.bot_data.get("follow_task")
    if follow_task:
        follow_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await follow_task
    # stop running batch jobs gracefully, then cancel + reap their tasks so
    # nothing is left "pending" when the loop closes
    for job in list(ACTIVE_JOBS.values()):
        job.cancel()
    for batch_task in list(BATCH_TASKS):
        batch_task.cancel()
    if BATCH_TASKS:
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(
                asyncio.gather(*list(BATCH_TASKS), return_exceptions=True),
                timeout=5,
            )
    server = application.bot_data.get("uvicorn_server")
    task = application.bot_data.get("uvicorn_task")
    if server:
        server.should_exit = True
    if task:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()


def build_application() -> Application:
    """Build the PTB Application with all handlers registered (no polling)."""
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
    app.add_handler(CommandHandler("favorites", cmd_favorites))
    app.add_handler(CommandHandler("follows", cmd_follows))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app


def main() -> None:
    if not BOT_TOKEN:
        sys.exit("BOT_TOKEN is required (set it in the environment / .env)")
    if not BASE_PUBLIC_URL:
        sys.exit("BASE_PUBLIC_URL is required (your public Railway domain)")

    app = build_application()
    log.info("starting search-bot (polling + watch server on port %d)", PORT)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
