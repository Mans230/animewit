import importlib
import json
import os


def test_touch_user_and_persist_roundtrip(tmp_store, tmp_path):
    u = tmp_store.touch_user(42, name="Ahmed", username="ahmed")
    assert u["name"] == "Ahmed"
    assert u["username"] == "ahmed"
    assert u["banned"] is False
    assert u["favorites"] == [] and u["follows"] == {}
    assert u["first_seen"] > 0 and u["last_seen"] >= u["first_seen"]

    # reload against the same DATA_DIR -> data must survive (persisted)
    importlib.reload(tmp_store)
    u2 = tmp_store.touch_user(42)
    assert u2["name"] == "Ahmed"
    assert os.path.exists(tmp_store.STORE_PATH)


def test_store_file_is_valid_json_after_save(tmp_store):
    tmp_store.touch_user(1, name="x")
    with open(tmp_store.STORE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert "users" in data and "1" in data["users"]


def test_favorites_toggle_both_ways(tmp_store):
    assert tmp_store.get_favorites(7) == []
    assert tmp_store.toggle_favorite(7, "https://x/anime/a", "Anime A") is True
    assert tmp_store.toggle_favorite(7, "https://x/anime/b", "Anime B") is True
    favs = tmp_store.get_favorites(7)
    assert [f["url"] for f in favs] == ["https://x/anime/a", "https://x/anime/b"]
    assert favs[0]["title"] == "Anime A"
    assert tmp_store.toggle_favorite(7, "https://x/anime/a", "Anime A") is False
    assert [f["url"] for f in tmp_store.get_favorites(7)] == ["https://x/anime/b"]


def test_follows_set_remove_index_update(tmp_store):
    assert tmp_store.set_follow(1, "https://x/anime/a", "A", 10, "https://x/ep/10") is True
    assert tmp_store.set_follow(1, "https://x/anime/a", "A", 10, "https://x/ep/10") is False
    assert tmp_store.set_follow(2, "https://x/anime/a", "A", 10, "https://x/ep/10") is True
    assert tmp_store.set_follow(2, "https://x/anime/b", "B", 3, "https://x/ep/3") is True

    follows = tmp_store.get_follows(1)
    assert follows["https://x/anime/a"]["ep_count"] == 10
    assert follows["https://x/anime/a"]["last_ep"] == "https://x/ep/10"

    index = tmp_store.follows_index()
    assert sorted(index["https://x/anime/a"]) == [1, 2]
    assert index["https://x/anime/b"] == [2]

    tmp_store.update_follow(1, "https://x/anime/a", 11, "https://x/ep/11")
    assert tmp_store.get_follows(1)["https://x/anime/a"]["ep_count"] == 11
    assert tmp_store.get_follows(1)["https://x/anime/a"]["last_ep"] == "https://x/ep/11"
    # updating a non-follow is a no-op
    tmp_store.update_follow(1, "https://x/anime/zzz", 1, "e")
    assert "https://x/anime/zzz" not in tmp_store.get_follows(1)

    assert tmp_store.remove_follow(2, "https://x/anime/a") is True
    assert tmp_store.remove_follow(2, "https://x/anime/a") is False
    assert tmp_store.follows_index()["https://x/anime/a"] == [1]


def test_ban(tmp_store):
    assert tmp_store.is_banned(9) is False
    tmp_store.touch_user(9)
    assert tmp_store.is_banned(9) is False
    tmp_store.set_banned(9, True)
    assert tmp_store.is_banned(9) is True
    tmp_store.set_banned(9, False)
    assert tmp_store.is_banned(9) is False


def test_stats_counts(tmp_store):
    tmp_store.touch_user(1)
    tmp_store.touch_user(2)
    tmp_store.set_banned(2, True)
    tmp_store.toggle_favorite(1, "https://x/a", "A")
    tmp_store.toggle_favorite(2, "https://x/b", "B")
    tmp_store.set_follow(1, "https://x/a", "A", 5, "e5")
    tmp_store.incr_videos_sent(1)
    tmp_store.incr_videos_sent(1, 2)
    tmp_store.incr_batches(2)

    stats = tmp_store.get_stats()
    assert stats == {
        "users": 2,
        "banned": 1,
        "follows": 1,
        "favorites": 2,
        "videos_sent": 3,
        "batches": 1,
    }


def test_all_user_ids(tmp_store):
    tmp_store.touch_user(3)
    tmp_store.touch_user(1)
    tmp_store.touch_user(2)
    assert sorted(tmp_store.all_user_ids()) == [1, 2, 3]
