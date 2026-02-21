from enum import Enum
from typing import Any, Dict, Iterable, List

from fastapi import FastAPI, HTTPException, APIRouter, Depends
from pydantic import BaseModel, model_validator
from osm_importer import import_ways
from way_sampler import sample_points_along_ways
from dem_retriever import retrieve_dem
from viewshed_analyzer import perform_viewshed_analysis
from db import get_db
from scenicness_saver import save_viewshed_results_to_db

app = FastAPI(
    title="ViewFinder",
    summary="A service that can estimate the best side to sit on, given any route, so you get the best views.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json"
)

router = APIRouter(prefix="/api")


def _viewshed_results_to_geojson(
    results: Iterable[Dict[str, Any]],
) -> Dict[str, Any]:
    features: List[Dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        lat = item.get("lat")
        lon = item.get("lon")
        if lat is None or lon is None:
            continue
        properties = {k: v for k, v in item.items() if k not in {"lat", "lon"}}
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [float(lon), float(lat)],
                },
                "properties": properties,
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
    }

class BoundingBox(BaseModel):
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float

    @model_validator(mode='after')
    def validate_bbox(self):
        if abs(self.max_lat - self.min_lat) > 1:
            raise ValueError('Latitude range must be at most 1 degree')
        if abs(self.max_lon - self.min_lon) > 1:
            raise ValueError('Longitude range must be at most 1 degree')
        return self

class WayType(str, Enum):
    railway = "railway"
    road = "road"
    path = "path"


class ComputeScenicnessRequest(BaseModel):
    bounding_box: BoundingBox = BoundingBox(min_lat=46.215, min_lon=9.368, max_lat=46.888, max_lon=10.335)
    way_type: WayType = WayType.railway

@router.post("/compute-scenicness/")
async def compute_scenicness(
    request: ComputeScenicnessRequest,
    session=Depends(get_db),
):
    try:
        bounding_box = request.bounding_box.model_dump()
        way_type = request.way_type

        await import_ways(session, bounding_box, way_type.value)

        sampled_points = await sample_points_along_ways(session=session, bounding_box=bounding_box)

        dem_data = retrieve_dem(bounding_box, write_geotiff=True)
        viewshed_results = perform_viewshed_analysis(dem_data, sampled_points)
        await save_viewshed_results_to_db(
            viewshed_results,
            session=session,
        )
        #return _viewshed_results_to_geojson(viewshed_results)
        return {"status": "success", "num_viewshed_points": len(viewshed_results)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

app.include_router(router)