import argparse
import subprocess
import tempfile
from pathlib import Path
from time import time

from overturemaps_downloader.core import (
    DOWNLOAD_EXT,
    OVERTURE_TYPE,
    build_query,
    create_area_boundary_table,
    establish_duckdb_connection,
    generate_map,
    get_bbox,
)
from overturemaps_downloader.releases import get_latest_release
from overturemaps_downloader.writers import _write_output


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and filter Overture places or buildings to a division area."
    )
    parser.add_argument("-c", "--country_code", required=True, type=str, help="ISO A2 country code")
    parser.add_argument("-r", "--region_code", required=False, type=str, help="ISO region code")
    parser.add_argument("-t", "--type", choices=["places", "buildings"], required=True)
    parser.add_argument("-f", choices=["geojson", "geojsonseq", "geoparquet"], required=True)
    parser.add_argument("-o", "--output", required=True, type=Path)
    parser.add_argument(
        "--release",
        required=False,
        type=str,
        default=None,
        help="Overture release version (default: latest)",
    )
    parser.add_argument(
        "--largest-only",
        action="store_true",
        default=False,
        help="Filter to the largest polygon only (default: use full multipolygon)",
    )
    parser.add_argument(
        "--display_map_output",
        required=False,
        type=Path,
        default=None,
        help="If provided, generate an HTML map and save it to this path",
    )
    return parser.parse_args()


def _cli_main() -> None:
    args = _parse_args()
    release = args.release or get_latest_release()
    overture_type = OVERTURE_TYPE[args.type]

    con = establish_duckdb_connection()
    create_area_boundary_table(
        con,
        args.country_code,
        args.region_code,
        release=release,
        largest_only=args.largest_only,
    )
    bbox = get_bbox(con)

    with tempfile.TemporaryDirectory() as tmpdir:
        download_path = Path(tmpdir) / f"{args.type}.{DOWNLOAD_EXT[args.f]}"
        print(f"Downloading temporary data to {download_path}...")
        subprocess.run(
            [
                "overturemaps", "download",
                f"--bbox={bbox}",
                "-f", args.f,
                f"--type={overture_type}",
                "-o", str(download_path),
            ],
            check=True,
        )
        q = build_query(str(download_path), args.type)
        _write_output(con, q, args.output, args.f)

    if args.display_map_output is not None:
        generate_map(args.output, args.display_map_output, con)


def main() -> None:
    """Entrypoint for the overturemaps-download console script."""
    start = time()
    _cli_main()
    print(f"Total time taken: {time() - start:.1f} seconds")
