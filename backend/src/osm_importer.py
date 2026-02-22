import os
import platform
import tempfile
import subprocess
import time
from typing import Dict, Tuple
import requests
import xml.etree.ElementTree as ET
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from config import OVERPASS_MAX_RETRIES, OVERPASS_REQUEST_DELAY

async def import_ways(session: AsyncSession, bounding_box: Dict[str, float], way_type: str) -> None:
    """Download OSM ways and import to Postgres using osm2pgrouting, then update tunnel/bridge info."""
    min_lat = bounding_box['min_lat']
    min_lon = bounding_box['min_lon']
    max_lat = bounding_box['max_lat']
    max_lon = bounding_box['max_lon']

    if way_type != "railway":
        raise NotImplementedError(f"Way type '{way_type}' is not implemented yet.")
    else:
        way_selector = '["railway"~"^(rail|narrow_gauge)$"]["service"!~"^(yard|siding|spur|crossover)$"]'

    overpass_url = "https://overpass.private.coffee/api/interpreter"
    overpass_query = f"""
    [out:xml][timeout:25];
    way{way_selector}({min_lat},{min_lon},{max_lat},{max_lon});
    (._;>;);
    out body;
    """
    
    logger.debug(f"import_ways: Downloading ways with query: {overpass_query}")
    max_retries = OVERPASS_MAX_RETRIES
    delay_seconds = OVERPASS_REQUEST_DELAY
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            # Use POST and send the Overpass query as form-encoded data
            response = requests.post(
                overpass_url,
                data={"data": overpass_query},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "ViewFinder (kontakt@jonasgaiswinkler.eu)",
                },
            )
        except requests.RequestException as exc:
            logger.warning(
                f"import_ways: Overpass request failed on attempt {attempt}/{max_retries}: {exc}"
            )
            if attempt == max_retries:
                raise
            time.sleep(delay_seconds)
            continue

        logger.debug(
            f"import_ways: Overpass API response status code: {response.status_code} (attempt {attempt}/{max_retries})"
        )

        if response.status_code == 504:
            logger.warning(
                f"import_ways: Overpass API returned 504 on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
            )
            if attempt == max_retries:
                response.raise_for_status()
            time.sleep(delay_seconds)
            continue

        if response.status_code == 429:
            logger.warning(
                f"import_ways: Overpass API returned 429 on attempt {attempt}/{max_retries}, retrying in {delay_seconds}s"
            )
            if attempt == max_retries:
                response.raise_for_status()
            time.sleep(delay_seconds)
            continue

        # Successful (non-504/429) response; break out of retry loop
        break

    if response is None:
        raise RuntimeError("import_ways: Failed to get a response from Overpass API")

    response.raise_for_status()  # Raise an error for other bad responses
    
    # Write XML to temp file for osm2pgrouting
    tmp = tempfile.NamedTemporaryFile(suffix=".osm", delete=False, mode="w", encoding="utf-8")
    tmp.write(response.text)
    tmp.close()
    logger.debug(f"import_ways: Saved OSM XML to {tmp.name}")

    await _cleanup_bounding_box(session, bounding_box)

    await _import_with_pgrouting(
            session=session,
            osm_path=tmp.name,
            mapconfig_path="",
            clean=False,
        )
    
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
    result = subprocess.run(cmd, capture_output=True, text=True)
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
    except OSError:
        pass