"""Скачивание арта Steam-приложения (капсулы, хедер, хиро, лого, скриншоты и т.д.).

Из видео скачивается только самый новый трейлер.
"""

import json
import math
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import IO, Any
from urllib.parse import urljoin, urlsplit

import httpx
import imageio_ffmpeg

_STORE_BROWSE_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1"
_ASSET_CDN = "https://shared.akamai.steamstatic.com/store_item_assets/"
_PAGE_BACKGROUND_CDN = "https://store.akamai.steamstatic.com/images/storepagebackground/"
_COMMUNITY_ICON_CDN = "https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/"
_TRAILER_CDN = "https://video.akamai.steamstatic.com/store_trailers/"

# Трейлер берётся из DASH-манифеста H.264 (AV1 поддерживается не везде).
_TRAILER_ENCODING = "dash_h264"
_TRAILER_NAME = "trailer"
_TRAILER_MAX_HEIGHT = 1080
_TRAILER_MAX_FPS = 60
# Склейка без перекодирования занимает секунды; дольше — значит ffmpeg завис.
_FFMPEG_TIMEOUT_SECONDS = 600
# Сколько сегментов трейлера качается одновременно.
_SEGMENT_WORKERS = 8
_MPD_NS = "{urn:mpeg:dash:schema:mpd:2011}"
_ISO_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?")
_SEGMENT_NUMBER_RE = re.compile(r"\$Number(?:%0(\d+)d)?\$")

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
_GROUPED_ASSETS = (
    *_ASSET_KEYS,
    "page_background",
    "community_icon",
    *_PROBED_ASSETS,
    _TRAILER_NAME,
)
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
    percent_positive: int

    def __str__(self) -> str:
        """`1234, 85%` — число обзоров и доля положительных."""
        if not self.total:
            return "0"
        return f"{self.total}, {self.percent_positive}%"


@dataclass(frozen=True)
class TrailerStream:
    """Поток DASH-трейлера (видео или аудио): init-сегмент и сегменты с данными."""

    init_url: str
    chunk_urls: list[str]


@dataclass(frozen=True)
class ScrapedApp:
    app_id: int
    name: str
    slug: str
    artworks: list[Artwork]
    reviews: Reviews
    # URL DASH-манифеста самого нового трейлера.
    trailer: str | None = None

    def dir_name(self, at: datetime) -> str:
        """Имя папки вида `20260926 130512 - Hollow_Knight (1234, 85.1%)` (время импорта)."""
        slug = _UNSAFE_PATH_CHARS_RE.sub("_", self.slug).strip(" .") or str(self.app_id)
        return f"{at:%Y%m%d %H%M%S} - {slug} ({self.reviews})"


class SteamAppNotFound(LookupError):
    pass


class TrailerError(RuntimeError):
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
        "data_request": {
            "include_assets": True,
            "include_screenshots": True,
            "include_trailers": True,
            "include_reviews": True,
        },
    }
    resp = client.get(_STORE_BROWSE_URL, params={"input_json": json.dumps(input_json)})
    resp.raise_for_status()
    items = resp.json().get("response", {}).get("store_items", [])
    if not items or items[0].get("success") != 1:
        raise SteamAppNotFound(f"Steam app {app_id} not found")
    return items[0]


def reviews_from_item(item: dict[str, Any]) -> Reviews:
    """Обзоры на всех языках из элемента ответа GetItems (как «Все обзоры» в магазине).

    Steam отдаёт число без обзоров, отфильтрованных как не относящиеся к игре, и долю
    положительных в целых процентах. Чистая функция, без сети.
    """
    summary = item.get("reviews", {}).get("summary_filtered", {})
    return Reviews(summary.get("review_count", 0), summary.get("percent_positive", 0))


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


def trailer_from_item(item: dict[str, Any]) -> str | None:
    """URL DASH-манифеста самого нового трейлера из элемента ответа GetItems. Чистая функция.

    Самый новый — с наибольшим `trailer_base_id`: он не меняется при перезаливке трейлера,
    в отличие от `trailer_id` и метки времени `?t=`.
    """
    trailers: dict[str, Any] = item.get("trailers", {})
    candidates = [
        (trailer.get("trailer_base_id", 0), path)
        for trailer in (*trailers.get("highlights", []), *trailers.get("other_trailers", []))
        for adaptive in trailer.get("adaptive_trailers", [])
        if adaptive.get("encoding") == _TRAILER_ENCODING and (path := adaptive.get("cdn_path"))
    ]
    if not candidates:
        return None
    return _TRAILER_CDN + max(candidates)[1]


