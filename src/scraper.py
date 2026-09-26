"""Скачивание арта Steam-приложения (капсулы, хедер, хиро, лого, скриншоты и т.д.).

Видео и трейлеры намеренно исключены.
"""

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
_ASSET_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"
_PAGE_BACKGROUND_CDN = "https://store.akamai.steamstatic.com/images/storepagebackground/"
_COMMUNITY_ICON_CDN = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"

# Ключи `assets` в ответе GetItems, содержащие имена файлов картинок.
_ASSET_KEYS = (
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

# Старые пути без хеша, которых нет в GetItems; проверяются и берутся, только если существуют.
_PROBED_ASSETS = {
    "logo": "logo.png",
    "logo_2x": "logo_2x.png",
}

_APP_URL_RE = re.compile(r"/app/(\d+)")
# Символы, недопустимые в именах файлов Windows.
_UNSAFE_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

_CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


@dataclass(frozen=True)
class Artwork:
    name: str
    url: str


@dataclass(frozen=True)
class ScrapedApp:
    app_id: int
    name: str
    slug: str
    artworks: list[Artwork]

    def dir_name(self, at: datetime) -> str:
        """Имя папки вида `20260926 130512 - Hollow_Knight` (время импорта)."""
        slug = _UNSAFE_PATH_CHARS_RE.sub("_", self.slug).strip(" .") or str(self.app_id)
        return f"{at:%Y%m%d %H%M%S} - {slug}"


class SteamAppNotFound(LookupError):
    pass


def parse_app_id(app: str | int) -> int:
    """Принимает app id (int или строку из цифр) или URL магазина вида .../app/367520/Name/."""
    if isinstance(app, int):
        return app
    app = app.strip()
    if app.isdigit():
        return int(app)
    if match := _APP_URL_RE.search(app):
        return int(match.group(1))
    raise ValueError(f"Cannot parse Steam app id from {app!r}")


def _fetch_store_item(app_id: int, client: httpx.Client) -> dict[str, Any]:
    input_json = {
        "ids": [{"appid": app_id}],
        "context": {"language": "english", "country_code": "US"},
        "data_request": {"include_assets": True, "include_screenshots": True},
    }
    resp = client.get(_STORE_BROWSE_URL, params={"input_json": json.dumps(input_json)})
    resp.raise_for_status()
    items = resp.json().get("response", {}).get("store_items", [])
    if not items or items[0].get("success") != 1:
        raise SteamAppNotFound(f"Steam app {app_id} not found")
    return items[0]


def artwork_from_item(app_id: int, item: dict[str, Any]) -> list[Artwork]:
    """Собирает URL арта из элемента ответа GetItems. Чистая функция, без сети."""
    assets: dict[str, Any] = item.get("assets", {})
    url_format: str = assets.get("asset_url_format", f"steam/apps/{app_id}/${{FILENAME}}")

    artworks: list[Artwork] = []
    for key in _ASSET_KEYS:
        if filename := assets.get(key):
            artworks.append(Artwork(key, _ASSET_CDN + url_format.replace("${FILENAME}", filename)))

    if bg_path := assets.get("page_background_path"):
        artworks.append(Artwork("page_background", _PAGE_BACKGROUND_CDN + bg_path))

    if icon := assets.get("community_icon"):
        artworks.append(Artwork("community_icon", f"{_COMMUNITY_ICON_CDN}{app_id}/{icon}.jpg"))

    screenshots = item.get("screenshots", {}).get("all_ages_screenshots", [])
    shots = sorted(screenshots, key=lambda s: s.get("ordinal", 0))
    filenames = [f for shot in shots if (f := shot.get("filename"))]
    for i, filename in enumerate(filenames, start=1):
        artworks.append(Artwork(f"screenshot_{i:02d}", _ASSET_CDN + filename))

    return artworks


def _probe_legacy_assets(app_id: int, client: httpx.Client) -> list[Artwork]:
    found: list[Artwork] = []
    for name, filename in _PROBED_ASSETS.items():
        url = f"{_ASSET_CDN}steam/apps/{app_id}/{filename}"
        if client.head(url).status_code == 200:
            found.append(Artwork(name, url))
    return found


def scrape_artwork(app: str | int, client: httpx.Client | None = None) -> ScrapedApp:
    """Возвращает весь арт (без видео) для app id или URL магазина Steam."""
    app_id = parse_app_id(app)
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        item = _fetch_store_item(app_id, client)
        name: str = item.get("name", "")
        slug: str = item.get("store_url_slug") or name.replace(" ", "_")
        artworks = artwork_from_item(app_id, item) + _probe_legacy_assets(app_id, client)
        return ScrapedApp(app_id, name, slug, artworks)
    finally:
        if owns_client:
            client.close()


def _extension(url: str, content_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix
    if suffix:
        return suffix
    return _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip(), "")


def download_artwork(
    artworks: list[Artwork], dest: Path, client: httpx.Client | None = None
) -> list[Path]:
    """Скачивает арт в `dest`. Отсутствующие на CDN файлы (404) пропускаются."""
    dest.mkdir(parents=True, exist_ok=True)
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    paths: list[Path] = []
    try:
        for art in artworks:
            resp = client.get(art.url)
            if resp.status_code == httpx.codes.NOT_FOUND:
                continue
            resp.raise_for_status()
            path = dest / (art.name + _extension(art.url, resp.headers.get("content-type", "")))
            path.write_bytes(resp.content)
            paths.append(path)
    finally:
        if owns_client:
            client.close()
    return paths
