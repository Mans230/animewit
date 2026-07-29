"""Scraper for witanime.life — contracts defined in SPEC.md §4/§5.

All decode algorithms are verified against the live site (see SPEC §5).
"""

import base64
import json
import logging
import re

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE = "https://witanime.life"

YONAPLAY_API_KEY = "23a97133-caf3-4eb4-9466-93d0a4ff8198"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}

TIMEOUT = 30


class ScraperError(Exception):
    """Raised when fetching or parsing witanime fails."""


def _get(url: str) -> str:
    """GET url and return response text, raising ScraperError on failure."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except requests.RequestException as exc:
        log.warning("GET %s failed: %s", url, exc)
        raise ScraperError(str(exc)) from exc


def search_anime(query: str) -> list[dict]:
    """GET {BASE}/?search_param=animes&s={query}
    -> [{"title": str, "url": str, "poster": str, "type": str, "status": str}]
    """
    url = f"{BASE}/?search_param=animes&s={requests.utils.quote(query)}"
    soup = BeautifulSoup(_get(url), "html.parser")
    results = []
    for card in soup.select("div.anime-card-container"):
        title_a = card.select_one(".anime-card-title h3 a")
        poster = card.select_one(".anime-card-poster img.img-responsive")
        type_a = card.select_one(".anime-card-type a")
        status_a = card.select_one(".anime-card-status a")
        if not title_a:
            continue
        results.append(
            {
                "title": title_a.get_text(strip=True),
                "url": title_a.get("href", ""),
                "poster": poster.get("src", "") if poster else "",
                "type": type_a.get_text(strip=True) if type_a else "",
                "status": status_a.get_text(strip=True) if status_a else "",
            }
        )
    return results


def _decode_episode_data(html: str) -> list[dict]:
    """Decode `var processedEpisodeData = 'A.B'` (SPEC §5.1)."""
    m = re.search(r"var processedEpisodeData\s*=\s*'([^']+)'", html)
    if not m:
        return []
    try:
        a_part, b_part = m.group(1).split(".")
        a = base64.b64decode(a_part)
        key = base64.b64decode(b_part)
        episodes = json.loads(
            bytes(a[i] ^ key[i % len(key)] for i in range(len(a)))
        )
        return episodes
    except Exception as exc:
        log.warning("processedEpisodeData decode failed: %s", exc)
        return []


def _episode_number(ep: dict) -> float:
    """Numeric sort key for an episode dict (handles '12.5', '1', ...)."""
    try:
        return float(ep.get("number", "0"))
    except (TypeError, ValueError):
        return 0.0


def get_anime_info(anime_url: str) -> dict:
    """-> {"title", "poster", "story", "genres": [str],
          "episodes": [{"number", "url", "type", "screenshot"}]}
    Episodes are sorted ascending by number.
    """
    html = _get(anime_url)
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.select_one("h1.anime-details-title")
    poster_el = soup.select_one(".anime-info-right .anime-thumbnail img")
    story_el = soup.select_one("p.anime-story")

    episodes = _decode_episode_data(html)
    episodes.sort(key=_episode_number)

    return {
        "title": title_el.get_text(strip=True) if title_el else "",
        "poster": poster_el.get("src", "") if poster_el else "",
        "story": story_el.get_text(strip=True) if story_el else "",
        "genres": [g.get_text(strip=True) for g in soup.select("ul.anime-genres li a")],
        "episodes": [
            {
                "number": str(ep.get("number", "")),
                "url": ep.get("url", ""),
                "type": ep.get("type", ""),
                "screenshot": ep.get("screenshot", ""),
            }
            for ep in episodes
        ],
    }


def _decode_watch_servers(html: str) -> list[str]:
    """Decode watch server embed URLs from `var _zH` / `var _zW` (SPEC §5.2)."""
    m_res = re.search(r'var _zH\s*=\s*"([^"]+)"', html)
    m_cfg = re.search(r'var _zW\s*=\s*"([^"]+)"', html)
    if not (m_res and m_cfg):
        return []
    try:
        res = json.loads(base64.b64decode(m_res.group(1)))
        cfg = json.loads(base64.b64decode(m_cfg.group(1)))
    except Exception as exc:
        log.warning("_zH/_zW parse failed: %s", exc)
        return []

    def decode_server(i: int) -> str:
        d = re.sub(r"[^A-Za-z0-9+/=]", "", res[i][::-1])
        c = cfg[i]
        off = c["d"][int(base64.b64decode(c["k"]))]
        return base64.b64decode(d)[:-off].decode()

    urls = []
    for i in range(len(res)):
        try:
            urls.append(decode_server(i))
        except Exception as exc:
            log.warning("server %d decode failed: %s", i, exc)
    return urls


def _decode_download_urls(html: str) -> list[str]:
    """Decode download URLs from `_m`, `_t`, `_s`, `_p0.._p{n-1}` (SPEC §5.3).

    Order of decoded URLs (index 0..count-1) matches the order of
    `a.download-link[data-index]` buttons in the page.
    """
    try:
        secret = base64.b64decode(
            json.loads(re.search(r"var _m = (\{.*?\});", html).group(1))["r"]
        ).decode()
        count = int(json.loads(re.search(r"var _t = (\{.*?\});", html).group(1))["l"])
        sarr = json.loads(re.search(r"var _s = (\[.*?\]);", html).group(1))
    except (AttributeError, ValueError, KeyError) as exc:
        log.warning("download vars parse failed: %s", exc)
        return []

    def xor_hex(hx: str, k: str) -> str:
        d = bytes.fromhex(hx)
        return bytes(b ^ ord(k[i % len(k)]) for i, b in enumerate(d)).decode()

    urls = []
    for i in range(count):
        try:
            chunks = json.loads(re.search(r"var _p%d = (\[.*?\]);" % i, html).group(1))
            seq = json.loads(xor_hex(sarr[i], secret))  # ordering
            dec = [xor_hex(c, secret) for c in chunks]  # decoded in send order
            arranged = [None] * len(seq)
            for j, s in enumerate(seq):
                arranged[s] = dec[j]
            urls.append("".join(arranged))
        except Exception as exc:
            log.warning("download %d decode failed: %s", i, exc)
    return urls


# Friendly labels for known yonaplay player hosts (matched against the span text)
_YONAPLAY_HOST_LABELS = (
    ("drive.google", "Google Drive"),
    ("mega.nz", "Mega"),
    ("4shared", "4shared"),
)


def get_yonaplay_players(embed_url: str) -> list[dict]:
    """Fetch a yonaplay embed page (Referer: witanime.life is required, it 404s
    otherwise) and extract the alternative player links hidden inside
    `onclick="go_to_player('<base64>')"` of div.OD_SUB (HD) / div.OD_SUB2 (FHD).

    -> [{"host": "Google Drive"|"Mega"|"4shared"|span text,
         "quality": "HD"|"FHD", "url": str}]
    Returns [] on any failure.
    """
    try:
        resp = requests.get(
            embed_url,
            headers={**HEADERS, "Referer": BASE + "/"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.warning("yonaplay fetch failed (%s): %s", embed_url, exc)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    players = []
    for selector, quality in (("div.OD_SUB", "HD"), ("div.OD_SUB2", "FHD")):
        div = soup.select_one(selector)
        if not div:
            continue
        for li in div.select("li"):
            m = re.search(r"go_to_player\('([^']+)'\)", li.get("onclick") or "")
            if not m:
                continue
            try:
                b64 = m.group(1)
                url = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode()
            except Exception as exc:
                log.warning("yonaplay player decode failed: %s", exc)
                continue
            if not url.startswith(("http://", "https://")):
                continue
            span = li.select_one("span")
            raw_host = span.get_text(strip=True) if span else ""
            host = raw_host
            for needle, label in _YONAPLAY_HOST_LABELS:
                if needle in raw_host.lower():
                    host = label
                    break
            players.append({"host": host, "quality": quality, "url": url})
    return players


def get_episode(ep_url: str) -> dict:
    """-> {"title", "anime_title", "anime_url", "number",
          "prev_url": str|None, "next_url": str|None,
          "servers": [{"name", "embed_url"}],
          "downloads": [{"quality", "host", "url"}]}
    """
    html = _get(ep_url)
    soup = BeautifulSoup(html, "html.parser")

    # --- titles ---
    anime_link = soup.select_one(".anime-page-link a")
    anime_title = anime_link.get_text(strip=True) if anime_link else ""
    anime_url = anime_link.get("href", "") if anime_link else ""
    title_tag = soup.select_one("title")
    title = title_tag.get_text(strip=True) if title_tag else anime_title
    m_num = re.search(r"الحلقة\s+([\d.]+)", title) or re.search(r"-([\d.]+)/?$", ep_url)
    number = m_num.group(1) if m_num else ""

    # --- prev / next ---
    prev_a = soup.select_one(".previous-episode a")
    next_a = soup.select_one(".next-episode a")
    prev_url = prev_a.get("href") if prev_a else None
    next_url = next_a.get("href") if next_a else None

    # --- watch servers (names from DOM, URLs decoded in matching order) ---
    server_names = []
    for a in soup.select("#episode-servers li a.server-link"):
        span = a.select_one("span.ser")
        server_names.append(span.get_text(strip=True) if span else a.get_text(strip=True))
    server_urls = _decode_watch_servers(html)
    servers = []
    for name, embed in zip(server_names, server_urls):
        if "yonaplay" in name.lower():
            embed += "&apiKey=" + YONAPLAY_API_KEY
        servers.append({"name": name, "embed_url": embed})

    # --- downloads (quality rows from ul.quality-list, URLs by data-index) ---
    decoded = _decode_download_urls(html)
    downloads = []
    for qlist in soup.select("ul.quality-list"):
        lis = qlist.select("li")
        if not lis:
            continue
        quality = lis[0].get_text(strip=True)  # li:first-child = quality name
        for a in qlist.select("a.download-link"):
            host_el = a.select_one("span.notice")
            host = host_el.get_text(strip=True) if host_el else a.get_text(strip=True)
            try:
                idx = int(a.get("data-index", "-1"))
            except ValueError:
                continue
            if 0 <= idx < len(decoded):
                downloads.append({"quality": quality, "host": host, "url": decoded[idx]})

    return {
        "title": title,
        "anime_title": anime_title,
        "anime_url": anime_url,
        "number": number,
        "prev_url": prev_url,
        "next_url": next_url,
        "servers": servers,
        "downloads": downloads,
    }
