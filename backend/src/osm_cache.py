"""
OSM Local Cache Management

Downloads and maintains a local OSM extract for fast bounding-box queries.
Uses Geofabrik extracts with incremental updates via pyosmium.
"""

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional

import requests
from loguru import logger

from config import (
    OSM_CACHE_ENABLED,
    OSM_DATA_DIR,
    OSM_GEOFABRIK_URL,
    OSM_STALE_DAYS,
)

# File names within OSM_DATA_DIR
SOURCE_FILE = "source.osm.pbf"        # Full regional extract
RAILWAYS_FILE = "railways.osm.pbf"    # Filtered railways only
UPDATE_STATE_FILE = "update.state"    # pyosmium state file


def get_source_path() -> Path:
    """Get path to the source OSM extract."""
    return Path(OSM_DATA_DIR) / SOURCE_FILE


def get_railways_path() -> Path:
    """Get path to the filtered railways file."""
    return Path(OSM_DATA_DIR) / RAILWAYS_FILE


def is_cache_available() -> bool:
    """Check if the local cache is available and usable."""
    if not OSM_CACHE_ENABLED:
        return False
    railways_path = get_railways_path()
    return railways_path.exists() and railways_path.stat().st_size > 0


def _ensure_data_dir() -> None:
    """Create OSM data directory if it doesn't exist."""
    Path(OSM_DATA_DIR).mkdir(parents=True, exist_ok=True)


def _get_file_age_days(path: Path) -> Optional[float]:
    """Get the age of a file in days, or None if it doesn't exist."""
    if not path.exists():
        return None
    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    age = datetime.now() - mtime
    return age.total_seconds() / 86400


def _is_data_stale() -> bool:
    """Check if the source data is too old for incremental updates."""
    source_path = get_source_path()
    age = _get_file_age_days(source_path)
    if age is None:
        return True  # No file = stale
    return age > OSM_STALE_DAYS


def download_full_extract() -> bool:
    """
    Download the full OSM extract from Geofabrik.
    Returns True on success, False on failure.
    """
    _ensure_data_dir()
    source_path = get_source_path()
    temp_path = source_path.with_suffix(".pbf.tmp")

    logger.info(f"osm_cache: Downloading full extract from {OSM_GEOFABRIK_URL}")
    
    try:
        # Stream download to handle large files
        with requests.get(OSM_GEOFABRIK_URL, stream=True, timeout=3600) as response:
            response.raise_for_status()
            total_size = int(response.headers.get("content-length", 0))
            downloaded = 0
            
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192 * 1024):  # 8MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total_size > 0:
                        pct = (downloaded / total_size) * 100
                        if downloaded % (100 * 1024 * 1024) < len(chunk):  # Log every ~100MB
                            logger.debug(f"osm_cache: Download progress: {pct:.1f}%")
        
        # Atomic replace
        shutil.move(str(temp_path), str(source_path))
        logger.info(f"osm_cache: Downloaded {downloaded / 1024 / 1024:.1f} MB")
        return True
        
    except Exception as e:
        logger.error(f"osm_cache: Failed to download extract: {e}")
        if temp_path.exists():
            temp_path.unlink()
        return False


def apply_incremental_updates() -> bool:
    """
    Apply incremental updates using pyosmium-up-to-date.
    Returns True on success, False on failure.
    """
    source_path = get_source_path()
    
    if not source_path.exists():
        logger.warning("osm_cache: Source file not found, cannot apply updates")
        return False
    
    logger.info("osm_cache: Applying incremental updates")
    
    try:
        result = subprocess.run(
            ["pyosmium-up-to-date", "-v", str(source_path)],
            capture_output=True,
            text=True,
            timeout=3600,  # 1 hour timeout
        )
        
        if result.returncode != 0:
            logger.error(f"osm_cache: pyosmium-up-to-date failed: {result.stderr}")
            return False
        
        logger.info("osm_cache: Incremental updates applied successfully")
        return True
        
    except subprocess.TimeoutExpired:
        logger.error("osm_cache: pyosmium-up-to-date timed out")
        return False
    except FileNotFoundError:
        logger.error("osm_cache: pyosmium-up-to-date not found, is osmium installed?")
        return False
    except Exception as e:
        logger.error(f"osm_cache: Failed to apply updates: {e}")
        return False


