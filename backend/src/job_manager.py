from __future__ import annotations

import asyncio
import traceback
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from loguru import logger
from pydantic import BaseModel


class JobStatus(str, Enum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class JobStep(str, Enum):
    importing_ways = "importing_ways"
    sampling_points = "sampling_points"
    retrieving_dem = "retrieving_dem"
    performing_viewshed = "performing_viewshed"
    saving_results = "saving_results"


class JobInfo(BaseModel):
    id: str
    status: JobStatus
    current_step: Optional[JobStep] = None
    current_tile: Optional[int] = None
    total_tiles: Optional[int] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    params: Dict[str, Any] = {}


class JobManager:
    """Manages a sequential queue of background computation jobs."""

    def __init__(self) -> None:
        self._jobs: Dict[str, JobInfo] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the background worker (call once at app startup)."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())
            logger.info("Job worker started")

    def create_job(self, params: Dict[str, Any]) -> JobInfo:
        """Create a new job entry and place it on the queue."""
        job_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc)
        job = JobInfo(
            id=job_id,
            status=JobStatus.queued,
            created_at=now,
            params=params,
        )
        self._jobs[job_id] = job
        self._queue.put_nowait(job_id)
        logger.info(f"Job {job_id} created (queue size: {self._queue.qsize()})")
        return job

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        return self._jobs.get(job_id)

    def list_jobs(self) -> List[JobInfo]:
        return list(self._jobs.values())

    def update_step(self, job_id: str, step: JobStep) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.current_step = step
            logger.info(f"Job {job_id} step → {step.value}")

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        """Process jobs one at a time, forever."""
        logger.info("Job worker loop running")
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            if job is None:
                self._queue.task_done()
                continue

            job.status = JobStatus.running
            job.started_at = datetime.now(timezone.utc)
            logger.info(f"Job {job_id} started")

            try:
                await self._run_job(job)
                job.status = JobStatus.completed
                job.finished_at = datetime.now(timezone.utc)
                logger.info(f"Job {job_id} completed")
            except Exception as exc:
                job.status = JobStatus.failed
                job.error = f"{exc}\n{traceback.format_exc()}"
                job.finished_at = datetime.now(timezone.utc)
                logger.error(f"Job {job_id} failed: {exc}")
            finally:
                self._queue.task_done()

    async def _run_job(self, job: JobInfo) -> None:
        """Execute the scenicness computation pipeline for *job*."""
        # Import here to avoid circular imports
        from db import AsyncSessionLocal
        from osm_importer import import_ways
        from way_sampler import sample_points_along_ways
        from dem_retriever import retrieve_dem
        from viewshed_analyzer import perform_viewshed_analysis
        from scenicness_saver import save_viewshed_results_to_db, refresh_scenicness_segments
        from utils import split_bounding_box

        from config import TILE_SIZE_KM

        bounding_box = job.params["bounding_box"]
        way_type = job.params["way_type"]

        tiles = split_bounding_box(bounding_box, TILE_SIZE_KM)
        total_tiles = len(tiles)
        job.total_tiles = total_tiles
        logger.info(f"Job {job.id}: bounding box split into {total_tiles} tile(s) ({TILE_SIZE_KM} km)")

        total_viewshed_points = 0

        async with AsyncSessionLocal() as session:
            for tile_idx, tile_bbox in enumerate(tiles, start=1):
                job.current_tile = tile_idx
                logger.info(f"Job {job.id}: processing tile {tile_idx}/{total_tiles}")

                # Step 1 – Import ways from OSM
                self.update_step(job.id, JobStep.importing_ways)
                await import_ways(session, tile_bbox, way_type)

                # Step 2 – Sample points along ways
                self.update_step(job.id, JobStep.sampling_points)
                sampled_points = await sample_points_along_ways(
                    session=session, bounding_box=tile_bbox
                )

                # Step 3 – Retrieve DEM data (CPU-bound → run in thread)
                self.update_step(job.id, JobStep.retrieving_dem)
                dem_data = await asyncio.to_thread(retrieve_dem, tile_bbox, True)

                # Step 4 – Perform viewshed analysis (CPU-bound → run in thread)
                self.update_step(job.id, JobStep.performing_viewshed)
                viewshed_results = await asyncio.to_thread(
                    perform_viewshed_analysis, dem_data, sampled_points
                )

                # Step 5 – Save results to DB (skip segment refresh until all tiles done)
                self.update_step(job.id, JobStep.saving_results)
                await save_viewshed_results_to_db(
                    viewshed_results, session=session, refresh_segments=False
                )

                total_viewshed_points += len(viewshed_results)
                logger.info(f"Job {job.id}: tile {tile_idx}/{total_tiles} done ({len(viewshed_results)} points)")

            # Final refresh of scenicness_segments after all tiles are processed
            logger.info(f"Job {job.id}: refreshing scenicness segments")
            await refresh_scenicness_segments(session)

        job.result = {"num_viewshed_points": total_viewshed_points, "num_tiles": total_tiles}


# Singleton instance used by the application
job_manager = JobManager()
