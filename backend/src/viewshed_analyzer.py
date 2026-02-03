from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, cast
import os
import tempfile

import numpy as np
import rasterio
from pyproj import CRS, Transformer, Geod
from concurrent.futures import ThreadPoolExecutor, as_completed
from osgeo import gdal

from factor_calculators.visible_area import visible_area_factors_from_split
from factor_calculators.elevation_difference import elevation_difference_factors_from_split

def perform_viewshed_analysis(
    dem_data: Dict[str, object],
    sampled_points: Optional[Sequence[Dict[str, float | int | str | None]]] = None,
    max_workers: Optional[int] = None,
):
    print("Starting viewshed analysis", flush=True)
    if not sampled_points:
        array = dem_data["array"]
        if isinstance(array, np.ndarray):
            return np.zeros(array.shape, dtype=np.uint16)
        raise ValueError("DEM array missing or invalid.")

    dem_array = dem_data.get("array")
    dem_transform = dem_data.get("transform")
    dem_crs = dem_data.get("crs")
    dem_path = cast(Optional[str], dem_data.get("path"))
    cleanup_paths = dem_data.get("cleanup_paths")

    if dem_path is None and (not isinstance(dem_array, np.ndarray) or dem_transform is None or dem_crs is None):
        raise ValueError("DEM data must include 'array', 'transform', and 'crs', or provide a 'path'.")

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            dem_path_to_use, dem_transform, dem_crs = _prepare_dem_data(
                tmp_dir,
                dem_path,
                dem_array,
                dem_transform,
                dem_crs,
            )
            if dem_crs is None:
                raise ValueError("DEM CRS missing or invalid.")
            transformer = Transformer.from_crs("EPSG:4326", dem_crs, always_xy=True)

            dem_array_for_factors: Optional[np.ndarray] = None
            dem_nodata: Optional[float] = None
            if isinstance(dem_array, np.ndarray):
                dem_array_for_factors = dem_array
            else:
                try:
                    with rasterio.open(dem_path_to_use) as src:
                        dem_array_for_factors = src.read(1)
                        dem_nodata = src.nodata
                except Exception:
                    dem_array_for_factors = None

            tasks: List[Tuple[float, float, str, Dict[str, float | int | str | None]]] = []
            for idx, point in enumerate(sampled_points):
                lat = point.get("lat") if isinstance(point, dict) else None
                lon = point.get("lon") if isinstance(point, dict) else None
                if lat is None or lon is None:
                    continue

                easting, northing = transformer.transform(float(lon), float(lat))
                view_name = f"viewshed_{idx}"
                tasks.append((easting, northing, view_name, point))

            if not tasks:
                if isinstance(dem_array, np.ndarray):
                    return np.zeros(dem_array.shape, dtype=np.uint16)
                raise ValueError("DEM array missing or invalid.")

            worker_count = max_workers or min(os.cpu_count() or 1, len(tasks))
            print(f"Viewshed tasks: {len(tasks)}, workers: {worker_count}", flush=True)

            def _process_task(
                easting: float,
                northing: float,
                view_name: str,
                point: Dict[str, float | int | str | None],
            ) -> Dict[str, object]:
                out_path = os.path.join(tmp_dir, f"{view_name}.tif")
                _run_viewshed_task_gdal(dem_path_to_use, out_path, easting, northing)

                tangent = _tangent_vector_for_sample(point, transformer)
                split = _split_viewshed_by_tangent(out_path, easting, northing, tangent)
                lat_value = point.get("lat") if isinstance(point, dict) else None
                lon_value = point.get("lon") if isinstance(point, dict) else None
                tangent_deg_4326 = point.get("tangent_deg_4326") if isinstance(point, dict) else None
                tangent_deg_3857 = point.get("tangent_deg_3857") if isinstance(point, dict) else None
                bridge_value = point.get("bridge") if isinstance(point, dict) else None
                tangent_dx = point.get("tangent_dx") if isinstance(point, dict) else None
                tangent_dy = point.get("tangent_dy") if isinstance(point, dict) else None
                if tangent_dx is None or tangent_dy is None:
                    if tangent is None:
                        tangent_dx = None
                        tangent_dy = None
                    else:
                        tangent_dx, tangent_dy = tangent

                visible_area_factors = visible_area_factors_from_split(split)
                elevation_difference_factors = elevation_difference_factors_from_split(
                    {
                        **split,
                        "dem_array": dem_array_for_factors,
                        "dem_nodata": dem_nodata,
                    }
                )

                return {
                    "lat": lat_value,
                    "lon": lon_value,
                    "bridge": bridge_value,
                    "tangent_dx": tangent_dx,
                    "tangent_dy": tangent_dy,
                    "tangent_deg_4326": tangent_deg_4326,
                    "tangent_deg_3857": tangent_deg_3857,
                    "factors": {
                        "visible_area": visible_area_factors,
                        "elevation_difference": elevation_difference_factors,
                    },
                }

            results: List[Dict[str, object]] = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(_process_task, easting, northing, view_name, point)
                    for easting, northing, view_name, point in tasks
                ]
                for future in as_completed(futures):
                    results.append(future.result())

            return results
    finally:
        if isinstance(cleanup_paths, list):
            for path in cleanup_paths:
                if isinstance(path, str) and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


