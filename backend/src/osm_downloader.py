from typing import List, Dict
import requests

def download_railway_lines(bounding_box: Dict[str, float]) -> List[Dict]:
    min_lat = bounding_box['min_lat']
    min_lon = bounding_box['min_lon']
    max_lat = bounding_box['max_lat']
    max_lon = bounding_box['max_lon']
    overpass_url = "http://overpass-api.de/api/interpreter"
    overpass_query = f"""
    [out:json][timeout:25];
    way["railway"="rail"]["usage"~"^(main|branch)$"]({min_lat},{min_lon},{max_lat},{max_lon});
    out geom;
    """
    
    print("download_railway_lines: Downloading railway lines with query:", overpass_query)
    response = requests.get(overpass_url, params={'data': overpass_query})
    print("download_railway_lines: Overpass API response status code:", response.status_code)
    response.raise_for_status()  # Raise an error for bad responses
    data = response.json()
    
    return data.get('elements', [])