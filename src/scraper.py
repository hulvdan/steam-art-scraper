"""Скачивание арта Steam-приложения (капсулы, хедер, хиро, лого, скриншоты и т.д.).

Видео и трейлеры намеренно исключены.
"""

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
_APP_REVIEWS_URL = "https://store.steampowered.com/appreviews/{app_id}"
_ASSET_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"
_PAGE_BACKGROUND_CDN = "https://store.akamai.steamstatic.com/images/storepagebackground/"
_COMMUNITY_ICON_CDN = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"

# Ключи `assets` в ответе GetItems, содержащие имена файлов картинок.
# Берётся `<key>_2x`, если есть, иначе `<key>`; файл сохраняется под именем `<key>`.
_ASSET_KEYS = (
    "header",
    "main_capsule",
    "small_capsule",
    "hero_capsule",
    "library_capsule",
    "library_hero",
)

# Старые пути без хеша, которых нет в GetItems: имя -> варианты от лучшего качества к худшему.
# Берётся первый существующий.
_PROBED_ASSETS = {
    "logo": ("logo_2x.png", "logo.png"),
}

_APP_URL_RE = re.compile(r"/app/(\d+)")
# Символы, недопустимые в именах файлов Windows.
_UNSAFE_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Суффикс папки, в которую идёт загрузка; до переименования игра считается неимпортированной.
_PARTIAL_SUFFIX = ".partial"

# Типы арта, которые собираются в папки `_<тип>` со ссылками на файлы всех игр.
# Скриншоты не группируются.
_GROUPED_ASSETS = (*_ASSET_KEYS, "page_background", "community_icon", *_PROBED_ASSETS)
_GROUP_PREFIX = "_"

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
class Reviews:
    total: int
    positive: int

    def __str__(self) -> str:
        """`1234, 85.1%` — число всех обзоров и доля положительных."""
        if not self.total:
            return "0"
        return f"{self.total}, {self.positive / self.total * 100:.1f}%"


@dataclass(frozen=True)
class ScrapedApp:
    app_id: int
    name: str
    slug: str
    artworks: list[Artwork]
    reviews: Reviews

    def dir_name(self, at: datetime) -> str:
        """Имя папки вида `20260926 130512 - Hollow_Knight (1234, 85.1%)` (время импорта)."""
        slug = _UNSAFE_PATH_CHARS_RE.sub("_", self.slug).strip(" .") or str(self.app_id)
        return f"{at:%Y%m%d %H%M%S} - {slug} ({self.reviews})"


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


def _fetch_reviews(app_id: int, client: httpx.Client) -> Reviews:
    """Обзоры на всех языках и от всех покупателей (как «Все обзоры» в магазине)."""
    params = {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}
    resp = client.get(_APP_REVIEWS_URL.format(app_id=app_id), params=params)
    resp.raise_for_status()
    summary = resp.json().get("query_summary", {})
    return Reviews(summary.get("total_reviews", 0), summary.get("total_positive", 0))


def artwork_from_item(app_id: int, item: dict[str, Any]) -> list[Artwork]:
    """Собирает URL арта из элемента ответа GetItems. Чистая функция, без сети."""
    assets: dict[str, Any] = item.get("assets", {})
    url_format: str = assets.get("asset_url_format", f"steam/apps/{app_id}/${{FILENAME}}")

    artworks: list[Artwork] = []
    for key in _ASSET_KEYS:
        if filename := assets.get(f"{key}_2x") or assets.get(key):
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
    for name, filenames in _PROBED_ASSETS.items():
        for filename in filenames:
            url = f"{_ASSET_CDN}steam/apps/{app_id}/{filename}"
            if client.head(url).status_code == 200:
                found.append(Artwork(name, url))
                break
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
        reviews = _fetch_reviews(app_id, client)
        return ScrapedApp(app_id, name, slug, artworks, reviews)
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


def import_artwork(
    artworks: list[Artwork], dest: Path, client: httpx.Client | None = None
) -> list[Path]:
    """Скачивает арт в `<dest>.partial` и переименовывает в `dest` только после успеха.

    Папка с финальным именем существует, только если импорт игры завершён целиком.
    """
    staging = dest.with_name(dest.name + _PARTIAL_SUFFIX)
    try:
        paths = download_artwork(artworks, staging, client)
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return [dest / p.name for p in paths]


def remove_partials(out: Path) -> None:
    """Удаляет недокачанные папки `*.partial`, оставшиеся после прерванного запуска."""
    if out.is_dir():
        for path in out.glob("*" + _PARTIAL_SUFFIX):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)


def _link(link: Path, target: Path) -> None:
    """Создаёт относительный симлинк `link` -> `target` (target лежит в соседней папке)."""
    try:
        link.symlink_to(Path("..", target.parent.name, target.name))
    except OSError:
        # Без режима разработчика Windows не даёт создавать симлинки — берётся жёсткая ссылка.
        link.hardlink_to(target)


def group_by_asset(out: Path) -> None:
    """Пересобирает папки `_<тип>` (`_logo`, `_library_hero`, ...) со ссылками на арт всех игр.

    Ссылка называется по папке игры: `_logo/<папка игры>.png`. Скриншоты не группируются.
    """
    if not out.is_dir():
        return
    for asset in _GROUPED_ASSETS:
        shutil.rmtree(out / (_GROUP_PREFIX + asset), ignore_errors=True)

    games = [
        p
        for p in out.iterdir()
        if p.is_dir()
        and not p.name.startswith(_GROUP_PREFIX)
        and not p.name.endswith(_PARTIAL_SUFFIX)
    ]
    for game in sorted(games):
        for file in game.iterdir():
            if file.stem not in _GROUPED_ASSETS or not file.is_file():
                continue
            group = out / (_GROUP_PREFIX + file.stem)
            group.mkdir(exist_ok=True)
            _link(group / (game.name + file.suffix), file)
