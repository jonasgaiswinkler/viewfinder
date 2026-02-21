import math
from typing import List, Dict, Any


def split_bounding_box(bounding_box: Dict[str, float], tile_size_km: float = 75.0) -> List[Dict[str, float]]:
    """Split a bounding box into smaller tiles of approximately *tile_size_km* × *tile_size_km*.

    Returns a list of bounding-box dicts with the same keys as the input.
    """
    min_lat = bounding_box["min_lat"]
    min_lon = bounding_box["min_lon"]
    max_lat = bounding_box["max_lat"]
    max_lon = bounding_box["max_lon"]

    # Approximate degree sizes in km
    km_per_deg_lat = 111.32
    mid_lat = math.radians((min_lat + max_lat) / 2)
    km_per_deg_lon = 111.32 * math.cos(mid_lat)

    step_lat = tile_size_km / km_per_deg_lat
    step_lon = tile_size_km / km_per_deg_lon if km_per_deg_lon > 0 else tile_size_km / 111.32

    tiles: List[Dict[str, float]] = []
    lat = min_lat
    while lat < max_lat:
        lon = min_lon
        tile_max_lat = min(lat + step_lat, max_lat)
        while lon < max_lon:
            tile_max_lon = min(lon + step_lon, max_lon)
            tiles.append({
                "min_lat": lat,
                "min_lon": lon,
                "max_lat": tile_max_lat,
                "max_lon": tile_max_lon,
            })
            lon = tile_max_lon
        lat = tile_max_lat

    return tiles


def validate_bounding_box(bounding_box: List[float]) -> bool:
    if len(bounding_box) != 4:
        return False
    min_lon, min_lat, max_lon, max_lat = bounding_box
    return (min_lon < max_lon) and (min_lat < max_lat)

def process_geojson(geojson_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    features = geojson_data.get("features", [])
    processed_features = []
    for feature in features:
        properties = feature.get("properties", {})
        geometry = feature.get("geometry", {})
        processed_features.append({
            "type": "Feature",
            "properties": properties,
            "geometry": geometry
        })
    return processed_features

def extract_coordinates(geometry: Dict[str, Any]) -> List[float]:
    if geometry["type"] == "LineString":
        return [coord for point in geometry["coordinates"] for coord in point]
    return []