from pathlib import Path

import duckdb
import geopandas as gpd
import pandas as pd
from duckdb.sqltypes import BLOB
from shapely import wkb, from_wkb

try:
    import folium as _folium
except ImportError:
    _folium = None

RESOLUTION = 6
OVERTURE_TYPE = {"places": "place", "buildings": "building", "addresses": "address"}
DOWNLOAD_EXT = {"geoparquet": "parquet", "geojson": "geojson", "geojsonseq": "geojsonseq"}
OVERTURE_S3_THEME = {
    "places": "theme=places/type=place",
    "buildings": "theme=buildings/type=building",
    "addresses": "theme=addresses/type=address",
}
POINT_GEOMETRY_TYPES = {"places", "addresses"}


def get_s3_path(feature_type: str, release: str) -> str:
    return f"s3://overturemaps-us-west-2/release/{release}/{OVERTURE_S3_THEME[feature_type]}/*.parquet"


def get_largest_polygon(geometry: BLOB) -> BLOB:
    geom = wkb.loads(geometry)
    if not hasattr(geom, "geoms"):
        return wkb.dumps(geom)
    return wkb.dumps(max(geom.geoms, key=lambda g: g.area))


def establish_duckdb_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("INSTALL spatial; LOAD spatial;")
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    con.execute("SET s3_region='us-west-2';")
    con.execute("SET temp_directory='/tmp/duckdb_tmp';")
    return con


def _region_filter(country_code: str, region_code: str | None) -> str:
    if country_code and region_code:
        return f"""
            WHERE region = '{country_code}-{region_code}'
            AND subtype = 'region'
            AND class = 'land'
        """
    elif country_code:
        return f"""
            WHERE country = '{country_code}'
            AND subtype = 'country'
            AND class = 'land'
        """
    else:
        raise ValueError(f"Invalid country or region code: {country_code!r}, {region_code!r}")


def create_area_boundary_table(
    con: duckdb.DuckDBPyConnection,
    country_code: str,
    region_code: str | None,
    release: str,
    largest_only: bool = False,
) -> None:
    end_filter = _region_filter(country_code, region_code)
    s3_path = f"s3://overturemaps-us-west-2/release/{release}/theme=divisions/type=division_area/*.parquet"

    row = con.execute(f"""
        SELECT
            ST_AsWKB(geometry) AS geom_wkb,
            CONCAT(bbox.xmin, ',', bbox.ymin, ',', bbox.xmax, ',', bbox.ymax) AS bbox
        FROM '{s3_path}'
        {end_filter}
    """).fetchone()
    if row is None:
        area = f"{country_code}-{region_code}" if region_code else country_code
        raise ValueError(f"No Overture boundary found for '{area}' in release {release!r}")
    geom_wkb, bbox_str = row

    geom = wkb.loads(bytes(geom_wkb))
    polygons = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
    if largest_only:
        polygons = [max(polygons, key=lambda g: g.area)]

    cells_within, cells_overlap = set(), set()
    for poly in polygons:
        poly_wkt = poly.wkt
        cells_within.update(con.execute(
            f"SELECT h3_polygon_wkt_to_cells_experimental_string('{poly_wkt}', {RESOLUTION}, 'full')"
        ).fetchone()[0])
        cells_overlap.update(con.execute(
            f"SELECT h3_polygon_wkt_to_cells_experimental_string('{poly_wkt}', {RESOLUTION}, 'overlap')"
        ).fetchone()[0])

    cells_boundary = cells_overlap - cells_within

    con.execute("""
        CREATE OR REPLACE TABLE area_boundary AS
        SELECT
            ST_GeomFromWKB($1) AS geometry,
            $2 AS bbox,
            CAST(split_part($2, ',', 1) AS DOUBLE) AS xmin,
            CAST(split_part($2, ',', 2) AS DOUBLE) AS ymin,
            CAST(split_part($2, ',', 3) AS DOUBLE) AS xmax,
            CAST(split_part($2, ',', 4) AS DOUBLE) AS ymax,
            $3 AS h3_cells_within,
            $4 AS h3_cells_boundary
    """, [geom_wkb, bbox_str, list(cells_within), list(cells_boundary)])


