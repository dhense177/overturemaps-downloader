#!/usr/bin/env python3
"""
Pre-compute estimated record counts and average record sizes for all Overture
data types and countries. Run this script offline to generate
aggregate_calculations.json, which the CLI uses to show data size estimates
before a download begins.

Usage:
    python aggregate_calculations.py
    python aggregate_calculations.py --release 2026-05-20.0
    python aggregate_calculations.py --output path/to/output.json

Runtime notes:
  - One bbox intersection query per country per data type (~200 countries each)
    — counts are approximate (bounding box, not exact polygon)
  - Average record size is estimated by reservoir-sampling 10,000 records per
    type from across the full global dataset, then writing to each output format
    and measuring bytes / record_count
"""

import argparse
import json
import random
import tempfile
from pathlib import Path
from time import time

from overturemaps_downloader.core import DOWNLOAD_EXT, OVERTURE_S3_THEME, establish_duckdb_connection
from overturemaps_downloader.releases import get_latest_release

ALL_TYPES: list[str] = ["addresses", "places", "buildings", "segments", "connectors"]
ALL_FORMATS: list[str] = ["geoparquet", "geojson", "geojsonseq"]
SAMPLE_SIZE: int = 10_000
SAMPLE_SEED: int = 42
NUM_SAMPLE_FILES: int = 5

DIVISIONS_S3 = (
    "s3://overturemaps-us-west-2/release/{release}"
    "/theme=divisions/type=division_area/*.parquet"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def s3_path(feature_type: str, release: str) -> str:
    return (
        f"s3://overturemaps-us-west-2/release/{release}"
        f"/{OVERTURE_S3_THEME[feature_type]}/*.parquet"
    )


def fetch_country_bboxes(con, release: str) -> list[tuple]:
    """
    Return (country_code, xmin, ymin, xmax, ymax) for every country in the
    Overture divisions dataset. Uses GROUP BY to safely collapse any
    multi-row countries (e.g. territories stored separately).
    """
    path = DIVISIONS_S3.format(release=release)
    return con.execute(f"""
        SELECT
            country,
            min(bbox.xmin) AS xmin,
            min(bbox.ymin) AS ymin,
            max(bbox.xmax) AS xmax,
            max(bbox.ymax) AS ymax
        FROM '{path}'
        WHERE subtype = 'country'
          AND class = 'land'
        GROUP BY country
        ORDER BY country
    """).fetchall()


def count_by_bbox(
    con,
    feature_type: str,
    country_bboxes: list[tuple],
    release: str,
) -> list[dict]:
    """
    Count records per country using bbox intersection.
    Approximate — uses each country's bounding box, not its exact polygon.
    """
    path = s3_path(feature_type, release)
    results = []
    total = len(country_bboxes)

    for i, (country, xmin, ymin, xmax, ymax) in enumerate(country_bboxes, 1):
        print(f"  [{i:>3}/{total}] {country}", end="\r")
        row = con.execute(f"""
            SELECT count(*)
            FROM read_parquet('{path}')
            WHERE bbox.xmin <= {xmax}
              AND bbox.xmax >= {xmin}
              AND bbox.ymin <= {ymax}
              AND bbox.ymax >= {ymin}
        """).fetchone()
        results.append({
            "country": country,
            "data_type": feature_type,
            "estimated_rows": row[0],
            "release": release,
        })

    print()  # clear the \r line
    return results


def sample_avg_record_bytes(con, feature_type: str, release: str) -> dict[str, int]:
    """
    Estimate average bytes per record for each output format.

    Randomly selects NUM_SAMPLE_FILES parquet files from the full S3 dataset,
    then reservoir-samples SAMPLE_SIZE records from within those files.
    Sampling at the file level avoids scanning the entire global dataset while
    still providing geographic diversity.
    """
    path = s3_path(feature_type, release)

    # List all parquet files for this type and randomly select a subset
    all_files = [row[0] for row in con.execute(f"SELECT * FROM glob('{path}')").fetchall()]
    rng = random.Random(SAMPLE_SEED)
    selected_files = rng.sample(all_files, min(NUM_SAMPLE_FILES, len(all_files)))
    files_sql = "[" + ", ".join(f"'{f}'" for f in selected_files) + "]"
    print(f"  Sampling from {len(selected_files)} of {len(all_files)} files...")

    # Draw the sample into a temp table so we write identical rows to each format
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _sample AS
        SELECT * FROM read_parquet({files_sql})
        USING SAMPLE reservoir ({SAMPLE_SIZE} ROWS) REPEATABLE ({SAMPLE_SEED})
    """)
    actual_n = con.execute("SELECT count(*) FROM _sample").fetchone()[0]

    avg_bytes: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        for fmt in ALL_FORMATS:
            ext = DOWNLOAD_EXT[fmt]
            tmp_file = Path(tmpdir) / f"sample.{ext}"
            if fmt == "geoparquet":
                con.execute(f"COPY (SELECT * FROM _sample) TO '{tmp_file}' (FORMAT PARQUET)")
            elif fmt == "geojson":
                con.execute(f"COPY (SELECT * FROM _sample) TO '{tmp_file}' WITH (FORMAT GDAL, DRIVER 'GeoJSON')")
            else:
                con.execute(f"COPY (SELECT * FROM _sample) TO '{tmp_file}' WITH (FORMAT GDAL, DRIVER 'GeoJSONSeq')")
            avg_bytes[fmt] = tmp_file.stat().st_size // actual_n

    con.execute("DROP TABLE IF EXISTS _sample")
    return avg_bytes


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-compute Overture record counts and avg record sizes per country and data type."
    )
    parser.add_argument(
        "--release",
        default=None,
        help="Overture release version (default: latest)",
    )
    parser.add_argument(
        "--output",
        default="aggregate_calculations.json",
        help="Output JSON file path (default: aggregate_calculations.json)",
    )
    args = parser.parse_args()

    release = args.release or get_latest_release()
    output_path = Path(args.output)

    print(f"Release : {release}")
    print(f"Output  : {output_path}\n")

    con = establish_duckdb_connection()
    total_start = time()

    # --- Average record bytes (sampled once per type, not per country) ---
    avg_record_bytes: dict[str, dict[str, int]] = {}
    for feature_type in ALL_TYPES:
        print(f"[{feature_type}] sampling {SAMPLE_SIZE:,} records for avg size...")
        t = time()
        avg_record_bytes[feature_type] = sample_avg_record_bytes(con, feature_type, release)
        sizes = "  |  ".join(f"{fmt}: {avg_record_bytes[feature_type][fmt]}B" for fmt in ALL_FORMATS)
        print(f"  {sizes}  ({time() - t:.1f}s)\n")

    # --- Per-country record counts ---
    print("[all types] fetching country bounding boxes from divisions...")
    t = time()
    country_bboxes = fetch_country_bboxes(con, release)
    print(f"  {len(country_bboxes)} countries fetched in {time() - t:.1f}s\n")

    counts: list[dict] = []
    for feature_type in ALL_TYPES:
        print(f"[{feature_type}] counting via bbox intersection...")
        t = time()
        batch = count_by_bbox(con, feature_type, country_bboxes, release)
        counts.extend(batch)
        print(f"  {len(batch)} countries in {time() - t:.1f}s\n")

    # --- Write output ---
    output = {
        "release": release,
        "avg_record_bytes": avg_record_bytes,
        "counts": counts,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w") as f:
        json.dump(output, f, indent=2)

    print(f"Saved {len(counts)} count records to '{output_path}'")
    print(f"Total time: {time() - total_start:.1f}s")


if __name__ == "__main__":
    main()
