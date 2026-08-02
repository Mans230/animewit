"""Direct mp4 resolvers for third-party embeds (best-effort, SPEC §8).

Supported:
- ok.ru: GET https://ok.ru/videoembed/{id} with a mobile User-Agent, extract
  the JSON inside `data-options` (or `flashvars.metadata`), pick the highest
  quality mp4 from the videos list.
- Google Drive: turn a `/file/d/{ID}/preview` link into the direct download
  stream at drive.usercontent.google.com and verify it serves video bytes.
- gofile.io: public API (guest account token + /contents) -> direct link of
  the first file plus the required accountToken Cookie header.
"""

import base64
import html as html_module
import json
import logging
import random
import re
import socket
import struct
import threading
import time
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

TIMEOUT = 30

# ok.ru quality names ordered from best to worst
_QUALITY_RANK = ["ultra", "quad", "full", "hd", "sd", "low", "lowest", "mobile"]


class DownloadCancelled(Exception):
    """Raised when a download is cancelled via its `cancel` callback."""


# ---------------------------------------------------------------------------
# Shared keep-alive session (SPEC perf fix): pooled connections for all
# direct-download streaming. Lazy so importing the module never does I/O.
# ---------------------------------------------------------------------------
_SESSION: "requests.Session | None" = None
_SESSION_LOCK = threading.Lock()


def get_session() -> requests.Session:
    """Process-wide requests.Session with an enlarged connection pool."""
    global _SESSION
    if _SESSION is None:
        with _SESSION_LOCK:
            if _SESSION is None:
                s = requests.Session()
                adapter = HTTPAdapter(
                    pool_connections=8, pool_maxsize=32, max_retries=2
                )
                s.mount("https://", adapter)
                s.mount("http://", adapter)
                _SESSION = s
    return _SESSION


# ---------------------------------------------------------------------------
# In-process resolve cache: successful resolutions live 6h, failures (None)
# 10 minutes only. Keyed by the embed/preview URL.
# ---------------------------------------------------------------------------
_RESOLVE_CACHE: dict[str, tuple[float, "str | None"]] = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_CACHE_TTL_OK = 6 * 3600
_CACHE_TTL_NONE = 10 * 60


def _cache_get(key: str) -> "tuple[str | None, bool]":
    """-> (value, hit). A cached None is a real hit (negative cache)."""
    with _RESOLVE_CACHE_LOCK:
        item = _RESOLVE_CACHE.get(key)
        if item is None:
            return None, False
        ts, val = item
        ttl = _CACHE_TTL_OK if val is not None else _CACHE_TTL_NONE
        if time.time() - ts > ttl:
            del _RESOLVE_CACHE[key]
            return None, False
        return val, True


def _cache_put(key: str, val: "str | None") -> None:
    with _RESOLVE_CACHE_LOCK:
        _RESOLVE_CACHE[key] = (time.time(), val)


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
    """Cached wrapper around the drive resolver (6h TTL; None cached 10 min)."""
    cached, hit = _cache_get(preview_url)
    if hit:
        return cached
    result = _resolve_drive_mp4_uncached(preview_url)
    _cache_put(preview_url, result)
    return result


def _resolve_drive_mp4_uncached(preview_url: str) -> str | None:
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


# ---------------------------------------------------------------------------
# gofile.io — public API resolver. A guest account token is created on demand
# (POST /accounts), cached in memory and regenerated on 401/403. The content
# endpoint returns a `children` dict; the first child of type "file" carries
# the direct `link`. Downloading that link requires the
# `Cookie: accountToken={token}` header, so resolve_gofile returns the header
# dict together with the URL.
# ---------------------------------------------------------------------------
_GOFILE_API = "https://api.gofile.io"
_GOFILE_WT = "4fd6sg89d7s6"  # public web token used by the gofile frontend
_GOFILE_TOKEN: "str | None" = None
_GOFILE_TOKEN_LOCK = threading.Lock()


