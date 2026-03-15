import os
import secrets
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, Dict, List, Optional
import sys

from loguru import logger
# Configure loguru from the environment variable LOG_LEVEL (default: INFO).
# This makes the `LOG_LEVEL` env var used by loguru in production.
logger.remove()
logger.add(sys.stderr, level=os.getenv("LOG_LEVEL", "INFO").upper())
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Depends, FastAPI, HTTPException, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from config import OSM_CACHE_ENABLED, OSM_UPDATE_HOUR
from db import AsyncSessionLocal
from job_manager import job_manager, JobInfo
import osm_cache
from route_solver import compute_scenic_route

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
# Lifespan – start / stop the background job worker and OSM cache scheduler
# ---------------------------------------------------------------------------

# APScheduler instance for background tasks
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start job manager
    job_manager.start()
    
    # Initialize OSM cache if enabled
    if OSM_CACHE_ENABLED:
        # Schedule daily OSM cache update
        scheduler.add_job(
            osm_cache.update_cache,
            "cron",
            hour=OSM_UPDATE_HOUR,
            id="osm_cache_update",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(f"OSM cache scheduler started, updates at {OSM_UPDATE_HOUR}:00 daily")
        
        # Check if cache needs initialization (non-blocking log, actual init is lazy)
        if not osm_cache.is_cache_available():
            logger.warning(
                "OSM cache not available. First request will use Overpass API. "
                "Run osm_cache.init_cache_if_needed() or wait for scheduled update."
            )
    
    yield
    
    # Shutdown
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="ViewFinder",
    summary="A service that can estimate the best side to sit on, given any route, so you get the best views.",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

router = APIRouter(prefix="/api", dependencies=[Depends(verify_credentials)])
public_router = APIRouter(prefix="/api")


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


class RouteRequest(BaseModel):
    coordinates: List[List[float]]  # [[lon, lat], ...]


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


@public_router.post("/route")
async def route(request: RouteRequest):
    """Compute a scenic route through the given waypoints.

    Accepts an array of ``[lon, lat]`` coordinates, routes between them on
    the ways network using pgRouting, and returns a GeoJSON FeatureCollection
    with direction-corrected scenicness segments.
    """
    if len(request.coordinates) < 2:
        raise HTTPException(status_code=400, detail="At least two coordinates are required")
    for i, coord in enumerate(request.coordinates):
        if not isinstance(coord, list) or len(coord) != 2:
            raise HTTPException(status_code=400, detail=f"Coordinate at index {i} must be [lon, lat]")

    async with AsyncSessionLocal() as session:
        try:
            result = await compute_scenic_route(session, request.coordinates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return result


app.include_router(router)
app.include_router(public_router)