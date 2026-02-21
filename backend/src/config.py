import os

def _get_range_km() -> float:
    val = os.getenv("VIEWFINDER_RANGE_KM", "25")
    try:
        return float(val)
    except Exception:
        return 25.0

# Public constant used across the project. Can be set with the
# environment variable `VIEWFINDER_RANGE_KM` (default: 25)
RANGE_KM = _get_range_km()


def _get_elevation_normalize() -> float:
    val = os.getenv("VIEWFINDER_ELEVATION_NORMALIZE", "9000")
    try:
        return float(val)
    except Exception:
        return 9000.0

# Public constant for elevation normalization used across the project.
ELEVATION_NORMALIZE = _get_elevation_normalize()


def _get_sampling_spacing_m() -> float:
    val = os.getenv("VIEWFINDER_SAMPLING_SPACING_M", "200")
    try:
        return float(val)
    except Exception:
        return 200.0

# Public constant for sample spacing (meters). Can be set via
# environment variable `VIEWFINDER_SAMPLING_SPACING_M` (default: 200)
SAMPLING_SPACING_M = _get_sampling_spacing_m()
