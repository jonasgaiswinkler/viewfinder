from fastapi import FastAPI, HTTPException, APIRouter
from pydantic import BaseModel, model_validator
from osm_downloader import download_railway_lines
from railway_sampler import sample_points_along_railway_lines
from dem_retriever import retrieve_dem
from viewshed_analyzer import perform_viewshed_analysis

app = FastAPI(
    title="ViewFinder",
    summary="A service that can estimate the best side to sit on, given any route, so you get the best views.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json"
)

router = APIRouter(prefix="/api")

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

class ComputeScenicnessRequest(BaseModel):
    bounding_box: BoundingBox

@router.post("/compute-scenicness/")
def compute_scenicness(request: ComputeScenicnessRequest):
    try:
        bbox = request.bounding_box.model_dump()
        railway_lines = download_railway_lines(bbox)
        sampled_points = sample_points_along_railway_lines(railway_lines)
        dem_data = retrieve_dem(bbox, write_geotiff=True)
        viewshed_results = perform_viewshed_analysis(railway_lines, dem_data, sampled_points)
        return viewshed_results
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

app.include_router(router)