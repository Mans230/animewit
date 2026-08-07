"""Batch season-download engine (SPEC.md §Module: season.py).

Sends a whole season (or a range of episodes) into Telegram one video after
another, using Google Drive / Mega sources (expanded from the yonaplay
players of each episode) plus Gofile sources from the episode download rows.
Supports instant cancel, per-episode TooBig fallback (direct-download links
instead), nearest-quality substitution and a
detailed final Arabic summary (always sent, even on cancel).

This module must NOT import bot.py — the job registry lives there.
"""

import asyncio
import collections
import contextlib
import logging
import os
import tempfile
import time

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import resolvers
import scraper

log = logging.getLogger(__name__)

# yonaplay players expose HD/FHD only — order = best → worst
QUALITIES = ["FHD", "HD"]
PREFER_HOST = {"drive": 0, "mega": 1, "gofile": 2}

# Map the episode page's Arabic download-row quality labels onto QUALITIES
# («الجودة المتوسطة SD» rows are ignored — batch sends HD/FHD only).
_DOWNLOAD_QUALITY_MAP = (("الخارقة", "FHD"), ("العالية", "HD"))

_CACHE_TTL = 6 * 3600          # 6h resolve cache
_CACHE_MAX = 500               # max cached episodes
_CACHE_MISS_TTL = 10 * 60      # empty results are re-tried sooner

# ep_url -> (timestamp, {quality: [src, ...]})
_SOURCES_CACHE: "collections.OrderedDict[str, tuple[float, dict]]" = (
    collections.OrderedDict()
)

_CHUNK = 4 * 1024 * 1024       # 4MB stream chunks


class TooBig(Exception):
    """Raised when a video exceeds the configured max size (carries .size)."""

    def __init__(self, size: int):
        super().__init__(f"video too big: {size} bytes")
        self.size = size


# resolvers.DownloadCancelled / resolvers.get_session are added by the
# parallel core-mods branch; fall back gracefully when running against the
# pre-merge resolvers module (tests mock everything anyway).
_DownloadCancelled = getattr(resolvers, "DownloadCancelled", None) or type(
    "DownloadCancelled", (Exception,), {}
)


def _http_session():
    getter = getattr(resolvers, "get_session", None)
    return getter() if callable(getter) else requests


def _ep_cache_key(ep: dict) -> str:
    return ep.get("url") or f"{ep.get('anime_url', '')}#{ep.get('number', '')}"


def _src_kind(url: str, host: str) -> str | None:
    low = f"{url} {host}".lower()
    if "drive.google" in low or "google drive" in low:
        return "drive"
    if "mega.nz" in low or "mega" in low:
        return "mega"
    if "gofile.io" in low or "gofile" in low:
        return "gofile"
    return None


def _download_row_quality(label: str) -> str | None:
    """Arabic download-row quality label -> 'FHD'|'HD'|None (SD skipped)."""
    for needle, quality in _DOWNLOAD_QUALITY_MAP:
        if needle in label:
            return quality
    return None


async def episode_quality_sources(ep: dict) -> dict[str, list[dict]]:
    """-> {"FHD": [{"kind": "drive"|"mega"|"gofile", "url": str, "host": str}], "HD": [...]}

    Drive + Mega (from yonaplay players) + Gofile (from the episode's own
    download rows — many newer episodes carry no drive/mega in yonaplay at
    all, only soraplay, while their download rows still offer gofile).
    4shared/dotplay/ok.ru/workupload/wahmi are excluded from batch. Deduped,
    host-sorted (drive first). Expands ALL yonaplay servers of the episode
    via scraper.get_yonaplay_players (in a thread). Results are cached in a
    6h TTL OrderedDict cache (max 500 entries).
    """
    key = _ep_cache_key(ep)
    now = time.time()
    if key in _SOURCES_CACHE:
        ts, cached = _SOURCES_CACHE[key]
        ttl = _CACHE_TTL if any(cached.values()) else _CACHE_MISS_TTL
        if now - ts < ttl:
            _SOURCES_CACHE.move_to_end(key)
            return cached
        _SOURCES_CACHE.pop(key, None)

    sources: dict[str, list[dict]] = {q: [] for q in QUALITIES}
    seen: set[tuple[str, str]] = set()
    for server in ep.get("servers") or []:
        if "yonaplay" not in (server.get("name") or "").lower():
            continue
        embed_url = server.get("embed_url") or ""
        if not embed_url:
            continue
        try:
            players = await asyncio.to_thread(
                scraper.get_yonaplay_players, embed_url
            )
        except Exception as exc:  # noqa: BLE001 — best-effort expansion
            log.warning("yonaplay expansion failed (%s): %s", embed_url, exc)
            continue
        for player in players:
            quality = player.get("quality") or ""
            if quality not in QUALITIES:
                continue
            url = player.get("url") or ""
            kind = _src_kind(url, player.get("host") or "")
            if not kind or (quality, url) in seen:
                continue
            seen.add((quality, url))
            sources[quality].append(
                {"kind": kind, "url": url, "host": player.get("host") or ""}
            )
    # Fallback: gofile rows from the episode's download section (already
    # decoded by scraper.get_episode). Accepted for batch: gofile only.
    for row in ep.get("downloads") or []:
        quality = _download_row_quality(row.get("quality") or "")
        if not quality:
            continue
        url = row.get("url") or ""
        kind = _src_kind(url, row.get("host") or "")
        if kind != "gofile" or (quality, url) in seen:
            continue
        seen.add((quality, url))
        sources[quality].append(
            {"kind": kind, "url": url, "host": row.get("host") or ""}
        )
    for quality in sources:
        sources[quality].sort(key=lambda s: PREFER_HOST.get(s["kind"], 9))

    _SOURCES_CACHE[key] = (now, sources)
    while len(_SOURCES_CACHE) > _CACHE_MAX:
        _SOURCES_CACHE.popitem(last=False)
    return sources


