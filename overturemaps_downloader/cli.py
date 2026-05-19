import subprocess
import tempfile
from pathlib import Path
from time import time

import click

from overturemaps_downloader.core import (
    DOWNLOAD_EXT,
    OVERTURE_TYPE,
    build_query,
    create_area_boundary_table,
    establish_duckdb_connection,
    generate_map,
    get_bbox,
)
from overturemaps_downloader.releases import get_available_releases, get_latest_release
from overturemaps_downloader.writers import _write_output


@click.group()
def cli() -> None:
    """Download and filter Overture Maps data to a geographic boundary."""


@cli.command()
@click.option("-c", "--country-code", required=True, help="ISO A2 country code (e.g. US, DE)")
@click.option("-r", "--region-code", default=None, help="ISO region code (e.g. CA, NY)")
@click.option(
    "-t", "--type", "feature_type",
    required=True,
    type=click.Choice(["places", "buildings"], case_sensitive=False),
    help="Overture feature type to download",
)
@click.option(
    "-f", "--format",
    required=True,
    type=click.Choice(["geojson", "geojsonseq", "geoparquet"], case_sensitive=False),
    help="Output file format",
)
@click.option(
    "-o", "--output",
    required=True,
    type=click.Path(path_type=Path),
    help="Output file path",
)
@click.option(
    "--release",
    default=None,
    show_default="latest",
    help="Overture release version",
)
@click.option(
    "--largest-only",
    is_flag=True,
    default=False,
    help="Use only the largest polygon of the boundary (default: full multipolygon)",
)
@click.option(
    "--map-output",
    default=None,
    type=click.Path(path_type=Path),
    help="If provided, generate an HTML map and save it to this path",
)
def download(
    country_code: str,
    region_code: str | None,
    feature_type: str,
    format: str,
    output: Path,
    release: str | None,
    largest_only: bool,
    map_output: Path | None,
) -> None:
    """Download Overture features clipped to a country or region boundary."""
    start = time()
    release = release or get_latest_release()
    overture_type = OVERTURE_TYPE[feature_type]

    click.echo("\n\n")
    click.echo(f"Establishing Database Connection...\n")
    con = establish_duckdb_connection()
    create_area_boundary_table(
        con,
        country_code,
        region_code,
        release=release,
        largest_only=largest_only,
    )
    bbox = get_bbox(con)

    with tempfile.TemporaryDirectory() as tmpdir:
        download_path = Path(tmpdir) / f"{feature_type}.{DOWNLOAD_EXT[format]}"
        click.echo(f"Downloading temporary data...\n")
        subprocess.run(
            [
                "overturemaps", "download",
                f"--bbox={bbox}",
                "-f", format,
                f"--type={overture_type}",
                "-o", str(download_path),
            ],
            check=True,
        )
        click.echo(f"Executing query and writing results to '{output}'\n")
        q = build_query(str(download_path), feature_type)
        _write_output(con, q, output, format)

    if map_output is not None:
        click.echo(f"Generating map and saving to '{map_output}'\n")
        generate_map(output, map_output, con)

    click.echo(f"Finished Processing!")
    click.echo(f"Total time taken: {time() - start:.1f} seconds")


@cli.command()
def releases() -> None:
    """List available Overture Maps releases."""
    all_releases, latest = get_available_releases()
    for r in all_releases:
        marker = " (latest)" if r == latest else ""
        click.echo(f"  {r}{marker}")


def main() -> None:
    """Entrypoint for the overturemaps-download console script."""
    cli()