def _prepare_dem_data(
    tmp_dir: str,
    dem_path: Optional[str],
    dem_array: Optional[object],
    dem_transform,
    dem_crs,
) -> Tuple[str, object, CRS]:
    if isinstance(dem_path, str):
        with rasterio.open(dem_path) as src:
            transform = src.transform
            crs = src.crs
        if crs is None:
            raise ValueError("DEM CRS missing or invalid.")
        return dem_path, transform, CRS.from_user_input(crs)

    if not isinstance(dem_array, np.ndarray):
        raise ValueError("DEM array missing or invalid.")
    if dem_transform is None or dem_crs is None:
        raise ValueError("DEM transform or CRS missing or invalid.")

    dem_path_to_use = os.path.join(tmp_dir, "dem.tif")
    _write_dem_geotiff(dem_path_to_use, cast(np.ndarray, dem_array), dem_transform, dem_crs)
    return dem_path_to_use, dem_transform, CRS.from_user_input(dem_crs)


def _write_dem_geotiff(path: str, array: np.ndarray, transform, crs) -> None:
    if not isinstance(array, np.ndarray):
        raise ValueError("DEM array missing or invalid.")

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=array.shape[0],
        width=array.shape[1],
        count=1,
        dtype=array.dtype,
        crs=crs,
        transform=transform,
        nodata=None,
    ) as dst:
        dst.write(array, 1)


def _run_viewshed_task_gdal(dem_path: str, out_path: str, easting: float, northing: float) -> None:
    #print(f"Running viewshed at {easting},{northing}", flush=True)
    src_ds = gdal.Open(dem_path, gdal.GA_ReadOnly)
    if src_ds is None:
        raise RuntimeError(f"Failed to open DEM for viewshed: {dem_path}")
    band = src_ds.GetRasterBand(1)
    if band is None:
        raise RuntimeError("DEM raster band missing.")

    gdal.ViewshedGenerate(
        band,
        "GTiff",
        out_path,
        ["COMPRESS=DEFLATE"],
        float(easting),
        float(northing),
        2.5,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.85714,
        gdal.GVM_Edge,
        10000.0,
    )
    src_ds = None
    #print("Finished viewshed", flush=True)


