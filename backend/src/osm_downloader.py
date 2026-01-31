from typing import List, Dict
import requests

def download_ways(bounding_box: Dict[str, float], way_type: str) -> List[Dict]:
    min_lat = bounding_box['min_lat']
    min_lon = bounding_box['min_lon']
    max_lat = bounding_box['max_lat']
    max_lon = bounding_box['max_lon']

    if way_type != "railway":
        raise NotImplementedError(f"Way type '{way_type}' is not implemented yet.")
    else:
        way_selector = '["railway"~"^(rail|narrow_gauge)$"]["service"!~"^(yard|siding|spur|crossover)$"]'

    overpass_url = "http://overpass-api.de/api/interpreter"
    overpass_query = f"""
    [out:json][timeout:25];
    way{way_selector}({min_lat},{min_lon},{max_lat},{max_lon});
    out geom;
    """
    
    print("download_ways: Downloading ways with query:", overpass_query)
    response = requests.get(overpass_url, params={'data': overpass_query})
    print("download_ways: Overpass API response status code:", response.status_code)
    response.raise_for_status()  # Raise an error for bad responses
    data = response.json()
    
    return data.get('elements', [])