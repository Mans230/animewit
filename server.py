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

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, PlainTextResponse

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
        sandbox="allow-scripts allow-same-origin allow-presentation allow-forms"
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
        src = html.escape(embed_url, quote=True)
        return HTMLResponse(WATCH_PAGE.format(src=src))

    return app
