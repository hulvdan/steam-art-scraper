"""Scrape artwork (capsules, header, hero, logo, screenshots, ...) for a Steam app.

Videos/trailers are intentionally excluded.
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
ASSET_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"
PAGE_BACKGROUND_CDN = "https://store.akamai.steamstatic.com/images/storepagebackground/"
COMMUNITY_ICON_CDN = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"

# Keys of `assets` in the GetItems response that hold image filenames.
ASSET_KEYS = (
    "header",
    "header_2x",
    "main_capsule",
    "main_capsule_2x",
    "small_capsule",
    "small_capsule_2x",
    "hero_capsule",
    "hero_capsule_2x",
    "library_capsule",
    "library_capsule_2x",
    "library_hero",
    "library_hero_2x",
)

# Legacy unhashed paths not exposed by GetItems; probed and kept only if they exist.
PROBED_ASSETS = {
    "logo": "logo.png",
    "logo_2x": "logo_2x.png",
}

_APP_URL_RE = re.compile(r"/app/(\d+)")

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class Artwork:
    name: str
    url: str


class SteamAppNotFound(LookupError):
    pass


def parse_app_id(app: str | int) -> int:
    """Accept an app id (int or digit string) or a store URL like .../app/367520/Name/."""
    if isinstance(app, int):
        return app
    app = app.strip()
    if app.isdigit():
        return int(app)
    if match := _APP_URL_RE.search(app):
        return int(match.group(1))
    raise ValueError(f"Cannot parse Steam app id from {app!r}")


def fetch_store_item(app_id: int, client: httpx.Client) -> dict[str, Any]:
    input_json = {
        "ids": [{"appid": app_id}],
        "context": {"language": "english", "country_code": "US"},
        "data_request": {"include_assets": True, "include_screenshots": True},
    }
    resp = client.get(STORE_BROWSE_URL, params={"input_json": json.dumps(input_json)})
    resp.raise_for_status()
    items = resp.json().get("response", {}).get("store_items", [])
    if not items or items[0].get("success") != 1:
        raise SteamAppNotFound(f"Steam app {app_id} not found")
    return items[0]


def artwork_from_item(item: dict[str, Any]) -> list[Artwork]:
    """Build artwork URLs from a GetItems store item. Pure, no network."""
    app_id = item["appid"]
    assets: dict[str, Any] = item.get("assets", {})
    url_format: str = assets.get("asset_url_format", f"steam/apps/{app_id}/${{FILENAME}}")

    artworks: list[Artwork] = []
    for key in ASSET_KEYS:
        if filename := assets.get(key):
            artworks.append(Artwork(key, ASSET_CDN + url_format.replace("${FILENAME}", filename)))

    if bg_path := assets.get("page_background_path"):
        artworks.append(Artwork("page_background", PAGE_BACKGROUND_CDN + bg_path))

    if icon := assets.get("community_icon"):
        artworks.append(Artwork("community_icon", f"{COMMUNITY_ICON_CDN}{app_id}/{icon}.jpg"))

    screenshots = item.get("screenshots", {}).get("all_ages_screenshots", [])
    for shot in sorted(screenshots, key=lambda s: s.get("ordinal", 0)):
        artworks.append(Artwork(f"screenshot_{shot['ordinal']:02d}", ASSET_CDN + shot["filename"]))

    return artworks


def probe_legacy_assets(app_id: int, client: httpx.Client) -> list[Artwork]:
    found: list[Artwork] = []
    for name, filename in PROBED_ASSETS.items():
        url = f"{ASSET_CDN}steam/apps/{app_id}/{filename}"
        if client.head(url).status_code == 200:
            found.append(Artwork(name, url))
    return found


def scrape_artwork(app: str | int, client: httpx.Client | None = None) -> list[Artwork]:
    """Return all artwork (no videos) for a Steam app id or store URL."""
    app_id = parse_app_id(app)
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        item = fetch_store_item(app_id, client)
        return artwork_from_item(item) + probe_legacy_assets(app_id, client)
    finally:
        if owns_client:
            client.close()


def _extension(url: str, content_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix
    if suffix:
        return suffix
    return CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip(), "")


def download_artwork(
    artworks: list[Artwork], dest: Path, client: httpx.Client | None = None
) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    paths: list[Path] = []
    try:
        for art in artworks:
            resp = client.get(art.url)
            resp.raise_for_status()
            path = dest / (art.name + _extension(art.url, resp.headers.get("content-type", "")))
            path.write_bytes(resp.content)
            paths.append(path)
    finally:
        if owns_client:
            client.close()
    return paths