def get_bbox(con: duckdb.DuckDBPyConnection) -> str:
    return con.execute("SELECT bbox FROM area_boundary").fetchone()[0]


def _bbox_filter() -> str:
    return """
    WHERE bbox.xmin <= (SELECT xmax FROM area_boundary)
      AND bbox.xmax >= (SELECT xmin FROM area_boundary)
      AND bbox.ymin <= (SELECT ymax FROM area_boundary)
      AND bbox.ymax >= (SELECT ymin FROM area_boundary)"""


def _h3_point(feature_type: str) -> str:
    if feature_type in POINT_GEOMETRY_TYPES:
        return f"h3_latlng_to_cell_string(ST_Y(p.geometry), ST_X(p.geometry), {RESOLUTION})"
    return (
        f"h3_latlng_to_cell_string("
        f"ST_Y(ST_Centroid(p.geometry)), "
        f"ST_X(ST_Centroid(p.geometry)), {RESOLUTION})"
    )


def build_within_query(source_path: str, feature_type: str) -> str:
    h3_point = _h3_point(feature_type)
    return f"""
WITH target_cells AS (
    SELECT unnest(h3_cells_within) AS h3_idx FROM area_boundary
),
features AS (
    SELECT * FROM '{source_path}'{_bbox_filter()}
)
SELECT p.*
FROM features p
JOIN target_cells t ON {h3_point} = t.h3_idx
"""


def build_boundary_query(source_path: str, feature_type: str) -> str:
    h3_point = _h3_point(feature_type)
    within_expr = (
        "ST_Within(p.geometry, a.geometry)"
        if feature_type in POINT_GEOMETRY_TYPES
        else "ST_Within(ST_Centroid(p.geometry), a.geometry)"
    )
    return f"""
WITH target_cells AS (
    SELECT unnest(h3_cells_boundary) AS h3_idx FROM area_boundary
),
features AS (
    SELECT * FROM '{source_path}'{_bbox_filter()}
),
candidates AS (
    SELECT p.*
    FROM features p
    JOIN target_cells t ON {h3_point} = t.h3_idx
)
SELECT p.*
FROM candidates p, area_boundary a
WHERE {within_expr}
"""


def generate_map(
    output_path: Path,
    map_output_path: Path,
    con: duckdb.DuckDBPyConnection,
) -> None:
    if _folium is None:
        raise ImportError(
            "Map generation requires the 'map' extra. "
            "Install it with: pip install overturemaps-downloader-py[map]"
        )

    if output_path.suffix == ".parquet":
        df = pd.read_parquet(output_path)
        gdf = gpd.GeoDataFrame(df, geometry=from_wkb(df["geometry"]), crs="EPSG:4326")
    else:
        gdf = gpd.read_file(output_path)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")

    boundary_wkb = con.execute("SELECT ST_AsWKB(geometry) FROM area_boundary").fetchone()[0]
    boundary_geom = wkb.loads(bytes(boundary_wkb))
    boundary_gdf = gpd.GeoDataFrame(geometry=[boundary_geom], crs="EPSG:4326")

    centroid = boundary_geom.centroid
    m = _folium.Map(location=[centroid.y, centroid.x], zoom_start=8)

    _folium.GeoJson(
        boundary_gdf.__geo_interface__,
        name="Boundary",
        style_function=lambda x: {"fillColor": "blue", "fillOpacity": 0.05, "color": "blue", "weight": 2},
    ).add_to(m)

    _folium.GeoJson(
        gdf[["geometry"]].__geo_interface__,
        name="Features",
        marker=_folium.CircleMarker(radius=3, fill=True, fill_color="red", fill_opacity=0.6, color="red", weight=0),
    ).add_to(m)

    _folium.LayerControl().add_to(m)

    map_output_path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(map_output_path))
