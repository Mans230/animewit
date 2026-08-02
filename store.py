"""JSON persistence layer for animewit bot (SPEC §Module: store.py).

Atomic writes (tmp file + os.replace), a single threading.RLock guards every
read/write, and the DB is lazily auto-loaded from disk on first access.

DB shape:
    {"users": {"<uid>": {"name", "username", "first_seen", "last_seen",
        "banned": bool,
        "favorites": [{"url", "title"}],
        "follows": {"<anime_url>": {"title", "ep_count": int, "last_ep": str}},
        "videos_sent": int, "batches": int}}}

DATA_DIR is read from the environment at import time (tests reload the module
with a patched env); defaults to tempfile.gettempdir()/witanime-bot-data.
"""

import copy
import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger(__name__)

DATA_DIR = os.environ.get("DATA_DIR") or os.path.join(
    tempfile.gettempdir(), "witanime-bot-data"
)
STORE_PATH = os.path.join(DATA_DIR, "store.json")

_lock = threading.RLock()
_db: dict | None = None  # lazy-loaded


def _now() -> float:
    return time.time()


def _new_user(name: str = "", username: str = "") -> dict:
    return {
        "name": name,
        "username": username,
        "first_seen": _now(),
        "last_seen": _now(),
        "banned": False,
        "favorites": [],
        "follows": {},
        "videos_sent": 0,
        "batches": 0,
    }


def _load_locked() -> dict:
    """Load the DB from disk if not loaded yet. Caller must hold the lock."""
    global _db
    if _db is not None:
        return _db
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
            raise ValueError("unexpected store.json shape")
        _db = data
    except FileNotFoundError:
        _db = {"users": {}}
    except Exception as exc:  # corrupt file -> start clean, keep bot alive
        log.warning("store load failed (%s) — starting with an empty DB", exc)
        _db = {"users": {}}
    return _db


def load_store() -> dict:
    """Public loader (idempotent)."""
    with _lock:
        return _load_locked()


def save_store() -> None:
    """Atomically persist the DB (tmp file + rename). Caller may hold the lock."""
    with _lock:
        _load_locked()
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp_path = STORE_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(_db, f, ensure_ascii=False)
        os.replace(tmp_path, STORE_PATH)


def _user_locked(user_id: int) -> dict:
    """Return the user record, creating it if missing. Caller holds the lock."""
    users = _load_locked()["users"]
    key = str(user_id)
    u = users.get(key)
    if u is None:
        u = _new_user()
        users[key] = u
    return u


def _find_user_locked(user_id: int) -> dict | None:
    return _load_locked()["users"].get(str(user_id))


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def touch_user(user_id: int, name: str = "", username: str = "") -> dict:
    """Create-or-update the user record (bumps last_seen). Returns a copy."""
    with _lock:
        u = _user_locked(user_id)
        u["last_seen"] = _now()
        if name:
            u["name"] = name
        if username:
            u["username"] = username
        save_store()
        return copy.deepcopy(u)


def is_banned(user_id: int) -> bool:
    with _lock:
        u = _find_user_locked(user_id)
        return bool(u and u.get("banned"))


def set_banned(user_id: int, banned: bool) -> None:
    with _lock:
        u = _user_locked(user_id)
        u["banned"] = bool(banned)
        save_store()


def all_user_ids() -> list[int]:
    with _lock:
        ids = []
        for key in _load_locked()["users"]:
            try:
                ids.append(int(key))
            except (TypeError, ValueError):
                continue
        return ids


def get_stats() -> dict:
    """-> {"users","banned","follows","favorites","videos_sent","batches"}"""
    with _lock:
        users = list(_load_locked()["users"].values())
        return {
            "users": len(users),
            "banned": sum(1 for u in users if u.get("banned")),
            "follows": sum(len(u.get("follows") or {}) for u in users),
            "favorites": sum(len(u.get("favorites") or []) for u in users),
            "videos_sent": sum(int(u.get("videos_sent") or 0) for u in users),
            "batches": sum(int(u.get("batches") or 0) for u in users),
        }


# ---------------------------------------------------------------------------
# favorites
# ---------------------------------------------------------------------------

def toggle_favorite(user_id: int, anime_url: str, title: str) -> bool:
    """Add the anime if absent, remove it if present. True=added, False=removed."""
    with _lock:
        u = _user_locked(user_id)
        favs = u.setdefault("favorites", [])
        for i, fav in enumerate(favs):
            if fav.get("url") == anime_url:
                del favs[i]
                save_store()
                return False
        favs.append({"url": anime_url, "title": title})
        save_store()
        return True


def get_favorites(user_id: int) -> list[dict]:
    with _lock:
        u = _find_user_locked(user_id)
        if not u:
            return []
        return copy.deepcopy(u.get("favorites") or [])


# ---------------------------------------------------------------------------
# follows (new-episode notifications)
# ---------------------------------------------------------------------------

def set_follow(user_id: int, anime_url: str, title: str, ep_count: int, last_ep: str) -> bool:
    """Start following an anime. False=already followed."""
    with _lock:
        u = _user_locked(user_id)
        follows = u.setdefault("follows", {})
        if anime_url in follows:
            return False
        follows[anime_url] = {
            "title": title,
            "ep_count": int(ep_count),
            "last_ep": last_ep,
        }
        save_store()
        return True


def remove_follow(user_id: int, anime_url: str) -> bool:
    with _lock:
        u = _find_user_locked(user_id)
        follows = (u or {}).get("follows") or {}
        if anime_url not in follows:
            return False
        del follows[anime_url]
        save_store()
        return True


def get_follows(user_id: int) -> dict:
    with _lock:
        u = _find_user_locked(user_id)
        if not u:
            return {}
        return copy.deepcopy(u.get("follows") or {})


def follows_index() -> dict[str, list[int]]:
    """anime_url -> [user_id, ...] across all users (for the follow checker)."""
    with _lock:
        index: dict[str, list[int]] = {}
        for key, u in _load_locked()["users"].items():
            try:
                uid = int(key)
            except (TypeError, ValueError):
                continue
            for anime_url in (u.get("follows") or {}):
                index.setdefault(anime_url, []).append(uid)
        return index


def update_follow(user_id: int, anime_url: str, ep_count: int, last_ep: str) -> None:
    """Update counters of an existing follow (no-op if not followed)."""
    with _lock:
        u = _find_user_locked(user_id)
        follow = ((u or {}).get("follows") or {}).get(anime_url)
        if follow is None:
            return
        follow["ep_count"] = int(ep_count)
        follow["last_ep"] = last_ep
        save_store()


# ---------------------------------------------------------------------------
# counters
# ---------------------------------------------------------------------------

def incr_videos_sent(user_id: int, n: int = 1) -> None:
    with _lock:
        u = _user_locked(user_id)
        u["videos_sent"] = int(u.get("videos_sent") or 0) + int(n)
        save_store()


def incr_batches(user_id: int) -> None:
    with _lock:
        u = _user_locked(user_id)
        u["batches"] = int(u.get("batches") or 0) + 1
        save_store()
