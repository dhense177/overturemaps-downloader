from pathlib import Path
import duckdb


def _write_output(con: duckdb.DuckDBPyConnection, query: str, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    path = str(output_path)
    if fmt == "geoparquet":
        con.execute(f"COPY ({query}) TO '{path}' (FORMAT PARQUET)")
    elif fmt == "geojson":
        con.execute(f"COPY ({query}) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSON')")
    else:
        con.execute(f"COPY ({query}) TO '{path}' WITH (FORMAT GDAL, DRIVER 'GeoJSONSeq')")
