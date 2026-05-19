# overturemaps-downloader

A CLI tool for downloading [Overture Maps](https://overturemaps.org/) data for specific administrative areas (i.e. countries, regions). The library uses precise boundary polygons (not bounding boxes) for each administrative area, ensuring that you get the exact data you are looking for. 

## How it works

1. Queries the Overture divisions dataset on S3 to get the boundary polygon for target area
2. Downloads a bounding-box crop of the specified area via the official `overturemaps` CLI
3. Utilizes DuckDB H3 capabilities to efficiently perform a spatial join against the boundary polygon
4. Writes the output file in chosen format
5. Optionally exports an html file with a map for viewing the extracted data

## Requirements

- Python 3.10+
- The [overturemaps CLI](https://github.com/OvertureMaps/overturemaps-py) installed and available on your PATH (`pip install overturemaps`)

## Installation

```bash
git clone https://github.com/dhense177/overturemaps-downloader.git
cd overturemaps-downloader
pip install .
```

## Usage

### Download features

```bash
overturemaps-download download \
  --country-code US \
  --region-code MA \
  --type places \
  --format geoparquet \
  --output output/massachusetts_places.parquet
```

#### Options

| Flag | Required | Description |
|------|----------|-------------|
| `-c`, `--country-code` | Yes | ISO A2 country code (e.g. `US`, `DE`, `JP`) |
| `-r`, `--region-code` | No | ISO region/subdivision code (e.g. `CA`, `NY`, `VLG`) |
| `-t`, `--type` | Yes | Feature type: `places` or `buildings` |
| `-f`, `--format` | Yes | Output format: `geojson`, `geojsonseq`, or `geoparquet` |
| `-o`, `--output` | Yes | Output file path |
| `--release` | No | Overture release version (default: latest) |
| `--largest-only` | No | Use only the largest polygon of the boundary instead of the full multipolygon |
| `--map-output` | No | Path to save an interactive HTML map of the results |

#### Examples

Download places for Italy:
```bash
overturemaps-download download -c IT -t places -f geojson -o italy_places.geojson
```

Download buildings for New York state, with a map:
```bash
overturemaps-download download \
  -c US -r NY \
  -t buildings \
  -f geoparquet \
  -o ny_buildings.parquet \
  --map-output ny_buildings_map.html
```

Download places using a specific Overture release:
```bash
overturemaps-download download \
  -c JP \
  -t places \
  -f geojsonseq \
  -o japan_places.geojsonseq \
  --release 2024-11-13.0
```

### List available releases

```bash
overturemaps-download releases
```

Output:
```
  2026-04-15.0 (latest)
  2026-03-18.0
```

## Output formats

| Format | Extension | Notes |
|--------|-----------|-------|
| `geojson` | `.geojson` | Standard GeoJSON, single FeatureCollection |
| `geojsonseq` | `.geojsonseq` | Newline-delimited GeoJSON, better for large datasets |
| `geoparquet` | `.parquet` | Columnar format, best for large datasets and analytics |

## License

MIT
