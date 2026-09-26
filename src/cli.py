from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer

from scraper import SteamAppNotFound, import_artwork, remove_partials, scrape_artwork

app = typer.Typer(help="Download Steam app artwork (no videos).")


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
        typer.Option("--out", "-o", help="Root dir; each app goes to 'YYYYMMDD HHMMSS - Title'"),
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

    if not list_only:
        remove_partials(out)

    failed = False
    for app_ref in refs:
        try:
            scraped = scrape_artwork(app_ref)
            if list_only:
                for art in scraped.artworks:
                    typer.echo(f"{scraped.app_id}\t{art.name}\t{art.url}")
                continue
            dir_name = scraped.dir_name(datetime.now())
            paths = import_artwork(scraped.artworks, out / dir_name)
        except (ValueError, SteamAppNotFound, httpx.HTTPError, OSError) as e:
            typer.echo(f"Error ({app_ref}): {e}", err=True)
            failed = True
            continue
        missing = len(scraped.artworks) - len(paths)
        typer.echo(f"{dir_name}: {len(paths)} files" + (f" ({missing} missing)" if missing else ""))

    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
