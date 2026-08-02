"""Batch season-download engine (SPEC.md §Module: season.py).

Sends a whole season (or a range of episodes) into Telegram one video after
another, from any available source: Google Drive / Mega (expanded from the
yonaplay players of each episode), ok.ru watch servers and official gofile
download links (ZIP files whose video is auto-extracted). Supports instant
cancel, per-episode TooBig fallback (direct-download links instead),
nearest-quality substitution and a detailed final Arabic summary (always
sent, even on cancel).

This module must NOT import bot.py — the job registry lives there.
"""

import asyncio
import collections
import contextlib
import logging
import os
import re
import tempfile
import time
import zipfile

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import resolvers
import scraper

log = logging.getLogger(__name__)

# order = best → worst (yonaplay exposes HD/FHD; gofile also ships SD)
QUALITIES = ["FHD", "HD", "SD"]
PREFER_HOST = {"drive": 0, "mega": 1, "okru": 2, "gofile": 3}

# user-facing Arabic labels per source kind
SRC_LABELS = {
    "drive": "Google Drive",
    "mega": "Mega",
    "okru": "ok.ru",
    "gofile": "gofile (ZIP)",
}

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
    return None


def _is_okru(name: str, embed_url: str) -> bool:
    low = f"{name} {embed_url}".lower()
    return "ok.ru" in low or "odnoklassniki" in low


def _server_name_quality(name: str) -> str:
    """ok.ru servers carry their quality as a suffix ('ok.ru - FHD');
    no suffix means HD."""
    m = re.search(r"-\s*(FHD|HD|SD)\s*$", name, re.IGNORECASE)
    return m.group(1).upper() if m else "HD"


def _download_label_quality(label: str) -> str | None:
    """Extract the quality from an Arabic download label such as
    'الجودة المتوسطة SD' — FHD must be tested before HD (substring)."""
    m = re.search(r"(FHD|HD|SD)", label, re.IGNORECASE)
    return m.group(1).upper() if m else None


