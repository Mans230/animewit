"""FastAPI watch page (SPEC §7) — runs inside the same process as bot.py.

GET /watch?u=<base64url(embed_url)>&sig=<hmac>
    Minimal HTML page: black background, only a fullscreen iframe player.
    sig = hmac_sha256(WATCH_SECRET, u).hexdigest()[:16] — 403 on mismatch.
GET /
    200 "OK" (Railway health check).
"""

import base64
import hashlib
import hmac
import html
import logging
import time

import requests
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

log = logging.getLogger(__name__)

# --- anti-iframe challenge detection ---------------------------------------
# Some ad-heavy hosts (upvideo, streamsb/sbanh, highload, ...) answer embed
# requests with a tiny JS "Redirecting..." anti-abuse challenge that never
# completes inside a third-party iframe (black screen). We probe each embed
# once (cached) and, when the challenge is detected, redirect the browser to
# the embed URL top-level instead of iframing it — the challenge then runs
# full-window and the player works.
_CHALLENGE_CACHE: dict[str, tuple[bool, float]] = {}
_CHALLENGE_TTL = 6 * 3600
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _has_iframe_challenge(embed_url: str) -> bool:
    cached = _CHALLENGE_CACHE.get(embed_url)
    if cached and time.time() - cached[1] < _CHALLENGE_TTL:
        return cached[0]
    result = False
    try:
        try:
            r = requests.get(
                embed_url, headers={"User-Agent": _DESKTOP_UA}, timeout=6
            )
        except requests.exceptions.SSLError:
            r = requests.get(
                embed_url, headers={"User-Agent": _DESKTOP_UA}, timeout=6, verify=False
            )
        body = r.text[:20000]
        result = "adBlockingDetected" in body or "<title>Redirecting" in body
    except Exception as exc:
        log.info("challenge probe failed for %s: %s", embed_url, exc)
    if len(_CHALLENGE_CACHE) > 2000:
        _CHALLENGE_CACHE.clear()
    _CHALLENGE_CACHE[embed_url] = (result, time.time())
    if result:
        log.info("iframe challenge detected -> redirect mode: %s", embed_url)
    return result

WATCH_PAGE = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>مشاهدة</title>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; background: #000; overflow: hidden; }}
  iframe {{ position: fixed; inset: 0; width: 100vw; height: 100vh; border: 0; }}
  #loading {{
    position: fixed; top: 12px; left: 50%; transform: translateX(-50%);
    background: rgba(20,20,20,.85); color: #eee; z-index: 10;
    padding: 6px 14px; border-radius: 20px; font: 14px/1.6 sans-serif;
  }}
  #fallback {{
    position: fixed; bottom: 8px; left: 50%; transform: translateX(-50%);
    z-index: 10; font: 12px/1.6 sans-serif;
  }}
  #fallback a {{ color: #8ab4f8; text-decoration: none; }}
</style>
</head>
<body>
<div id="loading">جاري تحميل المشغل... ⏳</div>
<iframe src="{src}" allowfullscreen scrolling="no"
        referrerpolicy="no-referrer"
        allow="autoplay; fullscreen; encrypted-media; picture-in-picture"
        onload="document.getElementById('loading').style.display='none'"></iframe>
<div id="fallback"><a href="{src}" target="_blank" rel="noopener">فتح في تاب جديد ↗</a></div>
</body>
</html>"""


def b64url_encode(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def b64url_decode(s: str) -> str:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad).decode()


def sign(u: str, secret: str) -> str:
    """sig = hmac_sha256(secret, u).hexdigest()[:16]"""
    return hmac.new(secret.encode(), u.encode(), hashlib.sha256).hexdigest()[:16]


def make_watch_url(base_public_url: str, embed_url: str, secret: str) -> str:
    """Build a signed watch-page URL for a bot button."""
    u = b64url_encode(embed_url)
    return f"{base_public_url.rstrip('/')}/watch?u={u}&sig={sign(u, secret)}"


def build_app(watch_secret: str) -> FastAPI:
    app = FastAPI(title="witanime search-bot watch server", docs_url=None, redoc_url=None)

    @app.get("/", response_class=PlainTextResponse)
    async def health() -> str:
        return "OK"

    @app.get("/watch", response_class=HTMLResponse)
    async def watch(u: str = Query(...), sig: str = Query(...)):
        expected = sign(u, watch_secret)
        if not hmac.compare_digest(sig, expected):
            return PlainTextResponse("Forbidden", status_code=403)
        try:
            embed_url = b64url_decode(u)
        except Exception:
            return PlainTextResponse("Bad Request", status_code=400)
        if not embed_url.startswith(("http://", "https://")):
            return PlainTextResponse("Bad Request", status_code=400)
        if _has_iframe_challenge(embed_url):
            # anti-iframe host: send the browser straight to the player page
            return RedirectResponse(embed_url, status_code=302)
        src = html.escape(embed_url, quote=True)
        return HTMLResponse(WATCH_PAGE.format(src=src))

    return app
