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


def _get_tile_size_km() -> float:
    val = os.getenv("VIEWFINDER_TILE_SIZE_KM", "50")
    try:
        return float(val)
    except Exception:
        return 50.0

# Public constant for bounding-box tile size (km). Can be set via
# environment variable `VIEWFINDER_TILE_SIZE_KM` (default: 50)
TILE_SIZE_KM = _get_tile_size_km()

def _get_overpass_max_retries() -> int:
    val = os.getenv("VIEWFINDER_OVERPASS_MAX_RETRIES", "5")
    try:
        return int(val)
    except Exception:
        return 5

# Public constant for Overpass API max retries. Can be set via
# environment variable `VIEWFINDER_OVERPASS_MAX_RETRIES` (default: 5)
OVERPASS_MAX_RETRIES = _get_overpass_max_retries()

def _get_overpass_request_delay() -> int:
    val = os.getenv("VIEWFINDER_OVERPASS_REQUEST_DELAY", "15")
    try:
        return int(val)
    except Exception:
        return 15

# Public constant for Overpass API request delay. Can be set via
# environment variable `VIEWFINDER_OVERPASS_REQUEST_DELAY` (default: 15)
OVERPASS_REQUEST_DELAY = _get_overpass_request_delay()

def _get_overpass_timeout() -> int:
    val = os.getenv("VIEWFINDER_OVERPASS_TIMEOUT", "60")
    try:
        return int(val)
    except Exception:
        return 60

# Public constant for Overpass API timeout. Can be set via
# environment variable `VIEWFINDER_OVERPASS_TIMEOUT` (default: 60)
OVERPASS_TIMEOUT = _get_overpass_timeout()
