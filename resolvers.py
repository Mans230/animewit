"""Direct mp4 resolvers for third-party embeds (best-effort, SPEC §8).

Supported:
- ok.ru: GET https://ok.ru/videoembed/{id} with a mobile User-Agent, extract
  the JSON inside `data-options` (or `flashvars.metadata`), pick the highest
  quality mp4 from the videos list.
- Google Drive: turn a `/file/d/{ID}/preview` link into the direct download
  stream at drive.usercontent.google.com and verify it serves video bytes.
"""

import html as html_module
import json
import logging
import re

import requests

log = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

TIMEOUT = 30

# ok.ru quality names ordered from best to worst
_QUALITY_RANK = ["ultra", "quad", "full", "hd", "sd", "low", "lowest", "mobile"]


def _resolve_okru(embed_url: str) -> str | None:
    """Extract the best-quality mp4 URL from an ok.ru embed page."""
    resp = requests.get(
        embed_url, headers={"User-Agent": MOBILE_UA}, timeout=TIMEOUT
    )
    resp.raise_for_status()
    page = resp.text

    metadata = None

    # Primary: JSON inside the data-options attribute of the player div
    m = re.search(r'data-options="([^"]+)"', page)
    if m:
        try:
            opts = json.loads(html_module.unescape(m.group(1)))
            metadata = json.loads(opts["flashvars"]["metadata"])
        except (ValueError, KeyError) as exc:
            log.warning("ok.ru data-options parse failed: %s", exc)

    # Fallback: flashvars.metadata embedded in a script
    if metadata is None:
        m = re.search(r'"metadata"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
        if m:
            try:
                metadata = json.loads(json.loads(f'"{m.group(1)}"'))
            except ValueError as exc:
                log.warning("ok.ru flashvars.metadata parse failed: %s", exc)

    if not metadata:
        return None

    videos = metadata.get("videos") or []
    if not videos:
        return None

    def rank(video: dict) -> int:
        name = (video.get("name") or "").lower()
        try:
            return _QUALITY_RANK.index(name)
        except ValueError:
            return len(_QUALITY_RANK)

    best = min(videos, key=rank)
    url = best.get("url")
    # ok.ru sometimes returns protocol-relative URLs
    if url and url.startswith("//"):
        url = "https:" + url
    return url or None


def _looks_like_video(headers) -> bool:
    """content-type is video/* or the resource is big enough to be a video."""
    ctype = (headers.get("Content-Type") or "").lower()
    if "video" in ctype or "octet-stream" in ctype:
        return True
    try:
        return int(headers.get("Content-Length") or 0) > 1024 * 1024
    except ValueError:
        return False


def resolve_drive_mp4(preview_url: str) -> str | None:
    """Turn a Google Drive `/file/d/{ID}/preview` URL into a direct mp4
    download URL and verify (HEAD, ranged GET fallback) that it serves video.
    Returns the direct URL or None on any failure."""
    m = re.search(r"/file/d/([A-Za-z0-9_-]+)", preview_url)
    if not m:
        return None
    file_id = m.group(1)
    direct = (
        "https://drive.usercontent.google.com/download"
        f"?id={file_id}&export=download&confirm=t"
    )
    headers = {"User-Agent": MOBILE_UA}
    try:
        resp = requests.head(direct, headers=headers, timeout=TIMEOUT, allow_redirects=True)
        if resp.ok and _looks_like_video(resp.headers):
            return direct
    except requests.RequestException as exc:
        log.warning("drive HEAD probe failed: %s", exc)
    # Fallback: some endpoints reject HEAD -> probe with a 1-byte ranged GET
    try:
        with requests.get(
            direct,
            headers={**headers, "Range": "bytes=0-0"},
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
        ) as resp:
            if resp.ok and _looks_like_video(resp.headers):
                return direct
    except requests.RequestException as exc:
        log.warning("drive range probe failed: %s", exc)
    return None


def resolve_mp4(embed_url: str) -> str | None:
    """Resolve an embed URL to a direct mp4 URL. Returns None if unsupported
    or on any failure (best-effort)."""
    try:
        if "ok.ru" in embed_url or "odnoklassniki" in embed_url:
            return _resolve_okru(embed_url)
        if "drive.google.com" in embed_url:
            return resolve_drive_mp4(embed_url)
        return None
    except Exception as exc:
        log.warning("resolve_mp4(%s) failed: %s", embed_url, exc)
        return None


# ---------------------------------------------------------------------------
# Mega.nz — public file links are directly downloadable via the Mega API
# (mega.py library). Embed URLs (mega.nz/embed/ID#KEY) are equivalent to
# /file/ URLs, so we normalize first.
# ---------------------------------------------------------------------------
def normalize_mega_url(url: str) -> str | None:
    """Return a canonical mega.nz/file/... URL, or None if not a Mega link."""
    if not url or "mega.nz" not in url:
        return None
    return url.replace("/embed/", "/file/")


def get_mega_size(url: str) -> int | None:
    """Best-effort remote size lookup for a public Mega file (bytes)."""
    try:
        from mega import Mega
        info = Mega().get_public_url_info(url)
        size = int((info or {}).get("size") or 0)
        return size or None
    except Exception as exc:  # noqa: BLE001 — size is best-effort only
        log.warning("mega size lookup failed: %s", exc)
        return None


def download_mega(url: str, path: str) -> int:
    """Blocking download of a public Mega file to `path`. Returns bytes written.
    Raises on any failure — the caller reports MSG_RESOLVE_FAIL."""
    import os as _os

    from mega import Mega

    dest_dir = _os.path.dirname(path)
    dest_name = _os.path.basename(path)
    Mega().download_url(url, dest_path=dest_dir, dest_filename=dest_name)
    return _os.path.getsize(path)
