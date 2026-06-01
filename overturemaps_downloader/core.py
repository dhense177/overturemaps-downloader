from pathlib import Path

import contextily as ctx
import datashader as ds
import datashader.transfer_functions as tf
import colorcet
import duckdb
import geopandas as gpd
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from duckdb.sqltypes import BLOB
from shapely import wkb, from_wkb

RESOLUTION = 6
OVERTURE_TYPE = {
    "places": "place",
    "buildings": "building",
    "building_part": "building_part",
    "addresses": "address",
    "segments": "segment",
    "connectors": "connector",
    "bathymetry": "bathymetry",
    "infrastructure": "infrastructure",
    "land": "land",
    "land_cover": "land_cover",
    "land_use": "land_use",
    "water": "water",
}
DOWNLOAD_EXT = {"geoparquet": "parquet", "geojson": "geojson", "geojsonseq": "geojsonseq"}
OVERTURE_S3_THEME = {
    "places": "theme=places/type=place",
    "buildings": "theme=buildings/type=building",
    "building_part": "theme=buildings/type=building_part",
    "addresses": "theme=addresses/type=address",
    "segments": "theme=transportation/type=segment",
    "connectors": "theme=transportation/type=connector",
    "bathymetry": "theme=base/type=bathymetry",
    "infrastructure": "theme=base/type=infrastructure",
    "land": "theme=base/type=land",
    "land_cover": "theme=base/type=land_cover",
    "land_use": "theme=base/type=land_use",
    "water": "theme=base/type=water",
}
POINT_GEOMETRY_TYPES = {"places", "addresses", "connectors"}
LINESTRING_GEOMETRY_TYPES = {"segments"}
# These types contain a mix of point, linestring, and polygon features per row
MIXED_GEOMETRY_TYPES = {"infrastructure", "land", "land_use", "water"}


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


def _boundary_expr(feature_type: str) -> str:
    if feature_type in POINT_GEOMETRY_TYPES:
        return "ST_Within(p.geometry, a.geometry)"
    if feature_type in LINESTRING_GEOMETRY_TYPES or feature_type in MIXED_GEOMETRY_TYPES:
        return "ST_Intersects(p.geometry, a.geometry)"
    return "ST_Within(ST_Centroid(p.geometry), a.geometry)"


def build_boundary_query(source_path: str, feature_type: str) -> str:
    h3_point = _h3_point(feature_type)
    within_expr = _boundary_expr(feature_type)
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


def build_combined_query(source_path: str, feature_type: str) -> str:
    h3_point = _h3_point(feature_type)
    within_expr = _boundary_expr(feature_type)
    return f"""
WITH target_within AS (
    SELECT unnest(h3_cells_within) AS h3_idx FROM area_boundary
),
target_boundary AS (
    SELECT unnest(h3_cells_boundary) AS h3_idx FROM area_boundary
),
features AS (
    SELECT * FROM '{source_path}'{_bbox_filter()}
),
within_results AS (
    SELECT p.*
    FROM features p
    JOIN target_within t ON {h3_point} = t.h3_idx
),
boundary_candidates AS (
    SELECT p.*
    FROM features p
    JOIN target_boundary t ON {h3_point} = t.h3_idx
),
boundary_results AS (
    SELECT p.*
    FROM boundary_candidates p, area_boundary a
    WHERE {within_expr}
)
SELECT * FROM within_results
UNION ALL
SELECT * FROM boundary_results
"""


def _geoms_to_xy(geometries) -> tuple[np.ndarray, np.ndarray]:
    """Convert geometries to NaN-separated x/y arrays for datashader line rendering."""
    xs, ys = [], []
    for geom in geometries:
        parts = list(geom.geoms) if hasattr(geom, "geoms") else [geom]
        for part in parts:
            coords = np.array(part.coords)
            xs.append(coords[:, 0])
            ys.append(coords[:, 1])
            xs.append([np.nan])
            ys.append([np.nan])
    return np.concatenate(xs), np.concatenate(ys)


def generate_map(
    output_path: Path,
    map_output_path: Path,
    con: duckdb.DuckDBPyConnection,
) -> None:
    if output_path.suffix == ".parquet":
        df = pd.read_parquet(output_path)
        gdf = gpd.GeoDataFrame(df, geometry=from_wkb(df["geometry"]), crs="EPSG:4326")
    else:
        gdf = gpd.read_file(output_path)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")

    boundary_wkb = con.execute("SELECT ST_AsWKB(geometry) FROM area_boundary").fetchone()[0]
    boundary_geom = wkb.loads(bytes(boundary_wkb))

    minx, miny, maxx, maxy = boundary_geom.bounds
    pad_x = (maxx - minx) * 0.2
    pad_y = (maxy - miny) * 0.2

    canvas = ds.Canvas(plot_width=1200, plot_height=900,
                       x_range=(minx, maxx), y_range=(miny, maxy))

    if len(gdf) > 0:
        geom_types = set(gdf.geometry.geom_type.unique())
        pure_points = geom_types <= {"Point", "MultiPoint"}
        pure_lines = geom_types <= {"LineString", "MultiLineString", "LinearRing"}
        if pure_lines:
            line_xs, line_ys = _geoms_to_xy(gdf.geometry)
            agg = canvas.line(pd.DataFrame({"x": line_xs, "y": line_ys}), "x", "y")
        elif pure_points:
            plot_df = pd.DataFrame({
                "x": gdf.geometry.x.values,
                "y": gdf.geometry.y.values,
            })
            agg = canvas.points(plot_df, "x", "y")
        else:
            # Polygons, mixed types, or anything else — centroid works for all
            plot_df = pd.DataFrame({
                "x": gdf.geometry.centroid.x.values,
                "y": gdf.geometry.centroid.y.values,
            })
            agg = canvas.points(plot_df, "x", "y")

        # No background — transparent pixels let the basemap show through
        feature_img = tf.shade(agg, cmap=colorcet.fire, how="log")
        boundary_xs, boundary_ys = _geoms_to_xy([boundary_geom.boundary])
        boundary_agg = canvas.line(pd.DataFrame({"x": boundary_xs, "y": boundary_ys}), "x", "y")
        boundary_img = tf.shade(boundary_agg, cmap=["white", "white"])
        result = tf.stack(feature_img, boundary_img)
    else:
        boundary_xs, boundary_ys = _geoms_to_xy([boundary_geom.boundary])
        boundary_agg = canvas.line(pd.DataFrame({"x": boundary_xs, "y": boundary_ys}), "x", "y")
        result = tf.shade(boundary_agg, cmap=["white", "white"])

    img_array = np.array(result.to_pil().convert("RGBA"))

    fig, ax = plt.subplots(figsize=(14, 10))
    ax.set_xlim(minx - pad_x, maxx + pad_x)
    ax.set_ylim(miny - pad_y, maxy + pad_y)
    ax.set_aspect("equal", adjustable="box")

    ctx.add_basemap(ax, crs="EPSG:4326", source=ctx.providers.CartoDB.DarkMatter)
    ax.imshow(img_array, extent=[minx, maxx, miny, maxy], origin="upper", zorder=5)
    ax.set_axis_off()

    map_output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(map_output_path.with_suffix(".png")), dpi=150, bbox_inches="tight", pad_inches=0)
    plt.close(fig)
