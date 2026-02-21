from typing import Dict, Iterable, List
import math
import rasterio
import rasterio.warp
from rasterio.errors import RasterioIOError
from rasterio.merge import merge
from rasterio.enums import Resampling
from pyproj import CRS
import tempfile
from loguru import logger
import numpy as np
from rasterio.transform import array_bounds


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

    logger.debug(f"retrieve_dem: bbox=({min_lat}, {min_lon}, {max_lat}, {max_lon})")

    if min_lat >= max_lat or min_lon >= max_lon:
        raise ValueError("Invalid bounding box. Expected min < max for both latitude and longitude.")

    tile_ids = list(_tiles_for_bbox(min_lat, min_lon, max_lat, max_lon))
    tile_urls = [_tile_url(tile_id) for tile_id in tile_ids]

    logger.debug(f"retrieve_dem: tiles={len(tile_ids)}")

    datasets: List[rasterio.io.DatasetReader] = [] # type: ignore
    try:
        with rasterio.Env(AWS_NO_SIGN_REQUEST="YES"):
            for url in tile_urls:
                try:
                    datasets.append(rasterio.open(url))
                    logger.debug(f"retrieve_dem: opened {url}")
                except RasterioIOError:
                    logger.debug(f"retrieve_dem: missing {url}")
                    continue

            if not datasets:
                raise ValueError("No Copernicus DEM tiles found for the provided bounding box.")

            mosaic, out_trans = merge(datasets, bounds=(min_lon, min_lat, max_lon, max_lat))
            logger.debug(f"retrieve_dem: mosaic shape={mosaic.shape}")

            source_crs = datasets[0].crs
            
            # Reproject to EPSG:3857 (Web Mercator in meters) for viewshed analysis
            source_crs_obj = CRS.from_user_input(source_crs)
            if source_crs_obj.is_geographic:
                logger.debug(f"retrieve_dem: reprojecting from {source_crs} to EPSG:3857")
                target_crs = CRS.from_epsg(3857)
                
                # Calculate source bounds from the merged mosaic transform and shape
                src_height = mosaic.shape[1]
                src_width = mosaic.shape[2]
                minx, miny, maxx, maxy = array_bounds(src_height, src_width, out_trans)

                # Calculate transform and destination size for reprojection
                transform, width, height = rasterio.warp.calculate_default_transform(
                    source_crs, target_crs, src_width, src_height,
                    left=minx, bottom=miny, right=maxx, top=maxy
                )

                # Ensure destination size is defined and convert to ints for numpy
                if width is None or height is None:
                    raise ValueError("Reprojection produced undefined destination width/height")
                width = int(width)
                height = int(height)
                bands = int(mosaic.shape[0])

                # Allocate destination array with the correct shape and dtype
                reprojected = np.zeros((bands, height, width), dtype=mosaic.dtype)

                # Reproject into the correctly sized destination
                rasterio.warp.reproject(
                    source=mosaic[0],
                    destination=reprojected[0],
                    src_transform=out_trans,
                    src_crs=source_crs,
                    dst_transform=transform,
                    dst_crs=target_crs,
                    resampling=Resampling.bilinear
                )

                final_array = reprojected[0]
                final_transform = transform
                final_crs = target_crs
            else:
                final_array = mosaic[0]
                final_transform = out_trans
                final_crs = source_crs

            result: Dict[str, object] = {
                "array": final_array,
                "transform": final_transform,
                "crs": final_crs,
            }

            if write_geotiff:
                tmp_file = tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
                tmp_path = tmp_file.name
                tmp_file.close()

                with rasterio.open(
                    tmp_path,
                    "w",
                    driver="GTiff",
                    height=final_array.shape[0],
                    width=final_array.shape[1],
                    count=1,
                    dtype=final_array.dtype,
                    crs=final_crs,
                    transform=final_transform,
                    nodata=None,
                ) as dst:
                    dst.write(final_array, 1)

                result["path"] = tmp_path
                result["cleanup_paths"] = [tmp_path]

            return result
    finally:
        for dataset in datasets:
            dataset.close()