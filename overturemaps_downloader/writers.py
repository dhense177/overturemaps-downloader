from pathlib import Path

import duckdb


def _write_output(
    con: duckdb.DuckDBPyConnection,
    queries: list[str],
    output_path: Path,
    fmt: str,
    tmpdir: str,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    tmp_files = []
    for i, query in enumerate(queries):
        tmp = Path(tmpdir) / f"stage_{i}.parquet"
        con.execute(f"COPY ({query}) TO '{tmp}' (FORMAT PARQUET)")
        tmp_files.append(str(tmp))

    files_sql = "[" + ", ".join(f"'{f}'" for f in tmp_files) + "]"
    merged = f"SELECT * FROM read_parquet({files_sql})"
    path = str(output_path)
    if fmt == "geoparquet":
        con.execute(f"COPY ({merged}) TO '{path}' (FORMAT PARQUET)")
    elif fmt == "geojson":
        con.execute(f"COPY ({merged}) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSON')")
    else:
        con.execute(f"COPY ({merged}) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSONSeq')")
