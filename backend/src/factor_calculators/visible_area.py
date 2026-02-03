from __future__ import annotations

from typing import Dict, Optional, Tuple, cast

import numpy as np
from affine import Affine


def calculate_visible_area_factors(
	left_area_value: float,
	right_area_value: float,
	total_left_area_value: float,
	total_right_area_value: float,
) -> Dict[str, float]:
	if total_left_area_value <= 0:
		total_left_area_value = left_area_value
	if total_right_area_value <= 0:
		total_right_area_value = right_area_value

	if total_left_area_value > 0:
		left_value = left_area_value / total_left_area_value
	else:
		left_value = 0.0
	if total_right_area_value > 0:
		right_value = right_area_value / total_right_area_value
	else:
		right_value = 0.0

	total_value = (left_value + right_value) / 2.0
	relative_value = right_value - left_value

	return {
		"total_value": total_value,
		"left_value": left_value,
		"right_value": right_value,
		"relative_value": relative_value,
	}


def visible_area_factors_from_split(split: Dict[str, object]) -> Dict[str, float]:
	left_mask = split.get("left_mask") if isinstance(split, dict) else None
	right_mask = split.get("right_mask") if isinstance(split, dict) else None
	viewshed = split.get("viewshed") if isinstance(split, dict) else None
	transform = split.get("transform") if isinstance(split, dict) else None
	nodata = split.get("nodata") if isinstance(split, dict) else None
	observer_easting = split.get("observer_easting") if isinstance(split, dict) else None
	observer_northing = split.get("observer_northing") if isinstance(split, dict) else None
	tangent = split.get("tangent") if isinstance(split, dict) else None
	pixel_area_value: Optional[float] = None
	if isinstance(transform, Affine):
		try:
			pixel_area_value = abs(transform.a * transform.e - transform.b * transform.d)
		except Exception:
			pixel_area_value = None

	left_area_value = 0.0
	right_area_value = 0.0
	if isinstance(pixel_area_value, (int, float)):
		if isinstance(left_mask, np.ndarray):
			try:
				left_area_value = float(np.count_nonzero(cast(np.ndarray, left_mask)) * pixel_area_value)
			except Exception:
				left_area_value = 0.0
		if isinstance(right_mask, np.ndarray):
			try:
				right_area_value = float(np.count_nonzero(cast(np.ndarray, right_mask)) * pixel_area_value)
			except Exception:
				right_area_value = 0.0

	total_left_area_value = 0.0
	total_right_area_value = 0.0
	if (
		isinstance(pixel_area_value, (int, float))
		and isinstance(viewshed, np.ndarray)
		and isinstance(transform, Affine)
		and isinstance(observer_easting, (int, float))
		and isinstance(observer_northing, (int, float))
		and isinstance(tangent, tuple)
		and len(tangent) == 2
	):
		tx, ty = cast(Tuple[float, float], tangent)
		if nodata is None or (isinstance(nodata, (int, float)) and nodata == 0):
			total_mask = np.ones_like(viewshed, dtype=bool)
		else:
			total_mask = viewshed != nodata
		rows_all, cols_all = np.indices(viewshed.shape)
		xs_all = transform.c + transform.a * cols_all + transform.b * rows_all
		ys_all = transform.f + transform.d * cols_all + transform.e * rows_all
		vx_all = xs_all - float(observer_easting)
		vy_all = ys_all - float(observer_northing)
		cross_all = tx * vy_all - ty * vx_all
		total_left_area_value = float(
			np.count_nonzero((cross_all >= 0) & total_mask) * pixel_area_value
		)
		total_right_area_value = float(
			np.count_nonzero((cross_all < 0) & total_mask) * pixel_area_value
		)

	return calculate_visible_area_factors(
		left_area_value,
		right_area_value,
		total_left_area_value,
		total_right_area_value,
	)
