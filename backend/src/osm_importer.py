import asyncio
import os
import platform
import tempfile
import subprocess
import time
from typing import Dict, Optional, Tuple
import requests
import xml.etree.ElementTree as ET
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from config import OVERPASS_MAX_RETRIES, OVERPASS_REQUEST_DELAY, OVERPASS_TIMEOUT
import osm_cache

async def import_ways(session: AsyncSession, bounding_box: Dict[str, float], way_type: str) -> None:
    """Download OSM ways and import to Postgres using osm2pgrouting, then update tunnel/bridge info.
    
    Uses local OSM cache if available, with fallback to Overpass API.
    """
    if way_type != "railway":
        raise NotImplementedError(f"Way type '{way_type}' is not implemented yet.")

    # Try local cache first (blocking subprocess → run in thread)
    osm_path = await asyncio.to_thread(_try_local_extraction, bounding_box)
    
    if osm_path is None:
        # Fall back to Overpass
        logger.debug("import_ways: Local cache unavailable, using Overpass API")
        osm_path = await _fetch_from_overpass(bounding_box)
    else:
        logger.debug(f"import_ways: Using local cache extraction: {osm_path}")

    await _cleanup_bounding_box(session, bounding_box)

    await _import_with_pgrouting(
            session=session,
            osm_path=osm_path,
            mapconfig_path="",
            clean=False,
        )


def _try_local_extraction(bounding_box: Dict[str, float]) -> Optional[str]:
    """Try to extract from local cache. Returns path to .osm file or None."""
    if not osm_cache.is_cache_available():
        return None
    
    return osm_cache.extract_bounding_box(bounding_box)


async def _fetch_from_overpass(bounding_box: Dict[str, float]) -> str:
    """Fetch OSM data from Overpass API. Returns path to temp .osm file."""
    min_lat = bounding_box['min_lat']
    min_lon = bounding_box['min_lon']
    max_lat = bounding_box['max_lat']
    max_lon = bounding_box['max_lon']

    way_selector = '["railway"~"^(rail|narrow_gauge)$"]["service"!~"^(yard|siding|spur)$"]'

    # List of Overpass API mirrors for fallback
    overpass_servers = [
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass-api.de/api/interpreter",
    ]

    overpass_query = f"""
    [out:xml][timeout:{OVERPASS_TIMEOUT}];
    way{way_selector}({min_lat},{min_lon},{max_lat},{max_lon});
    (._;>;);
    out body;
    """
    
    logger.debug(f"_fetch_from_overpass: Downloading ways with query: {overpass_query}")
    max_retries = OVERPASS_MAX_RETRIES
    delay_seconds = OVERPASS_REQUEST_DELAY
    response = None
    
    # Total attempts across all servers
    total_attempts = 0
    max_total_attempts = max_retries * len(overpass_servers)
    
    for server_idx, overpass_url in enumerate(overpass_servers):
        for attempt in range(1, max_retries + 1):
            total_attempts += 1
            try:
                # Use POST and send the Overpass query as form-encoded data
                # Blocking HTTP call → run in thread to avoid freezing the event loop
                response = await asyncio.to_thread(
                    lambda: requests.post(
                        overpass_url,
                        data={"data": overpass_query},
                        headers={
                            "Content-Type": "application/x-www-form-urlencoded",
                            "User-Agent": "ViewFinder (kontakt@jonasgaiswinkler.eu)",
                        },
                        timeout=OVERPASS_TIMEOUT + 30,  # Allow extra time for network
                    )
                )
            except requests.RequestException as exc:
                logger.warning(
                    f"_fetch_from_overpass: Request to {overpass_url} failed on attempt {attempt}/{max_retries}: {exc}"
                )
                if total_attempts >= max_total_attempts:
                    raise
                await asyncio.sleep(delay_seconds)
                continue

            logger.debug(
                f"_fetch_from_overpass: {overpass_url} response status: {response.status_code} (attempt {attempt}/{max_retries})"
            )

            if response.status_code == 504:
                logger.warning(
                    f"_fetch_from_overpass: {overpass_url} returned 504 on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
                )
                if total_attempts >= max_total_attempts:
                    response.raise_for_status()
                await asyncio.sleep(delay_seconds)
                continue

            if response.status_code == 429:
                logger.warning(
                    f"_fetch_from_overpass: {overpass_url} returned 429 on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
                )
                if total_attempts >= max_total_attempts:
                    response.raise_for_status()
                await asyncio.sleep(delay_seconds)
                continue

            # Some successful 200 responses may still indicate a query timeout
            # (Overpass returns XML with a <remark> element or XHTML error page). Detect that and
            # treat it as a transient error so we retry like on 504/429.
            if response.status_code == 200:
                try:
                    root = ET.fromstring(response.text)
                    
                    # Check for XHTML error page (runtime error, dispatcher error, etc.)
                    # XHTML is valid XML, so it parses successfully but has <html> root
                    if root.tag == "{http://www.w3.org/1999/xhtml}html" or root.tag == "html":
                        response_lower = response.text.lower()
                        if "runtime error" in response_lower or "dispatcher_client" in response_lower:
                            logger.warning(
                                f"_fetch_from_overpass: {overpass_url} returned XHTML error on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
                            )
                            if total_attempts >= max_total_attempts:
                                raise RuntimeError(f"Overpass returned XHTML error: {response.text[:500]}")
                            await asyncio.sleep(delay_seconds)
                            continue
                    
                    # Check for OSM XML with <remark> indicating timeout
                    remark = root.find("remark")
                    if remark is not None and remark.text:
                        remark_text = remark.text.strip().lower()
                        if (
                            "timed out" in remark_text
                            or "timeout" in remark_text
                            or "query timed out" in remark_text
                            or "runtime error" in remark_text
                        ):
                            logger.warning(
                                f"_fetch_from_overpass: {overpass_url} returned remark indicating timeout on attempt {attempt}/{max_retries}: {remark_text!r}, retrying in {delay_seconds}s"
                            )
                            if total_attempts >= max_total_attempts:
                                raise RuntimeError(f"Overpass returned timeout remark: {remark_text}")
                            await asyncio.sleep(delay_seconds)
                            continue
                except ET.ParseError:
                    # Non-XML body — treat as transient error and retry
                    logger.warning(
                        f"_fetch_from_overpass: {overpass_url} returned non-XML response on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
                    )
                    if total_attempts >= max_total_attempts:
                        raise RuntimeError(f"Overpass returned non-XML response: {response.text[:500]}")
                    await asyncio.sleep(delay_seconds)
                    continue

            # Successful response; break out of both loops
            break
        else:
            # Inner loop completed without break (all attempts failed for this server)
            # Try next server
            logger.info(f"_fetch_from_overpass: All attempts failed for {overpass_url}, trying next server")
            continue
        # Inner loop broke successfully
        break

    if response is None:
        raise RuntimeError("_fetch_from_overpass: Failed to get a response from any Overpass server")

    response.raise_for_status()  # Raise an error for other bad responses
    
    # Write XML to temp file for osm2pgrouting
    tmp = tempfile.NamedTemporaryFile(suffix=".osm", delete=False, mode="w", encoding="utf-8")
    tmp.write(response.text)
    tmp.close()
    logger.debug(f"_fetch_from_overpass: Saved OSM XML to {tmp.name}")
    return tmp.name
    
