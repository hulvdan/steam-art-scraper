import pytest

from steam_art import ASSET_CDN, artwork_from_item, parse_app_id


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
        "appid": 367520,
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
    arts = {a.name: a.url for a in artwork_from_item(item)}
    assert arts == {
        "header": f"{ASSET_CDN}steam/apps/367520/abc/header.jpg?t=1",
        "library_hero_2x": f"{ASSET_CDN}steam/apps/367520/def/library_hero_2x.jpg?t=1",
        "page_background": "https://store.akamai.steamstatic.com/images/storepagebackground/app/367520?t=1",
        "community_icon": "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/367520/f6ab.jpg",
        "screenshot_01": f"{ASSET_CDN}steam/apps/367520/ss_a.jpg?t=1",
        "screenshot_02": f"{ASSET_CDN}steam/apps/367520/ss_b.jpg?t=1",
    }
