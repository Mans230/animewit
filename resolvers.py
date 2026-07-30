"""Direct mp4 resolvers for third-party embeds (best-effort, SPEC §8).

Supported:
- ok.ru: GET https://ok.ru/videoembed/{id} with a mobile User-Agent, extract
  the JSON inside `data-options` (or `flashvars.metadata`), pick the highest
  quality mp4 from the videos list.
- Google Drive: turn a `/file/d/{ID}/preview` link into the direct download
  stream at drive.usercontent.google.com and verify it serves video bytes.
"""

import base64
import html as html_module
import json
import logging
import random
import re
import struct

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
# Mega.nz — minimal public-file downloader implemented directly on the Mega
# API (no third-party library: mega.py pins an ancient tenacity that is
# broken on Python 3.11+). A public file link carries a 256-bit key in its
# URL fragment; the file is fetched from g.api.mega.nz and decrypted inline
# with AES-CTR.
# ---------------------------------------------------------------------------
_MEGA_API = "https://g.api.mega.nz/cs"
_MEGA_ERRORS = {
    -1: "internal error",
    -2: "bad arguments",
    -3: "temporary failure (retry)",
    -9: "file not found",
    -11: "access violation (deleted/protected)",
    -16: "file blocked",
    -17: "over quota",
}


def normalize_mega_url(url: str) -> str | None:
    """Return the URL unchanged if it is a Mega link, else None.
    (Both /file/ and /embed/ formats are parsed directly.)"""
    if not url or "mega.nz" not in url:
        return None
    return url


def _mega_b64d(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def _parse_mega_url(url: str) -> tuple[str, str] | None:
    """Extract (file_id, key_b64) from /file/, /embed/ and legacy #! URLs."""
    m = re.search(r"mega\.nz/(?:file|embed)/([A-Za-z0-9_-]+)[#!]([A-Za-z0-9_-]+)", url)
    if not m:
        m = re.search(r"mega\.nz/#!([A-Za-z0-9_-]+)!([A-Za-z0-9_-]+)", url)
    return (m.group(1), m.group(2)) if m else None


def _mega_api(payload: dict) -> dict:
    """Single Mega API call; returns the response object or raises."""
    r = requests.post(
        _MEGA_API,
        params={"id": str(random.randint(0, 0xFFFFFFFF))},
        json=[payload],
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list) or not data:
        raise RuntimeError("mega: empty api response")
    item = data[0]
    if isinstance(item, int):
        raise RuntimeError(f"mega api error {item}: {_MEGA_ERRORS.get(item, 'unknown')}")
    if "g" not in item:
        raise RuntimeError("mega: file not accessible")
    return item


def _mega_key_iv(key_b64: str) -> tuple[bytes, int]:
    """Derive the 128-bit AES key and CTR initial value from the URL key."""
    fk = struct.unpack(">8I", _mega_b64d(key_b64))
    key = struct.pack(">4I", fk[0] ^ fk[4], fk[1] ^ fk[5], fk[2] ^ fk[6], fk[3] ^ fk[7])
    iv_init = ((fk[4] << 32) + fk[5]) << 64
    return key, iv_init


def get_mega_size(url: str) -> int | None:
    """Best-effort remote size lookup for a public Mega file (bytes)."""
    try:
        parsed = _parse_mega_url(url)
        if not parsed:
            return None
        item = _mega_api({"a": "g", "g": 1, "p": parsed[0]})
        return int(item.get("s") or 0) or None
    except Exception as exc:  # noqa: BLE001 — size is best-effort only
        log.warning("mega size lookup failed: %s", exc)
        return None


def download_mega(url: str, path: str, state: dict | None = None) -> int:
    """Blocking download+decrypt of a public Mega file to `path`.
    `state["done"]`/`state["total"]` are updated for progress reporting.
    Returns bytes written; raises on any failure."""
    from Crypto.Cipher import AES
    from Crypto.Util import Counter

    parsed = _parse_mega_url(url)
    if not parsed:
        raise RuntimeError("mega: unsupported url format")
    file_id, key_b64 = parsed
    key, iv_init = _mega_key_iv(key_b64)
    item = _mega_api({"a": "g", "g": 1, "p": file_id})
    total = int(item.get("s") or 0)
    if state is not None:
        state["total"] = total
    counter = Counter.new(128, initial_value=iv_init)
    aes = AES.new(key, AES.MODE_CTR, counter=counter)
    written = 0
    with requests.get(item["g"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                f.write(aes.decrypt(chunk))
                written += len(chunk)
                if state is not None:
                    state["done"] = written
    return written
