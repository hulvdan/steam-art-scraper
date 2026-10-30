from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from itertools import repeat
from pathlib import Path
from typing import Annotated

import httpx
import typer

from scraper import (
    SteamAppNotFound,
    TrailerError,
    group_by_asset,
    import_artwork,
    parse_app_id,
    remove_partials,
    scrape_artwork,
)

app = typer.Typer(help="Download Steam app artwork and the most recent trailer.")

# Сколько игр импортируется одновременно.
_GAME_WORKERS = 3


def _import_app(app_ref: str, out: Path) -> bool:
    """Скачивает арт и трейлер одной игры и печатает итог. False, если импорт не удался."""
    try:
        scraped = scrape_artwork(app_ref)
        dir_name = scraped.dir_name(datetime.now())
        paths = import_artwork(scraped.artworks, out / dir_name, trailer=scraped.trailer)
    except (ValueError, SteamAppNotFound, TrailerError, httpx.HTTPError, OSError) as e:
        typer.echo(f"Error ({app_ref}): {e}", err=True)
        return False
    missing = len(scraped.artworks) + bool(scraped.trailer) - len(paths)
    typer.echo(f"{dir_name}: {len(paths)} files" + (f" ({missing} missing)" if missing else ""))
    return True


def _dedupe_refs(refs: list[str]) -> list[str]:
    """Убирает повторы одной игры (id и URL): параллельные импорты не должны делить папку."""
    seen: set[int | str] = set()
    unique: list[str] = []
    for ref in refs:
        try:
            key: int | str = parse_app_id(ref)
        except ValueError:
            key = ref
        if key not in seen:
            seen.add(key)
            unique.append(ref)
    return unique


def _read_refs(path: Path) -> list[str]:
    """Читает app id / URL построчно, пропуская пустые строки и комментарии `#`."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


@app.command()
def _main(
    app_refs: Annotated[
        list[str] | None,
        typer.Argument(metavar="[APP]...", help="Steam app ids or store URLs"),
    ] = None,
    from_file: Annotated[
        Path | None,
        typer.Option(
            "--file", "-f", help="Text file with one app id / URL per line ('#' = comment)"
        ),
    ] = None,
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            "-o",
            help="Root dir; each app goes to 'YYYYMMDD HHMMSS - Title (reviews, positive%)'",
        ),
    ] = Path("output"),
    list_only: Annotated[
        bool, typer.Option("--list", help="Only print URLs, don't download")
    ] = False,
) -> None:
    refs = list(app_refs or [])
    if from_file is not None:
        refs += _read_refs(from_file)
    if not refs:
        typer.echo("Nothing to import.", err=True)
        return

    failed = False
    if list_only:
        for app_ref in refs:
            try:
                scraped = scrape_artwork(app_ref)
            except (ValueError, SteamAppNotFound, httpx.HTTPError) as e:
                typer.echo(f"Error ({app_ref}): {e}", err=True)
                failed = True
                continue
            for art in scraped.artworks:
                typer.echo(f"{scraped.app_id}\t{art.name}\t{art.url}")
            if scraped.trailer:
                typer.echo(f"{scraped.app_id}\ttrailer\t{scraped.trailer}")
    else:
        remove_partials(out)
        with ThreadPoolExecutor(_GAME_WORKERS) as pool:
            try:
                results = list(pool.map(_import_app, _dedupe_refs(refs), repeat(out)))
            except BaseException:
                # Не начатые игры не качаются после Ctrl+C или непредвиденной ошибки.
                pool.shutdown(cancel_futures=True)
                raise
        failed = not all(results)
        try:
            group_by_asset(out)
        except OSError as e:
            typer.echo(f"Error (grouping by asset): {e}", err=True)
            failed = True

    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
