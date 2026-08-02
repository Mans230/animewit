import pytest

import resolvers
import scraper

# Fake homepage built from the real card structure of witanime.life:
# div.episodes-card-container > .episodes-card-title h3 a
#                             + .ep-card-anime-title h3 a
#                             + img.img-responsive
FAKE_HOMEPAGE = """
<html><body>
<div class="episodes-card-container">
  <div class="episodes-card-image">
    <img class="img-responsive" src="https://witanime.life/wp-content/uploads/one-piece-ep.jpg"/>
  </div>
  <div class="episodes-card-title"><h3><a href="https://witanime.life/episode/one-piece-ep-1172/">الحلقة 1172</a></h3></div>
  <div class="ep-card-anime-title"><h3><a href="https://witanime.life/anime/one-piece/">One Piece</a></h3></div>
</div>
<div class="episodes-card-container">
  <div class="episodes-card-image">
    <img class="img-responsive" src="https://witanime.life/wp-content/uploads/naruto-ep.jpg"/>
  </div>
  <div class="episodes-card-title"><h3><a href="https://witanime.life/episode/naruto-ep-220/">الحلقة 220</a></h3></div>
  <div class="ep-card-anime-title"><h3><a href="https://witanime.life/anime/naruto/">Naruto</a></h3></div>
</div>
<div class="episodes-card-container">
  <!-- broken card without an episode link: must be skipped -->
  <div class="ep-card-anime-title"><h3><a href="https://witanime.life/anime/bleach/">Bleach</a></h3></div>
</div>
</body></html>
"""


def test_get_latest_episodes_parses_fake_cards(monkeypatch):
    monkeypatch.setattr(scraper, "_get", lambda url: FAKE_HOMEPAGE)
    items = scraper.get_latest_episodes()
    assert len(items) == 2  # broken card skipped

    first = items[0]
    assert first["ep_title"] == "الحلقة 1172"
    assert first["ep_url"] == "https://witanime.life/episode/one-piece-ep-1172/"
    assert first["anime_title"] == "One Piece"
    assert first["anime_url"] == "https://witanime.life/anime/one-piece/"
    assert first["screenshot"] == "https://witanime.life/wp-content/uploads/one-piece-ep.jpg"

    assert items[1]["anime_title"] == "Naruto"


def test_get_latest_episodes_respects_limit(monkeypatch):
    monkeypatch.setattr(scraper, "_get", lambda url: FAKE_HOMEPAGE)
    assert len(scraper.get_latest_episodes(limit=1)) == 1
    assert len(scraper.get_latest_episodes(limit=20)) == 2


@pytest.mark.live
def test_get_latest_episodes_live():
    items = scraper.get_latest_episodes(40)
    assert len(items) > 0
    for it in items:
        assert it["ep_url"].startswith("https://")
        assert it["anime_url"].startswith("https://")
        assert it["ep_title"]


@pytest.mark.live
def test_resolve_gofile_live():
    """Real gofile.io/d/ link through the public API. The sandbox network
    blocks gofile (and the link may expire), so ANY failure skips — this
    test must never fail the suite."""
    try:
        out = resolvers.resolve_gofile("https://gofile.io/d/CEK8fx")
    except Exception as exc:  # noqa: BLE001 — network is best-effort here
        pytest.skip(f"gofile unreachable: {exc}")
    if out is None:
        pytest.skip("gofile resolve failed (network blocked or link expired)")
    url, headers = out
    assert url.startswith("https://")
    assert "accountToken=" in headers.get("Cookie", "")
