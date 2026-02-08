from __future__ import annotations

from typing import Dict, Optional, Tuple, cast

import numpy as np


def calculate_elevation_factors(
        left_diff: float,
        right_diff: float,
        normalize_by: float,
) -> Dict[str, float]:
    if normalize_by <= 0:
        normalize_by = 1.0

    left_value = float(left_diff) / float(normalize_by)
    right_value = float(right_diff) / float(normalize_by)

    relative_value = right_value - left_value

    return {
        "left_value": left_value,
        "right_value": right_value,
        "relative_value": relative_value,
    }


def _masked_min_max(
        values: np.ndarray,
        mask: np.ndarray,
        *,
        nodata: Optional[float],
) -> Tuple[Optional[float], Optional[float]]:
    if values.size == 0:
        return None, None
    if mask.shape != values.shape:
        return None, None

    valid_mask = mask
    if nodata is None or (isinstance(nodata, (int, float)) and nodata == 0):
        pass
    else:
        valid_mask = mask & (values != nodata)

    if not np.any(valid_mask):
        return None, None

    masked_values = values[valid_mask]
    try:
        return float(np.min(masked_values)), float(np.max(masked_values))
    except Exception:
        return None, None


def elevation_factors_from_split(
        split: Dict[str, object],
        *,
        normalize_by: float = 9000.0,
) -> Dict[str, Dict[str, float]]:
    left_mask = split.get("left_mask") if isinstance(split, dict) else None
    right_mask = split.get("right_mask") if isinstance(split, dict) else None
    viewshed = split.get("viewshed") if isinstance(split, dict) else None
    dem_array = split.get("dem_array") if isinstance(split, dict) else None
    nodata = split.get("nodata") if isinstance(split, dict) else None
    dem_nodata = split.get("dem_nodata") if isinstance(split, dict) else None

    if not isinstance(left_mask, np.ndarray) or not isinstance(right_mask, np.ndarray):
        return {"elevation_difference":
                calculate_elevation_factors(0.0, 0.0, normalize_by),
                "max_elevation": calculate_elevation_factors(0.0, 0.0, normalize_by)}

    if isinstance(dem_array, np.ndarray):
        values = dem_array
        values_nodata = dem_nodata
    elif isinstance(viewshed, np.ndarray):
        values = viewshed
        values_nodata = nodata
    else:
        return {"elevation_difference":
                calculate_elevation_factors(0.0, 0.0, normalize_by),
                "max_elevation": calculate_elevation_factors(0.0, 0.0, normalize_by)}

    left_min, left_max = _masked_min_max(values, cast(
        np.ndarray, left_mask), nodata=cast(Optional[float], values_nodata))
    right_min, right_max = _masked_min_max(values, cast(
        np.ndarray, right_mask), nodata=cast(Optional[float], values_nodata))

    left_diff = (
        left_max - left_min) if left_min is not None and left_max is not None else 0.0
    right_diff = (
        right_max - right_min) if right_min is not None and right_max is not None else 0.0

    return {"elevation_difference": calculate_elevation_factors(left_diff, right_diff, normalize_by),
            "max_elevation": calculate_elevation_factors(
        left_max if left_max is not None else 0.0,
        right_max if right_max is not None else 0.0,
        normalize_by)}