def _gofile_fresh_token() -> str:
    """Create a guest gofile account and return its API token."""
    resp = get_session().post(f"{_GOFILE_API}/accounts", timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    token = (data.get("data") or {}).get("token") if data.get("status") == "ok" else None
    if not token:
        raise RuntimeError(f"gofile: account creation failed: {data.get('status')}")
    return token


def _gofile_token(force_refresh: bool = False) -> str:
    global _GOFILE_TOKEN
    with _GOFILE_TOKEN_LOCK:
        if _GOFILE_TOKEN is None or force_refresh:
            _GOFILE_TOKEN = _gofile_fresh_token()
        return _GOFILE_TOKEN


def _gofile_content(content_id: str) -> tuple[dict, str]:
    """GET /contents/{id}; on 401/403 regenerate the token and retry once.
    Returns (json_payload, token_used)."""
    for attempt in range(2):
        token = _gofile_token(force_refresh=bool(attempt))
        resp = get_session().get(
            f"{_GOFILE_API}/contents/{content_id}",
            params={"wt": _GOFILE_WT, "cache": "true"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT,
        )
        if resp.status_code in (401, 403) and attempt == 0:
            log.info("gofile token rejected (%s) — refreshing", resp.status_code)
            continue
        resp.raise_for_status()
        return resp.json(), token
    raise RuntimeError("gofile: content request failed after token refresh")


def resolve_gofile(page_url: str) -> "tuple[str, dict] | None":
    """Resolve a gofile.io/d/{id} page URL to (direct_url, headers) for the
    first file inside it. headers carries the required accountToken cookie.
    Cached like the other resolvers (6h; failures 10 min). None on failure."""
    key = f"gofile|{page_url}"
    cached, hit = _cache_get(key)
    if hit:
        return cached
    result = _resolve_gofile_uncached(page_url)
    _cache_put(key, result)
    return result


def _resolve_gofile_uncached(page_url: str) -> "tuple[str, dict] | None":
    try:
        m = re.search(r"/d/([A-Za-z0-9]+)", page_url)
        if not m:
            log.warning("gofile: cannot parse content id from %s", page_url)
            return None
        content_id = m.group(1)
        data, token = _gofile_content(content_id)
        children = ((data.get("data") or {}).get("children") or {})
        for child in children.values():
            if child.get("type") == "file" and child.get("link"):
                return child["link"], {"Cookie": f"accountToken={token}"}
        log.warning("gofile: no file child in content %s", content_id)
        return None
    except Exception as exc:  # noqa: BLE001 — best-effort resolver
        log.warning("resolve_gofile(%s) failed: %s", page_url, exc)
        return None


def resolve_mp4(embed_url: str) -> str | None:
    """Cached wrapper around resolve_mp4 (6h TTL; None cached 10 min)."""
    cached, hit = _cache_get(embed_url)
    if hit:
        return cached
    result = _resolve_mp4_uncached(embed_url)
    _cache_put(embed_url, result)
    return result


def _resolve_mp4_uncached(embed_url: str) -> str | None:
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
# The API answers on both domains; mega.co.nz is an old alias that DNS
# filters often forget to block.
_MEGA_API_HOSTS = ("g.api.mega.nz", "g.api.mega.co.nz")
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


# ---------------------------------------------------------------------------
# DNS-resilient HTTP layer. Some hosts (e.g. Railway) refuse to resolve
# mega.nz domains at the DNS level, so we fall back to resolving the host
# ourselves — via DNS-over-HTTPS or a raw UDP query to public resolvers —
# and then connect to the IP directly while keeping TLS SNI + certificate
# verification for the original hostname.
# ---------------------------------------------------------------------------
_DNS_CACHE: dict[str, str] = {}

# Last-resort hardcoded IPs (Mega's own ranges, stable for years) for hosts
# whose DNS is aggressively filtered; tried only when every resolver fails.
_STATIC_IPS: dict[str, list[str]] = {
    "g.api.mega.nz": ["66.203.125.11", "66.203.125.12", "66.203.125.13"],
    "g.api.mega.co.nz": ["66.203.125.11", "66.203.125.12", "66.203.125.13"],
}


def _doh_json_endpoints() -> list[tuple[str, dict]]:
    """dns-json compatible DoH resolvers (Google/Cloudflare are commonly
    blocked by filtering networks, so include less-known ones too)."""
    return [
        ("https://dns.google/resolve", {}),
        ("https://cloudflare-dns.com/dns-query", {"accept": "application/dns-json"}),
        ("https://dns.alidns.com/resolve", {"accept": "application/dns-json"}),
        ("https://doh.pub/resolve", {"accept": "application/dns-json"}),
        ("https://dns.adguard-dns.com/resolve", {"accept": "application/dns-json"}),
        ("https://dns.quad9.net/dns-query", {"accept": "application/dns-json"}),
        ("https://doh.opendns.com/dns-query", {"accept": "application/dns-json"}),
    ]


def _ips_from_dns_json(data: dict) -> list[str]:
    return [a["data"] for a in data.get("Answer") or [] if a.get("type") == 1]


def _hackertarget_resolve(host: str) -> list[str]:
    r = requests.get(f"https://api.hackertarget.com/dnslookup/?q={host}", timeout=10)
    ips = []
    for line in r.text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-2] == "A":
            ips.append(parts[-1])
    return ips


def _checkhost_resolve(host: str) -> list[str]:
    r = requests.get(
        f"https://check-host.net/check-dns?host={host}",
        headers={"Accept": "application/json"},
        timeout=10,
    )
    ips = []
    for node in (r.json() or {}).values():
        if isinstance(node, dict):
            ips.extend(node.get("A") or [])
    return ips


def _doh_resolve(host: str) -> str | None:
    """Resolve host via public DNS-over-HTTPS / DNS-lookup APIs."""
    for url, headers in _doh_json_endpoints():
        try:
            r = requests.get(
                url, params={"name": host, "type": "A"}, headers=headers, timeout=10
            )
            ips = _ips_from_dns_json(r.json())
            if ips:
                log.info("resolved %s via %s -> %s", host, url, ips[0])
                return ips[0]
        except Exception as exc:
            log.info("DoH %s failed for %s: %s", url, host, exc)
    for name, fn in (("hackertarget", _hackertarget_resolve), ("check-host", _checkhost_resolve)):
        try:
            ips = fn(host)
            if ips:
                log.info("resolved %s via %s -> %s", host, name, ips[0])
                return ips[0]
        except Exception as exc:
            log.info("%s lookup failed for %s: %s", name, host, exc)
    return None


def _udp_resolve(host: str, server: str = "8.8.8.8", timeout: float = 5.0) -> str | None:
    """Minimal raw UDP DNS A-record query (bypasses a filtering resolver)."""
    try:
        tid = random.randint(0, 0xFFFF)
        q = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
        for part in host.split("."):
            q += bytes([len(part)]) + part.encode()
        q += b"\x00" + struct.pack(">HH", 1, 1)  # type A, class IN
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(q, (server, 53))
            data, _ = sock.recvfrom(512)
        if len(data) < 12:
            return None
        ancount = struct.unpack(">H", data[6:8])[0]
        i = 12
        while data[i] != 0:  # skip question name
            i += data[i] + 1
        i += 5  # null byte + qtype + qclass
        for _ in range(ancount):
            if data[i] & 0xC0 == 0xC0:  # compressed name pointer
                i += 2
            else:
                while data[i] != 0:
                    i += data[i] + 1
                i += 1
            rtype, _rclass, _ttl, rdlen = struct.unpack(">HHIH", data[i : i + 10])
            i += 10
            if rtype == 1 and rdlen == 4:
                return socket.inet_ntoa(data[i : i + 4])
            i += rdlen
    except Exception as exc:
        log.info("UDP DNS %s failed for %s: %s", server, host, exc)
    return None


def _resolve_host(host: str) -> str:
    """Resolve `host` to an IPv4 address, bypassing broken/filtering DNS."""
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    try:
        ip = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)[0][4][0]
    except socket.gaierror:
        ip = None
    if not ip:
        log.info("system DNS failed for %s — trying DoH/UDP fallbacks", host)
        ip = _doh_resolve(host) or _udp_resolve(host) or _udp_resolve(host, "1.1.1.1")
    if not ip and host in _STATIC_IPS:
        ip = random.choice(_STATIC_IPS[host])
        log.info("using static fallback IP for %s -> %s", host, ip)
    if not ip:
        raise RuntimeError(f"cannot resolve {host}")
    _DNS_CACHE[host] = ip
    return ip


