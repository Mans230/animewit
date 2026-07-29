"""Direct mp4 resolvers for third-party embeds (best-effort, SPEC §8).

Currently supported: ok.ru
    GET https://ok.ru/videoembed/{id} with a mobile User-Agent, extract the
    JSON inside `data-options` (or `flashvars.metadata`), pick the highest
    quality mp4 from the videos list.
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


def resolve_mp4(embed_url: str) -> str | None:
    """Resolve an embed URL to a direct mp4 URL. Returns None if unsupported
    or on any failure (best-effort)."""
    try:
        if "ok.ru" in embed_url or "odnoklassniki" in embed_url:
            return _resolve_okru(embed_url)
        return None
    except Exception as exc:
        log.warning("resolve_mp4(%s) failed: %s", embed_url, exc)
        return None
