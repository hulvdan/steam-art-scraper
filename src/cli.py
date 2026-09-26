from pathlib import Path
from typing import Annotated

import httpx
import typer

from scraper import SteamAppNotFound, download_artwork, scrape_artwork

app = typer.Typer(help="Download Steam app artwork (no videos).")


@app.command()
def _main(
    app_refs: Annotated[
        list[str], typer.Argument(metavar="APP...", help="Steam app ids or store URLs")
    ],
    out: Annotated[
        Path, typer.Option("--out", "-o", help="Root dir; each app goes to '<app_id> <Title>'")
    ] = Path("output"),
    list_only: Annotated[
        bool, typer.Option("--list", help="Only print URLs, don't download")
    ] = False,
) -> None:
    failed = False
    for app_ref in app_refs:
        try:
            scraped = scrape_artwork(app_ref)
            if list_only:
                for art in scraped.artworks:
                    typer.echo(f"{scraped.app_id}\t{art.name}\t{art.url}")
                continue
            paths = download_artwork(scraped.artworks, out / scraped.dir_name)
        except (ValueError, SteamAppNotFound, httpx.HTTPError) as e:
            typer.echo(f"Error ({app_ref}): {e}", err=True)
            failed = True
            continue
        typer.echo(f"{scraped.dir_name}: {len(paths)} files")

    if failed:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