class _SniIpAdapter(HTTPAdapter):
    """Connect to a fixed IP while keeping TLS SNI and certificate
    hostname verification for the original domain."""

    def __init__(self, host: str, ip: str):
        self._host = host
        self._ip = ip
        super().__init__()

    def _ip_pool(self):
        return self.poolmanager.connection_from_host(
            self._ip,
            port=443,
            scheme="https",
            pool_kwargs={"assert_hostname": self._host, "server_hostname": self._host},
        )

    def get_connection(self, url, proxies=None):  # requests < 2.32
        return self._ip_pool()

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        # requests >= 2.32 — send() calls THIS, not get_connection
        return self._ip_pool()


def _http_for(host: str) -> "requests.Session | requests":
    """Return an HTTP client able to reach `host` even when the local
    resolver refuses it (connects to a self-resolved IP with proper SNI)."""
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return requests
    except socket.gaierror:
        ip = _resolve_host(host)
        s = requests.Session()
        s.mount("https://", _SniIpAdapter(host, ip))
        return s


def _mega_api(payload: dict) -> dict:
    """Single Mega API call over the first reachable API host; raises on
    total failure."""
    last_exc: Exception | None = None
    for host in _MEGA_API_HOSTS:
        try:
            http = _http_for(host)
            r = http.post(
                f"https://{host}/cs",
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
                raise RuntimeError(
                    f"mega api error {item}: {_MEGA_ERRORS.get(item, 'unknown')}"
                )
            if "g" not in item:
                raise RuntimeError("mega: file not accessible")
            return item
        except Exception as exc:  # try the next API host
            last_exc = exc
            log.info("mega api via %s failed: %s", host, exc)
    raise RuntimeError(f"mega api unreachable: {last_exc}")


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


def download_mega(
    url: str,
    path: str,
    state: dict | None = None,
    cancel=None,
) -> int:
    """Blocking download+decrypt of a public Mega file to `path`.
    `state["done"]`/`state["total"]` are updated for progress reporting.
    `cancel`: optional callable checked before every chunk; when it returns
    truthy, DownloadCancelled is raised.
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
    dl_host = urlparse(item["g"]).hostname
    http = _http_for(dl_host) if dl_host else requests
    if cancel is not None and cancel():
        raise DownloadCancelled("mega download cancelled")
    with http.get(item["g"], stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 2048):
                if cancel is not None and cancel():
                    raise DownloadCancelled("mega download cancelled")
                f.write(aes.decrypt(chunk))
                written += len(chunk)
                if state is not None:
                    state["done"] = written
    return written