def _iso_duration_seconds(value: str) -> float:
    """`PT1M35.3S` -> 95.3."""
    match = _ISO_DURATION_RE.fullmatch(value)
    if not match:
        raise TrailerError(f"Unsupported MPD duration {value!r}")
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)


def _mpd_stream(
    mpd_url: str, adaptation: ET.Element, rep: ET.Element, total_seconds: float
) -> TrailerStream:
    template = rep.find(f"{_MPD_NS}SegmentTemplate")
    if template is None:
        template = adaptation.find(f"{_MPD_NS}SegmentTemplate")
    if template is None:
        raise TrailerError("MPD representation has no SegmentTemplate")
    rep_id = rep.get("id", "")

    def url(attr: str, number: int) -> str:
        path = template.get(attr, "").replace("$RepresentationID$", rep_id)
        path = _SEGMENT_NUMBER_RE.sub(lambda m: f"{number:0{m.group(1) or 0}d}", path)
        return urljoin(mpd_url, path)

    segment_seconds = int(template.get("duration", "0")) / int(template.get("timescale", "1"))
    if segment_seconds <= 0:
        raise TrailerError("MPD SegmentTemplate has no duration")
    start = int(template.get("startNumber", "1"))
    # Округление гасит погрешность float: 30.0 / 3.0 должно дать ровно 10 сегментов.
    count = math.ceil(round(total_seconds / segment_seconds, 6))
    if count <= 0:
        raise TrailerError("MPD has no segments")
    return TrailerStream(
        url("initialization", start), [url("media", start + i) for i in range(count)]
    )


def pick_trailer_streams(mpd_url: str, mpd_xml: str) -> list[TrailerStream]:
    """Выбирает потоки трейлера из DASH-манифеста. Чистая функция, без сети.

    Видео — с наибольшим битрейтом не выше 1080p и 60 fps, аудио (если есть) — с наибольшим
    битрейтом. Видео с неизвестными высотой или частотой кадров не берётся. Пустой список, если
    подходящего видео нет.
    """
    try:
        return _pick_trailer_streams(mpd_url, mpd_xml)
    except (ET.ParseError, ValueError, ZeroDivisionError) as e:
        raise TrailerError(f"Malformed MPD: {e}") from e


def _pick_trailer_streams(mpd_url: str, mpd_xml: str) -> list[TrailerStream]:
    root = ET.fromstring(mpd_xml)
    total_seconds = _iso_duration_seconds(root.get("mediaPresentationDuration", ""))
    # У трейлеров Steam один Period; номера сегментов считаются только для него.
    period = root.find(f"{_MPD_NS}Period")
    if period is None:
        raise TrailerError("MPD has no Period")
    best: dict[str, tuple[int, TrailerStream]] = {}
    for adaptation in period.iter(f"{_MPD_NS}AdaptationSet"):
        kind = adaptation.get("contentType") or adaptation.get("mimeType", "").split("/")[0]
        if kind not in ("video", "audio"):
            continue
        for rep in adaptation.iter(f"{_MPD_NS}Representation"):
            if kind == "video":
                height = int(rep.get("height") or adaptation.get("maxHeight") or 0)
                fps = rep.get("frameRate") or adaptation.get("frameRate")
                if not height or height > _TRAILER_MAX_HEIGHT:
                    continue
                if not fps or Fraction(fps) > _TRAILER_MAX_FPS:
                    continue
            bandwidth = int(rep.get("bandwidth", "0"))
            if kind not in best or bandwidth > best[kind][0]:
                best[kind] = (bandwidth, _mpd_stream(mpd_url, adaptation, rep, total_seconds))
    if "video" not in best:
        return []
    return [best[kind][1] for kind in ("video", "audio") if kind in best]