def filter_to_railways() -> bool:
    """
    Filter the source OSM file to railways only.
    
    Two-pass filtering to match Overpass query:
    1. Keep only railway=rail and railway=narrow_gauge
    2. Exclude service tracks (yard, siding, spur, crossover)
    
    Returns True on success, False on failure.
    """
    source_path = get_source_path()
    railways_path = get_railways_path()
    temp_path = railways_path.with_suffix(".pbf.tmp")
    temp_path_step1 = railways_path.with_suffix(".pbf.step1")
    
    if not source_path.exists():
        logger.warning("osm_cache: Source file not found, cannot filter")
        return False
    
    logger.info("osm_cache: Filtering to railways only (2-pass)")
    
    try:
        # Step 1: Keep only railway=rail and railway=narrow_gauge
        logger.debug("osm_cache: Step 1 - filtering to railway=rail,narrow_gauge")
        result = subprocess.run(
            [
                "osmium", "tags-filter",
                str(source_path),
                "w/railway=rail,narrow_gauge",
                "-o", str(temp_path_step1),
                "--overwrite",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        
        if result.returncode != 0:
            logger.error(f"osm_cache: osmium tags-filter step 1 failed: {result.stderr}")
            return False
        
        # Step 2: Exclude service tracks using inverted filter
        # -i means "keep everything EXCEPT what matches"
        logger.debug("osm_cache: Step 2 - excluding service=yard,siding,spur")
        result = subprocess.run(
            [
                "osmium", "tags-filter",
                str(temp_path_step1),
                "-i",  # Invert: exclude matching objects
                "w/service=yard,siding,spur",
                "-o", str(temp_path),
                "--overwrite",
            ],
            capture_output=True,
            text=True,
            timeout=3600,
        )
        
        if result.returncode != 0:
            logger.error(f"osm_cache: osmium tags-filter step 2 failed: {result.stderr}")
            return False
        
        # Clean up intermediate file
        if temp_path_step1.exists():
            temp_path_step1.unlink()
        
        # Atomic replace
        shutil.move(str(temp_path), str(railways_path))
        
        size_mb = railways_path.stat().st_size / 1024 / 1024
        logger.info(f"osm_cache: Filtered railways file: {size_mb:.1f} MB")
        return True
        
    except subprocess.TimeoutExpired:
        logger.error("osm_cache: osmium tags-filter timed out")
        for p in [temp_path, temp_path_step1]:
            if p.exists():
                p.unlink()
        return False
    except FileNotFoundError:
        logger.error("osm_cache: osmium not found, is osmium-tool installed?")
        return False
    except Exception as e:
        logger.error(f"osm_cache: Failed to filter: {e}")
        for p in [temp_path, temp_path_step1]:
            if p.exists():
                p.unlink()
        return False


def extract_bounding_box(bounding_box: Dict[str, float]) -> Optional[str]:
    """
    Extract a bounding box from the railways file.
    Returns path to temporary .osm file, or None on failure.
    """
    railways_path = get_railways_path()
    
    if not railways_path.exists():
        logger.debug("osm_cache: Railways file not found")
        return None
    
    min_lon = bounding_box["min_lon"]
    min_lat = bounding_box["min_lat"]
    max_lon = bounding_box["max_lon"]
    max_lat = bounding_box["max_lat"]
    
    # Create temp file for output
    tmp = tempfile.NamedTemporaryFile(
        suffix=".osm", delete=False, mode="w", encoding="utf-8"
    )
    tmp.close()
    output_path = tmp.name
    
    try:
        # Extract bounding box with osmium
        bbox_str = f"{min_lon},{min_lat},{max_lon},{max_lat}"
        result = subprocess.run(
            [
                "osmium", "extract",
                "-b", bbox_str,
                str(railways_path),
                "-o", output_path,
                "--overwrite",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        
        if result.returncode != 0:
            logger.error(f"osm_cache: osmium extract failed: {result.stderr}")
            os.unlink(output_path)
            return None
        
        logger.debug(f"osm_cache: Extracted bbox to {output_path}")
        return output_path
        
    except subprocess.TimeoutExpired:
        logger.error("osm_cache: osmium extract timed out")
        if os.path.exists(output_path):
            os.unlink(output_path)
        return None
    except FileNotFoundError:
        logger.error("osm_cache: osmium not found")
        if os.path.exists(output_path):
            os.unlink(output_path)
        return None
    except Exception as e:
        logger.error(f"osm_cache: Failed to extract: {e}")
        if os.path.exists(output_path):
            os.unlink(output_path)
        return None


async def update_cache() -> bool:
    """
    Update the OSM cache. Called by the scheduler.
    
    Strategy:
    1. If data is stale (>60 days) or missing, download full extract
    2. Otherwise, apply incremental updates
    3. Re-filter to railways
    
    Returns True on success, False on failure.
    """
    if not OSM_CACHE_ENABLED:
        logger.debug("osm_cache: Cache disabled, skipping update")
        return False
    
    logger.info("osm_cache: Starting cache update")
    start_time = time.time()
    
    source_path = get_source_path()
    
    # Decide whether to download fresh or update incrementally
    if _is_data_stale():
        logger.info("osm_cache: Data is stale or missing, downloading full extract")
        if not download_full_extract():
            logger.error("osm_cache: Failed to download, cache update aborted")
            return False
    else:
        # Try incremental update
        if not apply_incremental_updates():
            logger.warning("osm_cache: Incremental update failed, trying full download")
            if not download_full_extract():
                logger.error("osm_cache: Failed to download, cache update aborted")
                return False
    
    # Re-filter to railways
    if not filter_to_railways():
        logger.error("osm_cache: Failed to filter, cache update aborted")
        return False
    
    elapsed = time.time() - start_time
    logger.info(f"osm_cache: Cache update completed in {elapsed:.1f}s")
    return True


def init_cache_if_needed() -> None:
    """
    Initialize the cache on startup if it doesn't exist.
    This is a blocking operation - downloads the full extract if needed.
    """
    if not OSM_CACHE_ENABLED:
        logger.info("osm_cache: Local cache disabled")
        return
    
    _ensure_data_dir()
    
    if is_cache_available():
        age = _get_file_age_days(get_railways_path())
        logger.info(f"osm_cache: Cache available, {age:.1f} days old")
        return
    
    logger.info("osm_cache: Cache not available, initializing...")
    
    # Check if source exists but railways doesn't
    if get_source_path().exists():
        logger.info("osm_cache: Source exists, filtering to railways")
        if filter_to_railways():
            logger.info("osm_cache: Cache initialized from existing source")
            return
    
    # Need to download
    logger.info("osm_cache: Downloading initial OSM data (this may take a while)")
    if download_full_extract() and filter_to_railways():
        logger.info("osm_cache: Cache initialized successfully")
    else:
        logger.warning("osm_cache: Failed to initialize cache, will use Overpass fallback")


# ---------------------------------------------------------------------------
# CLI entry point for manual cache management
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import asyncio
    
    parser = argparse.ArgumentParser(description="Manage OSM cache")
    parser.add_argument("--init", action="store_true", help="Initialize cache (download if needed)")
    parser.add_argument("--update", action="store_true", help="Update cache (incremental or full)")
    parser.add_argument("--status", action="store_true", help="Show cache status")
    parser.add_argument("--force-download", action="store_true", help="Force full re-download")
    
    args = parser.parse_args()
    
    if args.status:
        print(f"Cache enabled: {OSM_CACHE_ENABLED}")
        print(f"Data directory: {OSM_DATA_DIR}")
        print(f"Source URL: {OSM_GEOFABRIK_URL}")
        print(f"Cache available: {is_cache_available()}")
        source = get_source_path()
        railways = get_railways_path()
        if source.exists():
            age = _get_file_age_days(source)
            size = source.stat().st_size / 1024 / 1024
            print(f"Source file: {size:.1f} MB, {age:.1f} days old")
        else:
            print("Source file: not found")
        if railways.exists():
            age = _get_file_age_days(railways)
            size = railways.stat().st_size / 1024 / 1024
            print(f"Railways file: {size:.1f} MB, {age:.1f} days old")
        else:
            print("Railways file: not found")
    
    elif args.force_download:
        print("Force downloading full extract...")
        if download_full_extract():
            print("Download complete, filtering to railways...")
            if filter_to_railways():
                print("Done!")
            else:
                print("Filtering failed!")
                exit(1)
        else:
            print("Download failed!")
            exit(1)
    
    elif args.update:
        print("Updating cache...")
        success = asyncio.run(update_cache())
        if success:
            print("Update complete!")
        else:
            print("Update failed!")
            exit(1)
    
    elif args.init:
        print("Initializing cache...")
        init_cache_if_needed()
        if is_cache_available():
            print("Cache ready!")
        else:
            print("Initialization failed!")
            exit(1)
    
    else:
        parser.print_help()
