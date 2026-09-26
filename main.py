from pathlib import Path
from typing import Annotated

import typer

from steam_art import download_artwork, parse_app_id, scrape_artwork

_app = typer.Typer(help="Download Steam app artwork (no videos).")


@_app.command()
def _main(
    app_ref: Annotated[str, typer.Argument(metavar="APP", help="Steam app id or store URL")],
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Output dir (default: output/<app_id>)")
    ] = None,
    list_only: Annotated[
        bool, typer.Option("--list", help="Only print URLs, don't download")
    ] = False,
) -> None:
    app_id = parse_app_id(app_ref)
    artworks = scrape_artwork(app_id)
    if list_only:
        for art in artworks:
            typer.echo(f"{art.name}\t{art.url}")
        return

    for path in download_artwork(artworks, out or Path("output") / str(app_id)):
        typer.echo(path)


if __name__ == "__main__":
    _app()