def nearest_quality(available: list[str], wanted: str) -> str | None:
    """Exact match first; else the nearest better quality; else the nearest
    worse one. QUALITIES order = best → worst."""
    avail = [q for q in QUALITIES if q in available]
    if not avail:
        return None
    if wanted in avail:
        return wanted
    idx = QUALITIES.index(wanted) if wanted in QUALITIES else 0
    better = [q for q in avail if QUALITIES.index(q) < idx]
    if better:
        return max(better, key=QUALITIES.index)  # nearest to wanted
    worse = [q for q in avail if QUALITIES.index(q) > idx]
    return min(worse, key=QUALITIES.index) if worse else None


def _ep_number(ep: dict) -> float:
    try:
        return float(ep.get("number") or 0)
    except (TypeError, ValueError):
        return 0.0


async def scan_season(episodes: list[dict], max_eps: int, concurrency: int = 6,
                      fetch_episode=None) -> dict:
    """Fetch episodes (bounded by a semaphore) and compute per-episode
    drive/mega sources.

    -> {"eps": [{"url", "number", "type", "sources"}],
        "counts": {"FHD": int, "HD": int},
        "truncated": bool, "total": int}
    eps are sorted by numeric number and capped at max_eps.
    """
    if fetch_episode is None:
        async def fetch_episode(ep_url: str) -> dict:  # type: ignore[misc]
            return await asyncio.to_thread(scraper.get_episode, ep_url)

    ordered = sorted(episodes, key=_ep_number)
    truncated = len(ordered) > max_eps
    ordered = ordered[:max_eps]

    sem = asyncio.Semaphore(concurrency)

    async def _one(ep: dict) -> dict:
        async with sem:
            full = None
            try:
                full = await fetch_episode(ep["url"])
            except Exception as exc:  # noqa: BLE001 — one bad ep must not kill the scan
                log.warning("scan: fetch failed for %s: %s", ep.get("url"), exc)
            sources = {q: [] for q in QUALITIES}
            if full:
                full.setdefault("url", ep["url"])
                try:
                    sources = await episode_quality_sources(full)
                except Exception as exc:  # noqa: BLE001
                    log.warning("scan: sources failed for %s: %s", ep.get("url"), exc)
            return {
                "url": ep["url"],
                "number": str(ep.get("number", "")),
                "type": ep.get("type", ""),
                "sources": sources,
            }

    eps = list(await asyncio.gather(*(_one(ep) for ep in ordered)))
    eps.sort(key=_ep_number)
    counts = {
        q: sum(1 for e in eps if e["sources"].get(q)) for q in QUALITIES
    }
    return {
        "eps": eps,
        "counts": counts,
        "truncated": truncated,
        "total": len(episodes),
    }


def pick_source(sources: dict[str, list[dict]], wanted: str) -> tuple[str, dict] | None:
    """nearest_quality, then the first source (drive is preferred already by
    the sort in episode_quality_sources). Returns (quality_used, src)."""
    available = [q for q, srcs in sources.items() if srcs]
    quality = nearest_quality(available, wanted)
    if not quality:
        return None
    return quality, sources[quality][0]