def _mux(parts: list[Path], out: Path) -> None:
    """Склеивает потоки (видео и аудио) в один mp4 без перекодирования."""
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y"]
    for part in parts:
        cmd += ["-i", str(part)]
    for i in range(len(parts)):
        cmd += ["-map", str(i)]
    cmd += ["-c", "copy", "-movflags", "+faststart", str(out)]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise TrailerError("ffmpeg timed out") from e
    if result.returncode:
        raise TrailerError(f"ffmpeg failed: {result.stderr.strip()}")


def _get_content(url: str, client: httpx.Client) -> bytes:
    resp = client.get(url)
    resp.raise_for_status()
    return resp.content


def download_segments(urls: list[str], f: IO[bytes], client: httpx.Client) -> None:
    """Качает сегменты параллельно и пишет в `f` по порядку.

    Впереди записи качается не больше `2 * _SEGMENT_WORKERS` сегментов, чтобы не держать весь
    трейлер в памяти.
    """
    with ThreadPoolExecutor(_SEGMENT_WORKERS) as pool:
        pending: deque[Future[bytes]] = deque()
        try:
            for url in urls:
                pending.append(pool.submit(_get_content, url, client))
                if len(pending) >= 2 * _SEGMENT_WORKERS:
                    f.write(pending.popleft().result())
            while pending:
                f.write(pending.popleft().result())
        except BaseException:
            for future in pending:
                future.cancel()
            raise


def download_trailer(mpd_url: str, dest: Path, client: httpx.Client) -> Path | None:
    """Скачивает трейлер в `dest/trailer.mp4`. None, если манифеста нет или видео не подходит."""
    resp = client.get(mpd_url)
    if resp.status_code == httpx.codes.NOT_FOUND:
        return None
    resp.raise_for_status()
    streams = pick_trailer_streams(mpd_url, resp.text)
    if not streams:
        return None

    parts: list[Path] = []
    for i, stream in enumerate(streams):
        part = dest / f"{_TRAILER_NAME}.part{i}.mp4"
        with part.open("wb") as f:
            download_segments([stream.init_url, *stream.chunk_urls], f, client)
        parts.append(part)

    path = dest / (_TRAILER_NAME + ".mp4")
    _mux(parts, path)
    for part in parts:
        part.unlink()
    return path


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
    """Возвращает весь арт и URL самого нового трейлера для app id или URL магазина Steam."""
    app_id = parse_app_id(app)
    owns_client = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        item = _fetch_store_item(app_id, client)
        name: str = item.get("name", "")
        slug: str = item.get("store_url_slug") or name.replace(" ", "_")
        artworks = artwork_from_item(app_id, item) + _probe_legacy_assets(app_id, client)
        return ScrapedApp(
            app_id, name, slug, artworks, reviews_from_item(item), trailer_from_item(item)
        )
    finally:
        if owns_client:
            client.close()


def _extension(url: str, content_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix
    if suffix:
        return suffix
    return _CONTENT_TYPE_EXT.get(content_type.split(";")[0].strip(), "")


def download_artwork(
    artworks: list[Artwork],
    dest: Path,
    client: httpx.Client | None = None,
    trailer: str | None = None,
) -> list[Path]:
    """Скачивает арт и трейлер в `dest`. Отсутствующие на CDN файлы (404) пропускаются."""
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
        if trailer and (path := download_trailer(trailer, dest, client)):
            paths.append(path)
    finally:
        if owns_client:
            client.close()
    return paths


def import_artwork(
    artworks: list[Artwork],
    dest: Path,
    client: httpx.Client | None = None,
    trailer: str | None = None,
) -> list[Path]:
    """Скачивает арт и трейлер в `<dest>.partial` и переименовывает в `dest` только после успеха.

    Папка с финальным именем существует, только если импорт игры завершён целиком.
    """
    staging = dest.with_name(dest.name + _PARTIAL_SUFFIX)
    try:
        paths = download_artwork(artworks, staging, client, trailer)
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


def group_by_asset(out: Path) -> None:
    """Пересобирает папки `_<тип>` (`_logo`, `_library_hero`, ...) с жёсткими ссылками на арт всех игр.

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
            (group / (game.name + file.suffix)).hardlink_to(file)
