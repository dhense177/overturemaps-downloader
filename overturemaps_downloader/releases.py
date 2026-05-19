import json
from typing import List, Tuple
from urllib.request import urlopen

STAC_CATALOG_URL = "https://stac.overturemaps.org/catalog.json"
_cached_stac_catalog = None


def _get_stac_catalog() -> dict:
    global _cached_stac_catalog
    if _cached_stac_catalog is not None:
        return _cached_stac_catalog
    try:
        with urlopen(STAC_CATALOG_URL) as response:
            _cached_stac_catalog = json.load(response)
        return _cached_stac_catalog
    except Exception as e:
        raise Exception(f"Could not fetch STAC catalog: {e}") from e


def get_available_releases() -> Tuple[List[str], str]:
    """Return (all_releases, latest_release) from the Overture STAC catalog."""
    catalog = _get_stac_catalog()
    latest_release = catalog.get("latest")
    releases = []
    for link in catalog.get("links", []):
        if link.get("rel") == "child":
            href = link.get("href", "")
            release_version = href.strip("./").split("/")[0]
            if release_version:
                releases.append(release_version)
    return releases, latest_release


def get_latest_release() -> str:
    """Return the latest Overture release version string."""
    _, latest = get_available_releases()
    return latest
