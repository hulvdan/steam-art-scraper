import os
from datetime import datetime
from pathlib import Path

import httpx
import pytest

from scraper import (
    Artwork,
    Reviews,
    ScrapedApp,
    TrailerError,
    TrailerStream,
    artwork_from_item,
    download_artwork,
    download_trailer,
    group_by_asset,
    import_artwork,
    parse_app_id,
    pick_trailer_streams,
    remove_partials,
    reviews_from_item,
    trailer_from_item,
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
            "library_hero": "def/library_hero.jpg",
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
        "library_hero": f"{CDN}steam/apps/367520/def/library_hero_2x.jpg?t=1",
        "page_background": "https://store.akamai.steamstatic.com/images/storepagebackground/app/367520?t=1",
        "community_icon": "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/367520/f6ab.jpg",
        "screenshot_01": f"{CDN}steam/apps/367520/ss_a.jpg?t=1",
        "screenshot_02": f"{CDN}steam/apps/367520/ss_b.jpg?t=1",
    }


def _trailer(base_id: int, path: str) -> dict[str, object]:
    return {
        "trailer_base_id": base_id,
        "adaptive_trailers": [
            {"cdn_path": f"{path}/dash_av1.mpd", "encoding": "dash_av1"},
            {"cdn_path": f"{path}/dash_h264.mpd", "encoding": "dash_h264"},
        ],
    }


def test_trailer_from_item_picks_newest() -> None:
    item = {
        "trailers": {
            "highlights": [_trailer(961743, "1/new"), _trailer(116737, "1/old")],
            "other_trailers": [_trailer(209267, "1/mid")],
        }
    }
    assert trailer_from_item(item) == (
        "https://video.akamai.steamstatic.com/store_trailers/1/new/dash_h264.mpd"
    )


def test_trailer_from_item_none() -> None:
    assert trailer_from_item({}) is None
    assert trailer_from_item({"trailers": {"highlights": [{"trailer_base_id": 1}]}}) is None


_MPD_URL = "https://video.test/t/dash_h264.mpd"


def _mpd(frame_rate: str, *heights: int) -> str:
    template = (
        '<SegmentTemplate timescale="1000000" duration="3000000" startNumber="1"'
        ' initialization="d/init-$RepresentationID$.m4s"'
        ' media="d/chunk-$RepresentationID$-$Number%05d$.m4s"/>'
    )
    videos = "".join(
        f'<Representation id="{i}" bandwidth="{h * 1000}" height="{h}">{template}</Representation>'
        for i, h in enumerate(heights)
    )
    return f"""<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT6.1S">
  <Period>
    <AdaptationSet contentType="video" frameRate="{frame_rate}">{videos}</AdaptationSet>
    <AdaptationSet contentType="audio">
      <Representation id="a" bandwidth="128000">{template}</Representation>
    </AdaptationSet>
    <AdaptationSet contentType="image"><Representation id="thumbs" bandwidth="0"/></AdaptationSet>
  </Period>
</MPD>"""


def test_pick_trailer_streams() -> None:
    streams = pick_trailer_streams(_MPD_URL, _mpd("60000/1001", 2160, 1080, 720))
    assert streams == [
        TrailerStream(
            "https://video.test/t/d/init-1.m4s",
            [f"https://video.test/t/d/chunk-1-0000{n}.m4s" for n in (1, 2, 3)],
        ),
        TrailerStream(
            "https://video.test/t/d/init-a.m4s",
            [f"https://video.test/t/d/chunk-a-0000{n}.m4s" for n in (1, 2, 3)],
        ),
    ]


@pytest.mark.parametrize("frame_rate", ["120/1", ""])
def test_pick_trailer_streams_rejects_high_or_unknown_fps(frame_rate: str) -> None:
    assert pick_trailer_streams(_MPD_URL, _mpd(frame_rate, 1080)) == []


def test_pick_trailer_streams_malformed() -> None:
    with pytest.raises(TrailerError):
        pick_trailer_streams(_MPD_URL, "<MPD")


def test_download_trailer_skips_unsuitable(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == _MPD_URL
        return httpx.Response(200, text=_mpd("30/1", 2160))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert download_trailer(_MPD_URL, tmp_path, client) is None
    assert list(tmp_path.iterdir()) == []


_AT = datetime(2026, 9, 26, 13, 5, 2)


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("Hollow_Knight", "20260926 130502 - Hollow_Knight (1234, 85%)"),
        ("Bad:Name/With*Chars?.", "20260926 130502 - Bad_Name_With_Chars_ (1234, 85%)"),
        ("", "20260926 130502 - 367520 (1234, 85%)"),
    ],
)
def test_dir_name(slug: str, expected: str) -> None:
    assert ScrapedApp(367520, "", slug, [], Reviews(1234, 85)).dir_name(_AT) == expected


@pytest.mark.parametrize(
    ("reviews", "expected"),
    [(Reviews(1234, 85), "1234, 85%"), (Reviews(3, 100), "3, 100%"), (Reviews(0, 0), "0")],
)
def test_reviews_str(reviews: Reviews, expected: str) -> None:
    assert str(reviews) == expected


def test_reviews_from_item() -> None:
    item = {
        "reviews": {
            "summary_filtered": {"review_count": 507705, "percent_positive": 96},
            "summary_language_specific": {"review_count": 185835, "percent_positive": 97},
        }
    }
    assert reviews_from_item(item) == Reviews(507705, 96)
    assert reviews_from_item({}) == Reviews(0, 0)


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


def test_group_by_asset(tmp_path: Path) -> None:
    for game in ("20260101 000000 - A", "20260101 000001 - B", "C.partial"):
        (tmp_path / game).mkdir()
        (tmp_path / game / "logo.png").write_bytes(game.encode())
        (tmp_path / game / "screenshot_01.jpg").write_bytes(b"")
    (tmp_path / "20260101 000000 - A" / "header.jpg").write_bytes(b"h")
    (tmp_path / "_logo").mkdir()
    (tmp_path / "_logo" / "stale.png").write_bytes(b"")

    group_by_asset(tmp_path)
    group_by_asset(tmp_path)

    assert sorted(p.name for p in tmp_path.glob("_*")) == ["_header", "_logo"]
    assert not list(tmp_path.glob("_screenshot*"))
    logos = {p.name: p.read_bytes() for p in (tmp_path / "_logo").iterdir()}
    assert logos == {
        "20260101 000000 - A.png": b"20260101 000000 - A",
        "20260101 000001 - B.png": b"20260101 000001 - B",
    }
    assert (tmp_path / "_header" / "20260101 000000 - A.jpg").read_bytes() == b"h"
    logo_a = tmp_path / "_logo" / "20260101 000000 - A.png"
    assert os.path.samefile(logo_a, tmp_path / "20260101 000000 - A" / "logo.png")