class SeasonJob:
    """One batch season-download job (one active job per user; the registry
    ACTIVE_JOBS and the `sdc|<uid>` cancel callback live in bot.py)."""

    def __init__(self, *, bot, chat_id: int, user_id: int, anime_title: str,
                 episodes: list[dict], wanted_quality: str, max_video_bytes: int,
                 fetch_episode, too_big_reply, video_timeout: int = 3600):
        self.bot = bot
        self.chat_id = chat_id
        self.user_id = user_id
        self.anime_title = anime_title
        self.episodes = episodes
        self.wanted_quality = wanted_quality
        self.max_video_bytes = max_video_bytes
        self.fetch_episode = fetch_episode        # async (ep_url) -> ep dict
        self.too_big_reply = too_big_reply        # async (ep, size) -> None
        self.video_timeout = video_timeout
        self.cancel_event = asyncio.Event()
        self.cancel_msg = "⛔ تم إلغاء التحميل."
        self.results: list[tuple[str, str, str, str]] = []  # (number, status, quality, reason)
        self.status_msg = None
        self.current = 0
        self.current_quality = wanted_quality
        self.total = len(episodes)

    def cancel(self) -> None:
        self.cancel_event.set()

    def build_status_text(self) -> str:
        return (
            f"📦 {self.anime_title}\n"
            f"⬇️ جاري تحميل الحلقة {self.current}/{self.total} "
            f"(جودة {self.current_quality})…"
        )

    def build_status_markup(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ إلغاء التحميل",
                                 callback_data=f"sdc|{self.user_id}"),
        ]])

    async def _refresh_status(self) -> None:
        if self.status_msg is None:
            return
        with contextlib.suppress(Exception):
            await self.status_msg.edit_text(
                self.build_status_text(),
                reply_markup=self.build_status_markup(),
            )

    async def _download_one(self, src: dict, path: str, state: dict) -> int:
        """Download one source into `path`. Returns bytes written.
        Drive → resolve_drive_mp4 then stream via resolvers.get_session()
        (4MB chunks); Mega/Gofile → resolvers.download_mega/download_gofile
        (..., cancel=...). Checks self.cancel_event per chunk →
        DownloadCancelled; enforces max_video_bytes → TooBig."""
        if src["kind"] in ("mega", "gofile"):
            downloader = (
                resolvers.download_mega
                if src["kind"] == "mega"
                else resolvers.download_gofile
            )
            # In-flight size guard: get_mega_size may return None, so enforce
            # max_video_bytes while the file grows instead of downloading a
            # multi-GB file only to throw it away.
            oversize = {"hit": False}

            def _cancel() -> bool:
                if self.cancel_event.is_set():
                    return True
                try:
                    if os.path.getsize(path) > self.max_video_bytes:
                        oversize["hit"] = True
                        return True
                except OSError:
                    pass
                return False

            try:
                return await asyncio.to_thread(
                    downloader, src["url"], path, state,
                    cancel=_cancel,
                )
            except _DownloadCancelled:
                if oversize["hit"]:
                    try:
                        oversize_size = os.path.getsize(path)
                    except OSError:
                        oversize_size = self.max_video_bytes + 1
                    raise TooBig(oversize_size)
                raise

        mp4_url = await asyncio.to_thread(resolvers.resolve_drive_mp4, src["url"])
        if not mp4_url:
            raise RuntimeError("تعذر استخراج رابط مباشر من Google Drive")

        def _stream() -> int:
            session = _http_session()
            with session.get(
                mp4_url,
                stream=True,
                timeout=60,
                headers={"User-Agent": resolvers.MOBILE_UA},
            ) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("Content-Length") or 0)
                state["total"] = total
                if total > self.max_video_bytes:
                    raise TooBig(total)
                written = 0
                with open(path, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=_CHUNK):
                        if self.cancel_event.is_set():
                            raise _DownloadCancelled()
                        fh.write(chunk)
                        written += len(chunk)
                        state["done"] = written
                        if written > self.max_video_bytes:
                            raise TooBig(written)
            return written

        return await asyncio.to_thread(_stream)

    def _mark_remaining_cancelled(self, start: int) -> None:
        for ep in self.episodes[start:]:
            self.results.append(
                (str(ep.get("number", "")), "cancelled", "", self.cancel_msg)
            )

    async def _process_episode(self, i: int, ep: dict) -> None:
        number = str(ep.get("number", ""))
        full = None
        try:
            full = await self.fetch_episode(ep["url"])
            full.setdefault("url", ep["url"])
            sources = await episode_quality_sources(full)
            picked = pick_source(sources, self.wanted_quality)
            if not picked:
                self.results.append(
                    (number, "failed", "", "لا توجد جودة مناسبة")
                )
                return
            quality, src = picked
            self.current_quality = quality

            if src["kind"] in ("mega", "gofile"):
                # cheap remote size guard before starting the real download
                get_size = (
                    resolvers.get_mega_size
                    if src["kind"] == "mega"
                    else resolvers.get_gofile_size
                )
                size = await asyncio.to_thread(get_size, src["url"])
                if size and size > self.max_video_bytes:
                    await self.too_big_reply(full, size)
                    self.results.append(
                        (number, "links", quality, "الحجم أكبر من الحد المسموح")
                    )
                    return

            tmp = tempfile.NamedTemporaryFile(
                prefix="witanime-season-", suffix=".mp4", delete=False
            )
            path = tmp.name
            tmp.close()
            try:
                state = {"done": 0, "total": 0}
                size = await self._download_one(src, path, state)
                if size > self.max_video_bytes:
                    raise TooBig(size)
                with open(path, "rb") as video:
                    await self.bot.send_video(
                        chat_id=self.chat_id,
                        video=video,
                        caption=(
                            f"🎬 {self.anime_title} — الحلقة {number}\n"
                            f"📦 {i}/{self.total} | جودة {quality}"
                        ),
                        supports_streaming=True,
                        read_timeout=self.video_timeout,
                        write_timeout=self.video_timeout,
                        filename=f"{self.anime_title} - الحلقة {number}.mp4",
                    )
                self.results.append((number, "sent", quality, ""))
            except TooBig as tb:
                await self.too_big_reply(full, tb.size)
                self.results.append(
                    (number, "links", quality, "الحجم أكبر من الحد المسموح")
                )
            except _DownloadCancelled:
                self.results.append(
                    (number, "cancelled", quality, self.cancel_msg)
                )
            except Exception as exc:  # noqa: BLE001 — keep the batch going
                log.warning("batch ep %s failed: %s", number, exc)
                self.results.append(
                    (number, "failed", quality, str(exc) or "خطأ غير معروف")
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        except _DownloadCancelled:
            self.results.append((number, "cancelled", "", self.cancel_msg))
        except Exception as exc:  # noqa: BLE001 — fetch/sources failure
            log.warning("batch ep %s failed early: %s", number, exc)
            self.results.append(
                (number, "failed", "", str(exc) or "خطأ غير معروف")
            )

    def build_summary_text(self) -> str:
        icons = {"sent": "✅", "links": "🔗", "failed": "❌", "cancelled": "⛔"}
        labels = {
            "sent": lambda q, r: f"أُرسلت (جودة {q})",
            "links": lambda q, r: f"أُرسلت روابط التحميل المباشر (جودة {q})",
            "failed": lambda q, r: f"فشلت — {r}",
            "cancelled": lambda q, r: "أُلغيت",
        }
        lines = [f"📦 {self.anime_title} — ملخص تحميل الموسم", ""]
        tally = {"sent": 0, "links": 0, "failed": 0, "cancelled": 0}
        for number, status, quality, reason in self.results:
            tally[status] = tally.get(status, 0) + 1
            lines.append(
                f"{icons[status]} الحلقة {number}: {labels[status](quality, reason)}"
            )
        lines.append("")
        lines.append(
            f"المجموع: {len(self.results)} | "
            f"✅ {tally['sent']} | 🔗 {tally['links']} | "
            f"❌ {tally['failed']} | ⛔ {tally['cancelled']}"
        )
        return "\n".join(lines)

    async def run(self) -> None:
        """Sequential per-episode download+send; the final detailed summary
        message is ALWAYS sent (also on cancel)."""
        try:
            self.status_msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=self.build_status_text(),
                reply_markup=self.build_status_markup(),
            )
        except Exception:  # noqa: BLE001 — status msg is nice-to-have
            log.exception("batch: status message failed")
            self.status_msg = None

        for i, ep in enumerate(self.episodes, 1):
            self.current = i
            if self.cancel_event.is_set():  # instant cancel before each episode
                self._mark_remaining_cancelled(i - 1)
                break
            await self._refresh_status()
            await self._process_episode(i, ep)
        else:
            self.current = self.total

        if self.status_msg is not None:
            with contextlib.suppress(Exception):
                await self.status_msg.edit_text(
                    f"📦 {self.anime_title}\n"
                    + (self.cancel_msg if self.cancel_event.is_set()
                       else "✅ انتهى التحميل — الملخص بالأسفل"),
                    reply_markup=InlineKeyboardMarkup([]),  # clear the cancel button
                )
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=self.build_summary_text(),
            )
        except Exception:  # noqa: BLE001
            log.exception("batch: final summary failed")
