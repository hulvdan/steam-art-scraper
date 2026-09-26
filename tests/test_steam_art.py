from datetime import datetime
from pathlib import Path

import httpx
import pytest

from scraper import (
    Artwork,
    ScrapedApp,
    artwork_from_item,
    download_artwork,
    import_artwork,
    parse_app_id,
    remove_partials,
)

CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (367520, 367520),
        ("367520", 367520),
        (" 367520 ", 367520),
        ("https://store.steampowered.com/app/367520/Hollow_Knight/", 367520),
        ("store.steampowered.com/app/367520", 367520),
    ],
)
def test_parse_app_id(value: str | int, expected: int) -> None:
    assert parse_app_id(value) == expected


def test_parse_app_id_invalid() -> None:
    with pytest.raises(ValueError):
        parse_app_id("https://store.steampowered.com/")


def test_artwork_from_item() -> None:
    item = {
        "assets": {
            "asset_url_format": "steam/apps/367520/${FILENAME}?t=1",
            "header": "abc/header.jpg",
            "library_hero_2x": "def/library_hero_2x.jpg",
            "community_icon": "f6ab",
            "page_background_path": "app/367520?t=1",
        },
        "screenshots": {
            "all_ages_screenshots": [
                {"filename": "steam/apps/367520/ss_b.jpg?t=1", "ordinal": 2},
                {"filename": "steam/apps/367520/ss_a.jpg?t=1", "ordinal": 1},
            ]
        },
    }
    arts = {a.name: a.url for a in artwork_from_item(367520, item)}
    assert arts == {
        "header": f"{CDN}steam/apps/367520/abc/header.jpg?t=1",
        "library_hero_2x": f"{CDN}steam/apps/367520/def/library_hero_2x.jpg?t=1",
        "page_background": "https://store.akamai.steamstatic.com/images/storepagebackground/app/367520?t=1",
        "community_icon": "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/367520/f6ab.jpg",
        "screenshot_01": f"{CDN}steam/apps/367520/ss_a.jpg?t=1",
        "screenshot_02": f"{CDN}steam/apps/367520/ss_b.jpg?t=1",
    }


_AT = datetime(2026, 9, 26, 13, 5, 2)


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("Hollow_Knight", "20260926 130502 - Hollow_Knight"),
        ("Bad:Name/With*Chars?.", "20260926 130502 - Bad_Name_With_Chars_"),
        ("", "20260926 130502 - 367520"),
    ],
)
def test_dir_name(slug: str, expected: str) -> None:
    assert ScrapedApp(367520, "", slug, []).dir_name(_AT) == expected


def test_download_artwork_skips_404(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("missing.jpg"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"img", headers={"content-type": "image/jpeg"})

    arts = [
        Artwork("ok", "https://cdn.test/ok.jpg"),
        Artwork("gone", "https://cdn.test/missing.jpg"),
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        paths = download_artwork(arts, tmp_path, client)
    assert paths == [tmp_path / "ok.jpg"]
    assert (tmp_path / "ok.jpg").read_bytes() == b"img"


def test_import_artwork_renames_on_success(tmp_path: Path) -> None:
    arts = [Artwork("ok", "https://cdn.test/ok.jpg")]
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=b"img"))
    with httpx.Client(transport=transport) as client:
        paths = import_artwork(arts, tmp_path / "game", client)
    assert paths == [tmp_path / "game" / "ok.jpg"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["game"]


def test_import_artwork_leaves_nothing_on_failure(tmp_path: Path) -> None:
    arts = [Artwork("ok", "https://cdn.test/ok.jpg"), Artwork("bad", "https://cdn.test/bad.jpg")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if "bad" in request.url.path else 200, content=b"img")

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        import_artwork(arts, tmp_path / "game", client)
    assert list(tmp_path.iterdir()) == []


def test_remove_partials(tmp_path: Path) -> None:
    (tmp_path / "a.partial").mkdir()
    (tmp_path / "a.partial" / "x.jpg").write_bytes(b"")
    (tmp_path / "20260101 000000 - B").mkdir()
    remove_partials(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["20260101 000000 - B"]
