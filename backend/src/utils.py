from typing import List, Dict, Any

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