async def episode_quality_sources(ep: dict) -> dict[str, list[dict]]:
    """-> {"FHD": [{"kind": "drive"|"mega"|"okru"|"gofile", "url", "host"}], ...}

    Sources: drive/mega from the yonaplay players of the episode, ok.ru watch
    servers (quality from the server-name suffix, HD by default) and gofile
    official download links (quality from the Arabic label; the ZIP is
    auto-extracted at download time). Other hosts (4shared/dotplay/
    mediafire/workupload/send...) stay manual-link only. Deduped, host-sorted
    (drive first). yonaplay servers are expanded via
    scraper.get_yonaplay_players (in a thread). Results are cached in a 6h
    TTL OrderedDict cache (max 500 entries).
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

    def _add(quality: str, kind: str, url: str, host: str) -> None:
        if quality not in QUALITIES or not url or (quality, url) in seen:
            return
        seen.add((quality, url))
        sources[quality].append({"kind": kind, "url": url, "host": host})

    for server in ep.get("servers") or []:
        name = server.get("name") or ""
        embed_url = server.get("embed_url") or ""
        if not embed_url:
            continue
        # ok.ru / odnoklassniki watch servers resolve directly to mp4
        if _is_okru(name, embed_url):
            _add(_server_name_quality(name), "okru", embed_url, "ok.ru")
            continue
        if "yonaplay" not in name.lower():
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
            kind = _src_kind(player.get("url") or "", player.get("host") or "")
            if kind:
                _add(quality, kind, player.get("url") or "", player.get("host") or "")

    # official gofile download links (ZIPs auto-extracted at download time)
    for dl in ep.get("downloads") or []:
        if "gofile" not in (dl.get("host") or "").lower():
            continue
        quality = _download_label_quality(dl.get("quality") or "")
        if quality:  # unknown-quality labels are ignored
            _add(quality, "gofile", dl.get("url") or "", "gofile")

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


_VIDEO_EXTS = (".mp4", ".mkv", ".avi")


def _extract_video(zip_path: str) -> str | None:
    """Extract the biggest video entry (.mp4/.mkv/.avi) of a gofile ZIP into
    a new temp file and return its path (caller owns cleanup). Returns None
    when the archive has no video or is corrupt."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            entries = [
                info
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(_VIDEO_EXTS)
            ]
            if not entries:
                log.warning("gofile zip has no video entry: %s", zip_path)
                return None
            biggest = max(entries, key=lambda info: info.file_size)
            ext = os.path.splitext(biggest.filename)[1] or ".mp4"
            tmp = tempfile.NamedTemporaryFile(
                prefix="witanime-gofile-", suffix=ext, delete=False
            )
            out_path = tmp.name
            try:
                with tmp, zf.open(biggest) as src_fh:
                    while True:
                        chunk = src_fh.read(_CHUNK)
                        if not chunk:
                            break
                        tmp.write(chunk)
            except Exception:
                with contextlib.suppress(OSError):
                    os.unlink(out_path)
                raise
            return out_path
    except Exception as exc:  # noqa: BLE001 — BadZipFile/OSError/... → None
        log.warning("gofile zip extract failed (%s): %s", zip_path, exc)
        return None


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
        # (number, status, quality, reason, src_label)
        self.results: list[tuple[str, str, str, str, str]] = []
        self.status_msg = None
        self.current = 0
        self.current_quality = wanted_quality
        self.current_src = ""
        self.total = len(episodes)

    def cancel(self) -> None:
        self.cancel_event.set()

    def build_status_text(self) -> str:
        src_part = f" | {self.current_src}" if self.current_src else ""
        return (
            f"📦 {self.anime_title}\n"
            f"⬇️ جاري تحميل الحلقة {self.current}/{self.total} "
            f"(جودة {self.current_quality}{src_part})…"
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

    def _stream_to(self, url: str, path: str, state: dict,
                   headers: dict | None = None) -> "callable":
        """Build the blocking stream-to-file worker shared by the drive /
        okru / gofile download paths (4MB chunks, cancel checks, TooBig)."""

        def _stream() -> int:
            session = _http_session()
            with session.get(
                url,
                stream=True,
                timeout=60,
                headers={"User-Agent": resolvers.MOBILE_UA, **(headers or {})},
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

        return _stream

    async def _download_gofile(self, src: dict, path: str, state: dict) -> int:
        """gofile: resolve to a direct ZIP link, stream the ZIP to a temp
        file (max_video_bytes enforced on the ZIP itself), extract its
        biggest video, delete the ZIP immediately, enforce the size limit on
        the extracted video and move it onto `path`."""
        resolved = await asyncio.to_thread(resolvers.resolve_gofile, src["url"])
        if not resolved:
            raise RuntimeError("تعذر استخراج رابط مباشر من gofile")
        direct_url, headers = resolved

        zip_fd, zip_path = tempfile.mkstemp(
            prefix="witanime-gofile-", suffix=".zip"
        )
        os.close(zip_fd)
        try:
            await asyncio.to_thread(
                self._stream_to(direct_url, zip_path, state, headers)
            )
            extracted = await asyncio.to_thread(_extract_video, zip_path)
        finally:
            with contextlib.suppress(OSError):  # the ZIP never outlives this
                os.unlink(zip_path)
        if not extracted:
            raise RuntimeError("ملف gofile لا يحتوي على فيديو")
        try:
            size = os.path.getsize(extracted)
            if size > self.max_video_bytes:
                raise TooBig(size)
            os.replace(extracted, path)  # same temp dir → atomic rename
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(extracted)
            raise
        state["total"] = size
        state["done"] = size
        return size

    async def _download_one(self, src: dict, path: str, state: dict) -> int:
        """Download one source into `path`. Returns bytes written.
        Drive → resolve_drive_mp4 then stream via resolvers.get_session()
        (4MB chunks); ok.ru → resolve_mp4 then the same stream path;
        Mega → resolvers.download_mega(..., cancel=...);
        gofile → resolve_gofile + ZIP auto-extract (_download_gofile).
        Checks self.cancel_event per chunk → DownloadCancelled; enforces
        max_video_bytes → TooBig."""
        if src["kind"] == "mega":
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
                    resolvers.download_mega, src["url"], path, state,
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

        if src["kind"] == "gofile":
            return await self._download_gofile(src, path, state)

        # drive + okru share the resolve-then-stream path
        if src["kind"] == "okru":
            mp4_url = await asyncio.to_thread(resolvers.resolve_mp4, src["url"])
            fail_msg = "تعذر استخراج رابط مباشر من ok.ru"
        else:
            mp4_url = await asyncio.to_thread(resolvers.resolve_drive_mp4, src["url"])
            fail_msg = "تعذر استخراج رابط مباشر من Google Drive"
        if not mp4_url:
            raise RuntimeError(fail_msg)

        return await asyncio.to_thread(self._stream_to(mp4_url, path, state))

    def _mark_remaining_cancelled(self, start: int) -> None:
        for ep in self.episodes[start:]:
            self.results.append(
                (str(ep.get("number", "")), "cancelled", "", self.cancel_msg, "")
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
                    (number, "failed", "", "لا توجد جودة مناسبة", "")
                )
                return
            quality, src = picked
            self.current_quality = quality
            src_label = SRC_LABELS.get(src["kind"], src["kind"])
            self.current_src = src_label

            if src["kind"] == "mega":
                # cheap remote size guard before starting the real download
                size = await asyncio.to_thread(resolvers.get_mega_size, src["url"])
                if size and size > self.max_video_bytes:
                    await self.too_big_reply(full, size)
                    self.results.append(
                        (number, "links", quality,
                         "الحجم أكبر من الحد المسموح", src_label)
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
                            f"📦 {i}/{self.total} | جودة {quality} | {src_label}"
                        ),
                        supports_streaming=True,
                        read_timeout=self.video_timeout,
                        write_timeout=self.video_timeout,
                        filename=f"{self.anime_title} - الحلقة {number}.mp4",
                    )
                self.results.append((number, "sent", quality, "", src_label))
            except TooBig as tb:
                await self.too_big_reply(full, tb.size)
                self.results.append(
                    (number, "links", quality,
                     "الحجم أكبر من الحد المسموح", src_label)
                )
            except _DownloadCancelled:
                self.results.append(
                    (number, "cancelled", quality, self.cancel_msg, src_label)
                )
            except Exception as exc:  # noqa: BLE001 — keep the batch going
                log.warning("batch ep %s failed: %s", number, exc)
                self.results.append(
                    (number, "failed", quality,
                     str(exc) or "خطأ غير معروف", src_label)
                )
            finally:
                with contextlib.suppress(OSError):
                    os.unlink(path)
        except _DownloadCancelled:
            self.results.append((number, "cancelled", "", self.cancel_msg, ""))
        except Exception as exc:  # noqa: BLE001 — fetch/sources failure
            log.warning("batch ep %s failed early: %s", number, exc)
            self.results.append(
                (number, "failed", "", str(exc) or "خطأ غير معروف", "")
            )

    def build_summary_text(self) -> str:
        icons = {"sent": "✅", "links": "🔗", "failed": "❌", "cancelled": "⛔"}
        labels = {
            "sent": lambda q, r, s: f"أُرسلت (جودة {q} | {s})",
            "links": lambda q, r, s: f"أُرسلت روابط التحميل المباشر (جودة {q} | {s})",
            "failed": lambda q, r, s: f"فشلت — {r}",
            "cancelled": lambda q, r, s: "أُلغيت",
        }
        lines = [f"📦 {self.anime_title} — ملخص تحميل الموسم", ""]
        tally = {"sent": 0, "links": 0, "failed": 0, "cancelled": 0}
        for number, status, quality, reason, src_label in self.results:
            tally[status] = tally.get(status, 0) + 1
            lines.append(
                f"{icons[status]} الحلقة {number}: "
                f"{labels[status](quality, reason, src_label)}"
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