def _tangent_vector_for_sample(
    sample: Dict[str, float | int | str | None],
    transformer: Transformer,
) -> Optional[Tuple[float, float]]:
    if not isinstance(sample, dict):
        return None

    lat = sample.get("lat")
    lon = sample.get("lon")
    tangent_dx = sample.get("tangent_dx")
    tangent_dy = sample.get("tangent_dy")
    tangent_deg_4326 = sample.get("tangent_deg_4326")
    tangent_lat = sample.get("tangent_lat")
    tangent_lon = sample.get("tangent_lon")
    if lat is None or lon is None:
        return None

    base_x, base_y = transformer.transform(float(lon), float(lat))

    if tangent_dx is not None and tangent_dy is not None:
        dx = float(tangent_dx)
        dy = float(tangent_dy)
        if dx == 0 and dy == 0:
            return None
        return (dx, dy)

    if tangent_deg_4326 is not None:
        azimuth = float(tangent_deg_4326)
        geod = Geod(ellps="WGS84")
        step_distance_m = 10.0
        step_lon, step_lat, _ = geod.fwd(float(lon), float(lat), azimuth, step_distance_m)
        step_x, step_y = transformer.transform(step_lon, step_lat)
        dx = step_x - base_x
        dy = step_y - base_y
        if dx == 0 and dy == 0:
            return None
        return (dx, dy)

    if tangent_lat is None or tangent_lon is None:
        return None

    tlat = float(tangent_lat)
    tlon = float(tangent_lon)
    if tlat == 0 and tlon == 0:
        return None

    epsilon = 1.0e-5
    norm = (tlat * tlat + tlon * tlon) ** 0.5
    step_lat = float(lat) + (tlat / norm) * epsilon
    step_lon = float(lon) + (tlon / norm) * epsilon
    step_x, step_y = transformer.transform(step_lon, step_lat)
    dx = step_x - base_x
    dy = step_y - base_y
    if dx == 0 and dy == 0:
        return None
    return (dx, dy)


def _split_viewshed_by_tangent(
    raster_path: str,
    observer_easting: float,
    observer_northing: float,
    tangent: Optional[Tuple[float, float]],
) -> Dict[str, object]:
    if tangent is None:
        return {
            "left_viewshed": None,
            "right_viewshed": None,
            "viewshed": None,
            "transform": None,
            "crs": None,
        }

    tx, ty = tangent
    with rasterio.open(raster_path) as src:
        data = src.read(1)
        transform = src.transform
        crs = src.crs
        nodata = src.nodata

    if nodata is None or (isinstance(nodata, (int, float)) and nodata == 0):
        total_mask = np.ones_like(data, dtype=bool)
    else:
        total_mask = data != nodata

    visible_mask = (data > 0) & total_mask
    if not np.any(visible_mask):
        left_mask = np.zeros_like(visible_mask, dtype=bool)
        right_mask = np.zeros_like(visible_mask, dtype=bool)
        return {
            "left_viewshed": np.zeros_like(data),
            "right_viewshed": np.zeros_like(data),
            "viewshed": data,
            "transform": transform,
            "crs": crs,
            "nodata": nodata,
            "observer_easting": observer_easting,
            "observer_northing": observer_northing,
            "tangent": tangent,
            "left_mask": left_mask,
            "right_mask": right_mask,
        }

    rows, cols = np.nonzero(visible_mask)
    xs = transform.c + transform.a * cols + transform.b * rows
    ys = transform.f + transform.d * cols + transform.e * rows
    vx = xs - observer_easting
    vy = ys - observer_northing
    cross = tx * vy - ty * vx

    left_mask = np.zeros_like(visible_mask, dtype=bool)
    right_mask = np.zeros_like(visible_mask, dtype=bool)

    left_mask[rows, cols] = cross >= 0
    right_mask[rows, cols] = cross < 0

    left_viewshed = np.where(left_mask, data, 0)
    right_viewshed = np.where(right_mask, data, 0)

    return {
        "left_viewshed": left_viewshed,
        "right_viewshed": right_viewshed,
        "viewshed": data,
        "transform": transform,
        "crs": crs,
        "nodata": nodata,
        "observer_easting": observer_easting,
        "observer_northing": observer_northing,
        "tangent": tangent,
        "left_mask": left_mask,
        "right_mask": right_mask,
    }


