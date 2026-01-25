from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Dict, List


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius_m = 6_371_000.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    lat1_rad = radians(lat1)
    lat2_rad = radians(lat2)

    a = sin(dlat / 2) ** 2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon / 2) ** 2
    c = 2 * asin(min(1.0, sqrt(a)))
    return radius_m * c


def _interpolate(lat1: float, lon1: float, lat2: float, lon2: float, fraction: float) -> Dict[str, float]:
    return {
        "lat": lat1 + (lat2 - lat1) * fraction,
        "lon": lon1 + (lon2 - lon1) * fraction,
    }


def _make_sample(
    lat: float,
    lon: float,
    way_id: int | str | None,
    tangent_lat: float,
    tangent_lon: float,
) -> Dict[str, float | int | str | None]:
    return {
        "lat": lat,
        "lon": lon,
        "way_id": way_id,
        "tangent_lat": tangent_lat,
        "tangent_lon": tangent_lon,
    }


def _average_tangent(
    prev_lat: float,
    prev_lon: float,
    next_lat: float | None,
    next_lon: float | None,
) -> tuple[float, float]:
    if next_lat is None or next_lon is None:
        return prev_lat, prev_lon

    avg_lat = prev_lat + next_lat
    avg_lon = prev_lon + next_lon
    if avg_lat == 0 and avg_lon == 0:
        return prev_lat, prev_lon
    return avg_lat, avg_lon


def sample_points_along_railway_lines(
    railway_lines: List[Dict],
    spacing_m: float = 200.0,
) -> List[Dict[str, float | int | str | None]]:
    if spacing_m <= 0:
        raise ValueError("spacing_m must be greater than 0")

    samples: List[Dict[str, float | int | str | None]] = []
    last_sample_lat: float | None = None
    last_sample_lon: float | None = None
    min_spacing_m = spacing_m * 0.9
    connect_tolerance_m = max(1.0, spacing_m * 0.1)

    print("sample_points_along_railway_lines: Sampling points with spacing (m):", spacing_m)

    for line in railway_lines:
        tags = line.get("tags") or {}
        if str(tags.get("tunnel", "")).lower() == "yes":
            continue
        geometry = line.get("geometry")
        if not geometry or len(geometry) < 2:
            continue

        way_id = line.get("id")
        distance_since = 0.0

        for idx in range(len(geometry) - 1):
            start = geometry[idx]
            end = geometry[idx + 1]
            start_lat = float(start["lat"])
            start_lon = float(start["lon"])
            end_lat = float(end["lat"])
            end_lon = float(end["lon"])

            segment_vec_lat = end_lat - start_lat
            segment_vec_lon = end_lon - start_lon
            segment_len = _haversine_m(start_lat, start_lon, end_lat, end_lon)
            if segment_len <= 0:
                continue

            if idx == 0:
                if last_sample_lat is not None and last_sample_lon is not None:
                    dist_from_last = _haversine_m(last_sample_lat, last_sample_lon, start_lat, start_lon)
                    if dist_from_last <= connect_tolerance_m:
                        if dist_from_last < min_spacing_m:
                            distance_since = dist_from_last
                        else:
                            samples.append(
                                _make_sample(
                                    start_lat,
                                    start_lon,
                                    way_id,
                                    segment_vec_lat,
                                    segment_vec_lon,
                                )
                            )
                            last_sample_lat = start_lat
                            last_sample_lon = start_lon
                    else:
                        last_sample_lat = None
                        last_sample_lon = None
                else:
                    samples.append(
                        _make_sample(
                            start_lat,
                            start_lon,
                            way_id,
                            segment_vec_lat,
                            segment_vec_lon,
                        )
                    )
                    last_sample_lat = start_lat
                    last_sample_lon = start_lon

            while distance_since + segment_len >= spacing_m:
                remaining = spacing_m - distance_since
                fraction = remaining / segment_len
                sample = _interpolate(start_lat, start_lon, end_lat, end_lon, fraction)

                tangent_lat = segment_vec_lat
                tangent_lon = segment_vec_lon
                if abs(1.0 - fraction) < 1e-9:
                    next_vec_lat = None
                    next_vec_lon = None
                    if idx + 2 < len(geometry):
                        next_point = geometry[idx + 2]
                        next_vec_lat = float(next_point["lat"]) - end_lat
                        next_vec_lon = float(next_point["lon"]) - end_lon
                    tangent_lat, tangent_lon = _average_tangent(
                        segment_vec_lat,
                        segment_vec_lon,
                        next_vec_lat,
                        next_vec_lon,
                    )

                samples.append(
                    _make_sample(
                        float(sample["lat"]),
                        float(sample["lon"]),
                        way_id,
                        tangent_lat,
                        tangent_lon,
                    )
                )
                last_sample_lat = float(sample["lat"])
                last_sample_lon = float(sample["lon"])

                start_lat = float(sample["lat"])
                start_lon = float(sample["lon"])
                segment_vec_lat = end_lat - start_lat
                segment_vec_lon = end_lon - start_lon
                segment_len = _haversine_m(start_lat, start_lon, end_lat, end_lon)
                distance_since = 0.0

                if segment_len <= 0:
                    break

            distance_since += segment_len

    print("sample_points_along_railway_lines: Total samples generated:", len(samples))
    return samples
