import os
import secrets
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from fastapi import Depends, FastAPI, HTTPException, APIRouter
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, model_validator

from job_manager import job_manager, JobInfo

# ---------------------------------------------------------------------------
# Basic-auth dependency (credentials from env vars)
# ---------------------------------------------------------------------------

AUTH_USERNAME = os.getenv("VIEWFINDER_AUTH_USERNAME", "")
AUTH_PASSWORD = os.getenv("VIEWFINDER_AUTH_PASSWORD", "")

security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if not AUTH_USERNAME or not AUTH_PASSWORD:
        raise HTTPException(status_code=500, detail="Auth credentials not configured on server")
    username_ok = secrets.compare_digest(credentials.username.encode(), AUTH_USERNAME.encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), AUTH_PASSWORD.encode())
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return credentials.username


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

router = APIRouter(prefix="/api", dependencies=[Depends(verify_credentials)])


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class BoundingBox(BaseModel):
    min_lat: float
    min_lon: float
    max_lat: float
    max_lon: float


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