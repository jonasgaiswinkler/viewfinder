from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, cast
import math
import os
import shutil
import subprocess
import tempfile

import numpy as np
import rasterio
from pyproj import CRS, Transformer
from concurrent.futures import ThreadPoolExecutor, as_completed

def perform_viewshed_analysis(
    railway_lines: List[Dict],
    dem_data: Dict[str, object],
    sampled_points: Optional[Sequence[Dict[str, float | int | str | None]]] = None,
    max_workers: Optional[int] = None,
):
    if not sampled_points:
        array = dem_data["array"]
        if isinstance(array, np.ndarray):
            return np.zeros(array.shape, dtype=np.uint16)
        raise ValueError("DEM array missing or invalid.")

    dem_array = dem_data.get("array")
    dem_transform = dem_data.get("transform")
    dem_crs = dem_data.get("crs")
    dem_path = dem_data.get("path")
    cleanup_paths = dem_data.get("cleanup_paths")

    if dem_path is None and (not isinstance(dem_array, np.ndarray) or dem_transform is None or dem_crs is None):
        raise ValueError("DEM data must include 'array', 'transform', and 'crs', or provide a 'path'.")

    grass_cmd = _resolve_grass_command()
    utm_crs, transformer = _utm_crs_for_points(sampled_points)

    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            grass_db = os.path.join(tmp_dir, "grassdata")
            location_dir = os.path.join(grass_db, "viewshed")
            os.makedirs(grass_db, exist_ok=True)

            _run_grass(grass_cmd, ["-c", f"epsg:{utm_crs.to_epsg()}", "-e", location_dir])

            if isinstance(dem_path, str):
                dem_path_to_use = dem_path
            else:
                if not isinstance(dem_array, np.ndarray):
                    raise ValueError("DEM array missing or invalid.")
                dem_path_to_use = os.path.join(tmp_dir, "dem.tif")
                _write_dem_geotiff(dem_path_to_use, cast(np.ndarray, dem_array), dem_transform, dem_crs)

            mapset = os.path.join(location_dir, "PERMANENT")
            _run_grass(grass_cmd, [mapset, "--exec", "r.import", f"input={dem_path_to_use}", "output=dem", "--overwrite"])

            tasks: List[Tuple[str, float, float, str, Dict[str, float | int | str | None]]] = []
            for idx, point in enumerate(sampled_points):
                lat = point.get("lat") if isinstance(point, dict) else None
                lon = point.get("lon") if isinstance(point, dict) else None
                if lat is None or lon is None:
                    continue

                easting, northing = transformer.transform(float(lon), float(lat))
                mapset_name = f"viewshed_{idx}"
                view_name = f"viewshed_{idx}"
                _run_grass(grass_cmd, [mapset, "--exec", "g.mapset", "-c", f"mapset={mapset_name}"])
                mapset_path = os.path.join(location_dir, mapset_name)
                tasks.append((mapset_path, easting, northing, view_name, point))

            if not tasks:
                if isinstance(dem_array, np.ndarray):
                    return np.zeros(dem_array.shape, dtype=np.uint16)
                raise ValueError("DEM array missing or invalid.")

            worker_count = max_workers or min(os.cpu_count() or 1, len(tasks))
            print(f"Viewshed tasks: {len(tasks)}, workers: {worker_count}")

            def _process_task(
                mapset_path: str,
                easting: float,
                northing: float,
                view_name: str,
                point: Dict[str, float | int | str | None],
            ) -> Dict[str, object]:
                _run_viewshed_task(grass_cmd, mapset_path, easting, northing, view_name)
                out_path = os.path.join(tmp_dir, f"{view_name}.tif")
                _export_viewshed(grass_cmd, mapset_path, view_name, out_path)

                tangent = _tangent_vector_for_sample(point, transformer)
                split = _split_viewshed_by_tangent(out_path, easting, northing, tangent)
                left_area = split.get("left_area")
                right_area = split.get("right_area")
                total_left_area = split.get("total_left_area")
                total_right_area = split.get("total_right_area")
                left_area_value = float(left_area) if isinstance(left_area, (int, float)) else 0.0
                right_area_value = float(right_area) if isinstance(right_area, (int, float)) else 0.0
                total_left_value = float(total_left_area) if isinstance(total_left_area, (int, float)) else 0.0
                total_right_value = float(total_right_area) if isinstance(total_right_area, (int, float)) else 0.0
                if total_left_value <= 0:
                    total_left_value = left_area_value
                if total_right_value <= 0:
                    total_right_value = right_area_value
                if total_left_value > 0:
                    left_area_norm = left_area_value / total_left_value
                else:
                    left_area_norm = 0.0
                if total_right_value > 0:
                    right_area_norm = right_area_value / total_right_value
                else:
                    right_area_norm = 0.0
                lat_value = point.get("lat") if isinstance(point, dict) else None
                lon_value = point.get("lon") if isinstance(point, dict) else None
                if tangent is None:
                    tangent_dx = None
                    tangent_dy = None
                    tangent_azimuth = None
                else:
                    tangent_dx, tangent_dy = tangent
                    tangent_azimuth = (math.degrees(math.atan2(tangent_dx, tangent_dy)) + 360.0) % 360.0
                return {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [lon_value, lat_value],
                    },
                    "properties": {
                        "lat": lat_value,
                        "lon": lon_value,
                        "way_id": point.get("way_id") if isinstance(point, dict) else None,
                        "left_area": left_area_norm,
                        "right_area": right_area_norm,
                        "tangent_dx": tangent_dx,
                        "tangent_dy": tangent_dy,
                        "tangent_azimuth_deg": tangent_azimuth,
                    },
                }

            results: List[Dict[str, object]] = []
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                futures = [
                    executor.submit(_process_task, mapset_path, easting, northing, view_name, point)
                    for mapset_path, easting, northing, view_name, point in tasks
                ]
                for future in as_completed(futures):
                    results.append(future.result())

            return {
                "type": "FeatureCollection",
                "features": results,
            }
    finally:
        if isinstance(cleanup_paths, list):
            for path in cleanup_paths:
                if isinstance(path, str) and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


