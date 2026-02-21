from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from fastapi import FastAPI, HTTPException, APIRouter
from pydantic import BaseModel, model_validator

from job_manager import job_manager, JobInfo


# ---------------------------------------------------------------------------
# Lifespan – start / stop the background job worker
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    job_manager.start()
    yield


app = FastAPI(
    title="ViewFinder",
    summary="A service that can estimate the best side to sit on, given any route, so you get the best views.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/jobs/", response_model=JobInfo, status_code=202)
async def create_job(request: ComputeScenicnessRequest):
    """Create a new scenicness computation job. Returns immediately with the job info."""
    job = job_manager.create_job(
        params={
            "bounding_box": request.bounding_box.model_dump(),
            "way_type": request.way_type.value,
        }
    )
    return job


@router.get("/jobs/", response_model=List[JobInfo])
async def list_jobs():
    """Return all jobs (queued, running, completed, failed)."""
    return job_manager.list_jobs()


@router.get("/jobs/{job_id}", response_model=JobInfo)
async def get_job(job_id: str):
    """Return the status and details of a single job."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


app.include_router(router)