async def _cleanup_bounding_box(
    session: AsyncSession,
    bounding_box: Dict[str, float],
) -> None:
    """Delete existing pgRouting data and scenicness points within a bounding box."""

    params = {
        "min_lon": bounding_box["min_lon"],
        "min_lat": bounding_box["min_lat"],
        "max_lon": bounding_box["max_lon"],
        "max_lat": bounding_box["max_lat"],
    }

    logger.debug(f"cleanup_bounding_box: Cleaning up area {bounding_box}")

    # Check if pgRouting tables exist yet
    ways_exists = await session.execute(text("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'ways'
        )
    """))
    if not ways_exists.scalar():
        logger.debug("cleanup_bounding_box: ways table does not exist yet, skipping cleanup")
        return

    # 1. Collect vertex IDs that could become orphaned (before deleting ways)
    result = await session.execute(text("""
        SELECT DISTINCT v_id FROM (
            SELECT source AS v_id FROM ways
            WHERE the_geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
            UNION
            SELECT target AS v_id FROM ways
            WHERE the_geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
        ) sub
    """), params)
    affected_vertex_ids = [row[0] for row in result.fetchall()]

    # 2. Delete scenicness data for edges in the bounding box
    await session.execute(text("""
        DELETE FROM scenicness_points
        WHERE edge_id IN (
            SELECT gid FROM ways
            WHERE the_geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
        )
    """), params)

    # 3. Delete edges in the bounding box
    await session.execute(text("""
        DELETE FROM ways
        WHERE the_geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
    """), params)

    # 4. Remove orphaned vertices — only check the ones we know were affected
    logger.debug(f"cleanup_bounding_box: ways_vertices_pgr ({len(affected_vertex_ids)} candidates)")
    if affected_vertex_ids:
        # Find which candidates are still referenced by remaining ways
        still_referenced = await session.execute(text("""
            SELECT DISTINCT v_id FROM (
                SELECT source AS v_id FROM ways WHERE source = ANY(:vertex_ids)
                UNION
                SELECT target AS v_id FROM ways WHERE target = ANY(:vertex_ids)
            ) sub
        """), {"vertex_ids": affected_vertex_ids})
        still_referenced_ids = {row[0] for row in still_referenced.fetchall()}

        # Orphans = candidates that are no longer referenced
        orphan_ids = [v for v in affected_vertex_ids if v not in still_referenced_ids]
        logger.debug(f"cleanup_bounding_box: deleting {len(orphan_ids)} orphaned vertices")

        if orphan_ids:
            # Disable FK triggers to avoid per-row constraint checks (the main bottleneck)
            await session.execute(text(
                "ALTER TABLE ways_vertices_pgr DISABLE TRIGGER ALL"
            ))
            await session.execute(text(
                "DELETE FROM ways_vertices_pgr WHERE id = ANY(:orphan_ids)"
            ), {"orphan_ids": orphan_ids})
            await session.execute(text(
                "ALTER TABLE ways_vertices_pgr ENABLE TRIGGER ALL"
            ))
            logger.debug(f"cleanup_bounding_box: deleted {len(orphan_ids)} orphaned vertices")

    await session.commit()
    logger.debug(f"cleanup_bounding_box: Cleaned up area {bounding_box}")

def _parse_tunnel_bridge_from_osm(osm_path: str) -> Dict[int, Tuple[bool, bool]]:
    """Parse .osm XML and return {osm_way_id: (is_tunnel, is_bridge)} for each way."""
    result = {}
    tree = ET.parse(osm_path)
    root = tree.getroot()

    for way in root.findall("way"):
        way_id_str = way.get("id")
        if way_id_str is None:
            continue
        way_id = int(way_id_str)
        tags = {}
        for tag in way.findall("tag"):
            k = tag.get("k")
            v = tag.get("v")
            if k is not None and v is not None:
                tags[k] = v

        is_tunnel = tags.get("tunnel", "").lower() == "yes"
        bridge_val = tags.get("bridge", "")
        is_bridge = bridge_val != "" and bridge_val.lower() != "no"
        result[way_id] = (is_tunnel, is_bridge)

    return result

async def _ensure_tunnel_bridge_columns(session: AsyncSession) -> None:
    """Add tunnel/bridge columns to ways table if they don't exist.
    Must be called after osm2pgrouting has created the table."""
    await session.execute(text(
        "ALTER TABLE ways ADD COLUMN IF NOT EXISTS tunnel BOOLEAN DEFAULT FALSE"
    ))
    await session.execute(text(
        "ALTER TABLE ways ADD COLUMN IF NOT EXISTS bridge BOOLEAN DEFAULT FALSE"
    ))
    await session.commit()

async def _update_tunnel_bridge(
    session: AsyncSession,
    osm_path: str,
) -> int:
    """Update ways table with tunnel/bridge info parsed from the .osm file."""
    await _ensure_tunnel_bridge_columns(session)

    way_tags = _parse_tunnel_bridge_from_osm(osm_path)
    if not way_tags:
        logger.debug("_update_tunnel_bridge: No ways found in OSM data to update tunnel/bridge info")
        return 0

    update_sql = text("""
        UPDATE ways
        SET tunnel = :is_tunnel,
            bridge = :is_bridge
        WHERE osm_id = :osm_id
    """)

    count = 0
    for osm_id, (is_tunnel, is_bridge) in way_tags.items():
        result = await session.execute(update_sql, {
            "osm_id": osm_id,
            "is_tunnel": is_tunnel,
            "is_bridge": is_bridge,
        })
        count += result.rowcount  # type: ignore[union-attr]

    await session.commit()
    logger.debug(f"update_tunnel_bridge: Updated {count} ways")
    return count

async def _import_with_pgrouting(
    session: AsyncSession,
    osm_path: str,
    mapconfig_path: str = "",
    clean: bool = False,
) -> None:
    """Run osm2pgrouting to import OSM data."""
    if not mapconfig_path:
        # Resolve relative to the backend root (one level up from src/)
        mapconfig_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "mapconfig_railway.xml",
        )
    
    is_windows = platform.system() == "Windows"

    if is_windows:
        def to_wsl_path(win_path: str) -> str:
            abs_path = os.path.abspath(win_path)
            drive = abs_path[0].lower()
            rest = abs_path[2:].replace("\\", "/")
            return f"/mnt/{drive}{rest}"

        osm_arg = to_wsl_path(osm_path)
        conf_arg = to_wsl_path(mapconfig_path)
        cmd_prefix = ["wsl"]
    else:
        osm_arg = osm_path
        conf_arg = mapconfig_path
        cmd_prefix = []

    cmd = [
        *cmd_prefix,
        "osm2pgrouting",
        "--file", osm_arg,
        "--dbname", os.getenv("POSTGRES_DB", ""),
        "--username", os.getenv("POSTGRES_USER", ""),
        "--password", os.getenv("POSTGRES_PASSWORD", ""),
        "--host", os.getenv("POSTGRES_HOST", "localhost"),
        "--port", os.getenv("POSTGRES_PORT", "5432"),
        "--conf", conf_arg,
    ]
    if clean:
        cmd.append("--clean")

    logger.debug(f"import_with_pgrouting: Running osm2pgrouting")
    # Blocking subprocess → run in thread to avoid freezing the event loop
    result = await asyncio.to_thread(subprocess.run, cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"osm2pgrouting failed (code {result.returncode}):\n{result.stderr}"
        )
    logger.debug(f"import_with_pgrouting: Success run osm2pgrouting")

    await _update_tunnel_bridge(session, osm_path)
    # Clean up temp file
    _cleanup_osm_file(osm_path)

def _cleanup_osm_file(osm_path: str) -> None:
    """Remove the temp .osm file after all processing is done."""
    try:
        os.remove(osm_path)
        logger.debug(f"_cleanup_osm_file: Removed temp OSM file {osm_path}")
    except OSError:
        logger.debug(f"_cleanup_osm_file: Failed to remove temp OSM file {osm_path}")