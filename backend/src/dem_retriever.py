from typing import Dict, Iterable, List
import math
import rasterio
from rasterio.errors import RasterioIOError
from rasterio.merge import merge
import tempfile


COPERNICUS_DEM_90M_BUCKET = "https://copernicus-dem-90m.s3.amazonaws.com"


def _tile_id(lat: int, lon: int) -> str:
    north_south = "N" if lat >= 0 else "S"
    east_west = "E" if lon >= 0 else "W"
    return (
        f"Copernicus_DSM_COG_30_"
        f"{north_south}{abs(lat):02d}_00_"
        f"{east_west}{abs(lon):03d}_00_DEM"
    )


def _tiles_for_bbox(min_lat: float, min_lon: float, max_lat: float, max_lon: float) -> Iterable[str]:
    lat_start = math.floor(min_lat)
    lat_end = math.ceil(max_lat) - 1
    lon_start = math.floor(min_lon)
    lon_end = math.ceil(max_lon) - 1

    if lat_end < lat_start or lon_end < lon_start:
        return []

    return (_tile_id(lat, lon) for lat in range(lat_start, lat_end + 1) for lon in range(lon_start, lon_end + 1))


def _tile_url(tile_id: str) -> str:
    return f"{COPERNICUS_DEM_90M_BUCKET}/{tile_id}/{tile_id}.tif"


def retrieve_dem(bounding_box: Dict[str, float], write_geotiff: bool = False) -> Dict[str, object]:
    min_lat = bounding_box["min_lat"]
    min_lon = bounding_box["min_lon"]
    max_lat = bounding_box["max_lat"]
    max_lon = bounding_box["max_lon"]

    range_km = 25.0
    km_per_degree_lat = 111.32
    center_lat = (min_lat + max_lat) / 2.0
    km_per_degree_lon = max(0.000001, km_per_degree_lat * math.cos(math.radians(center_lat)))
    lat_pad = range_km / km_per_degree_lat
    lon_pad = range_km / km_per_degree_lon

    min_lat = max(-90.0, min_lat - lat_pad)
    max_lat = min(90.0, max_lat + lat_pad)
    min_lon = max(-180.0, min_lon - lon_pad)
    max_lon = min(180.0, max_lon + lon_pad)

    print(f"retrieve_dem: bbox=({min_lat}, {min_lon}, {max_lat}, {max_lon})")

    if min_lat >= max_lat or min_lon >= max_lon:
        raise ValueError("Invalid bounding box. Expected min < max for both latitude and longitude.")

    tile_ids = list(_tiles_for_bbox(min_lat, min_lon, max_lat, max_lon))
    tile_urls = [_tile_url(tile_id) for tile_id in tile_ids]

    print(f"retrieve_dem: tiles={len(tile_ids)}")
    if tile_ids:
        print(f"retrieve_dem: first_tile={tile_ids[0]}")

    datasets: List[rasterio.io.DatasetReader] = [] # type: ignore
    try:
        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
            for url in tile_urls:
                try:
                    datasets.append(rasterio.open(url))
                    print(f"retrieve_dem: opened {url}")
                except RasterioIOError:
                    print(f"retrieve_dem: missing {url}")
                    continue

            if not datasets:
                raise ValueError("No Copernicus DEM tiles found for the provided bounding box.")

            mosaic, out_trans = merge(datasets, bounds=(min_lon, min_lat, max_lon, max_lat))
            print(f"retrieve_dem: mosaic shape={mosaic.shape}")

            result: Dict[str, object] = {
                "array": mosaic[0],
                "transform": out_trans,
                "crs": datasets[0].crs,
            }

            if write_geotiff:
                tmp_file = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                tmp_path = tmp_file.name
                tmp_file.close()

                with rasterio.open(
                    tmp_path,
                    "w",
                    driver="GTiff",
                    height=mosaic.shape[1],
                    width=mosaic.shape[2],
                    count=1,
                    dtype=mosaic.dtype,
                    crs=datasets[0].crs,
                    transform=out_trans,
                    nodata=None,
                ) as dst:
                    dst.write(mosaic[0], 1)

                result["path"] = tmp_path
                result["cleanup_paths"] = [tmp_path]

            return result
    finally:
        for dataset in datasets:
            dataset.close()