def _resolve_grass_command() -> str:
    grass_bin = os.environ.get("GRASSBIN")
    if grass_bin:
        return grass_bin
    grass_cmd = shutil.which("grass") or shutil.which("grass84") or shutil.which("grass83")
    if not grass_cmd:
        raise RuntimeError("GRASS GIS executable not found. Set GRASSBIN or add GRASS to PATH.")
    return grass_cmd


def _utm_crs_for_points(points: Sequence[Dict[str, float | int | str | None]]) -> Tuple[CRS, Transformer]:
    lats = []
    lons = []
    for point in points:
        if not isinstance(point, dict):
            continue
        lat = point.get("lat")
        lon = point.get("lon")
        if lat is None or lon is None:
            continue
        lats.append(float(lat))
        lons.append(float(lon))

    if not lats or not lons:
        raise ValueError("No valid sampled points to compute UTM zone.")

    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)
    zone = int((center_lon + 180) / 6) + 1
    epsg = 32600 + zone if center_lat >= 0 else 32700 + zone
    utm_crs = CRS.from_epsg(epsg)
    transformer = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True)
    return utm_crs, transformer


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


def _run_grass(grass_cmd: str, args: List[str]) -> None:
    completed = subprocess.run(
        [grass_cmd, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"GRASS command failed: {' '.join(args)}\n{message}")


def _run_viewshed_task(grass_cmd: str, mapset_path: str, easting: float, northing: float, view_name: str) -> None:
    print(f"Running viewshed {view_name} at {easting},{northing}")
    _run_grass(grass_cmd, [mapset_path, "--exec", "g.region", "raster=dem@PERMANENT"])
    _run_grass(
        grass_cmd,
        [
            mapset_path,
            "--exec",
            "r.viewshed",
            "input=dem@PERMANENT",
            f"output={view_name}",
            f"coordinates={easting},{northing}",
            "observer_elevation=2.5",
            "target_elevation=0.0",
            "max_distance=25000",
            "--overwrite",
            "-b",
            "-c",
            "-r"
        ],
    )
    print(f"Finished viewshed {view_name}")


def _export_viewshed(grass_cmd: str, mapset_path: str, view_name: str, out_path: str) -> None:
    _run_grass(
        grass_cmd,
        [
            mapset_path,
            "--exec",
            "r.out.gdal",
            f"input={view_name}",
            f"output={out_path}",
            "format=GTiff",
            "createopt=COMPRESS=DEFLATE",
            "--overwrite",
        ],
    )


def _tangent_vector_for_sample(
    sample: Dict[str, float | int | str | None],
    transformer: Transformer,
) -> Optional[Tuple[float, float]]:
    if not isinstance(sample, dict):
        return None

    lat = sample.get("lat")
    lon = sample.get("lon")
    tangent_lat = sample.get("tangent_lat")
    tangent_lon = sample.get("tangent_lon")
    if lat is None or lon is None or tangent_lat is None or tangent_lon is None:
        return None

    tlat = float(tangent_lat)
    tlon = float(tangent_lon)
    if tlat == 0 and tlon == 0:
        return None

    base_x, base_y = transformer.transform(float(lon), float(lat))
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
        valid_mask = src.read_masks(1) > 0

    if data.dtype == np.uint8:
        data = np.where(data == 255, 0, data)

    pixel_area = abs(transform.a * transform.e - transform.b * transform.d)

    visible_mask = (data > 0) & valid_mask
    rows_all, cols_all = np.indices(data.shape)
    xs_all = transform.c + transform.a * cols_all + transform.b * rows_all
    ys_all = transform.f + transform.d * cols_all + transform.e * rows_all
    vx_all = xs_all - observer_easting
    vy_all = ys_all - observer_northing
    cross_all = tx * vy_all - ty * vx_all
    total_left_area = float(np.count_nonzero((cross_all >= 0) & valid_mask) * pixel_area)
    total_right_area = float(np.count_nonzero((cross_all < 0) & valid_mask) * pixel_area)

    if not np.any(visible_mask):
        return {
            "left_viewshed": np.zeros_like(data),
            "right_viewshed": np.zeros_like(data),
            "viewshed": data,
            "transform": transform,
            "crs": crs,
            "left_area": 0.0,
            "right_area": 0.0,
            "total_left_area": total_left_area,
            "total_right_area": total_right_area,
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

    left_area = float(np.count_nonzero(left_mask) * pixel_area)
    right_area = float(np.count_nonzero(right_mask) * pixel_area)

    return {
        "left_viewshed": left_viewshed,
        "right_viewshed": right_viewshed,
        "viewshed": data,
        "transform": transform,
        "crs": crs,
        "left_area": left_area,
        "right_area": right_area,
        "total_left_area": total_left_area,
        "total_right_area": total_right_area,
    }