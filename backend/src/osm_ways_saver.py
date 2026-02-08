from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional
import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _tunnel_to_bool(tag_value: Optional[str]) -> bool:
    if tag_value is None:
        return False
    return str(tag_value).strip().lower() == "yes"


def _bridge_to_bool(tag_value: Optional[str]) -> bool:
    return tag_value is not None


def _geometry_to_linestring_wkt(geometry: List[Dict[str, float]]) -> Optional[str]:
    if not geometry:
        return None
    coords = []
    for point in geometry:
        lon = point.get("lon")
        lat = point.get("lat")
        if lon is None or lat is None:
            continue
        coords.append(f"{lon} {lat}")
    if len(coords) < 2:
        return None
    return f"LINESTRING({', '.join(coords)})"


async def save_ways_to_db(
    ways: Iterable[Dict[str, Any]],
    way_type: str,
    session: AsyncSession,
    bounding_box: Optional[Dict[str, float]] = None,
) -> int:
    """
    Persist OSM ways into osm_ways table and delete stale ways in the same bounding box.

    Expected table fields (osm_ways): way_id, way_type, tunnel, bridge, tags, geom
    Expected table fields (osm_way_nodes): way_id, node_id, seq
    """
    rows = []
    node_rows = []
    way_ids: List[int] = []
    total_ways = 0
    skipped_no_id = 0
    skipped_no_geometry = 0
    skipped_invalid_geometry = 0
    skipped_missing_coords = 0
    skipped_duplicate_id = 0
    seen_way_ids: set[int] = set()
    for way in ways:
        total_ways += 1
        way_id = way.get("id")
        tags = way.get("tags") or {}
        geometry = way.get("geometry") or []
        if not geometry:
            skipped_no_geometry += 1
            continue
        if any(point.get("lon") is None or point.get("lat") is None for point in geometry):
            skipped_missing_coords += 1
        wkt = _geometry_to_linestring_wkt(geometry)
        if way_id is None:
            skipped_no_id += 1
            continue
        if wkt is None:
            skipped_invalid_geometry += 1
            continue
        way_id_int = int(way_id)
        if way_id_int in seen_way_ids:
            skipped_duplicate_id += 1
            continue
        seen_way_ids.add(way_id_int)
        way_ids.append(way_id_int)
        nodes = way.get("nodes") or []
        if isinstance(nodes, list) and nodes:
            first_node_id = nodes[0]
            last_node_id = nodes[-1]
            if first_node_id is not None:
                node_rows.append(
                    {
                        "way_id": way_id_int,
                        "node_id": int(first_node_id),
                        "seq": 0,
                    }
                )
            if last_node_id is not None and last_node_id != first_node_id:
                node_rows.append(
                    {
                        "way_id": way_id_int,
                        "node_id": int(last_node_id),
                        "seq": len(nodes) - 1,
                    }
                )
        rows.append(
            {
                "way_id": way_id_int,
                "way_type": way_type,
                "tunnel": _tunnel_to_bool(tags.get("tunnel")),
                "bridge": _bridge_to_bool(tags.get("bridge")),
                "tags": json.dumps(tags),
                "wkt": wkt,
            }
        )

    print(
        "save_ways_to_db: total=%s saved=%s skipped_no_id=%s skipped_no_geometry=%s "
        "skipped_invalid_geometry=%s skipped_missing_coords=%s skipped_duplicate_id=%s"
        % (
            total_ways,
            len(rows),
            skipped_no_id,
            skipped_no_geometry,
            skipped_invalid_geometry,
            skipped_missing_coords,
            skipped_duplicate_id,
        )
    )

    if node_rows:
        valid_way_ids = {row["way_id"] for row in rows}
        node_rows = [row for row in node_rows if row["way_id"] in valid_way_ids]

    if bounding_box:
        delete_params = {
            "min_lon": bounding_box["min_lon"],
            "min_lat": bounding_box["min_lat"],
            "max_lon": bounding_box["max_lon"],
            "max_lat": bounding_box["max_lat"],
            "way_ids": way_ids,
        }
        if way_ids:
            delete_way_nodes_sql = text(
                """
                DELETE FROM osm_way_nodes
                WHERE way_id IN (
                    SELECT way_id
                    FROM osm_ways
                    WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                      AND way_id != ALL(:way_ids)
                )
                """
            )
            await session.execute(delete_way_nodes_sql, delete_params)
        else:
            delete_way_nodes_sql = text(
                """
                DELETE FROM osm_way_nodes
                WHERE way_id IN (
                    SELECT way_id
                    FROM osm_ways
                    WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                )
                """
            )
            await session.execute(delete_way_nodes_sql, delete_params)
        if way_ids:
            delete_sql = text(
                """
                DELETE FROM osm_ways
                WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                  AND way_id != ALL(:way_ids)
                """
            )
            await session.execute(delete_sql, delete_params)
        else:
            delete_sql = text(
                """
                DELETE FROM osm_ways
                WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
                """
            )
            await session.execute(delete_sql, delete_params)

    if not rows:
        if bounding_box:
            await session.commit()
        return 0

    insert_sql = text(
        """
        INSERT INTO osm_ways (way_id, way_type, tunnel, bridge, tags, geom)
        VALUES (:way_id, :way_type, :tunnel, :bridge, CAST(:tags AS jsonb), ST_GeomFromText(:wkt, 4326))
        ON CONFLICT (way_id)
        DO UPDATE SET
            way_type = EXCLUDED.way_type,
            tunnel = EXCLUDED.tunnel,
            bridge = EXCLUDED.bridge,
            tags = EXCLUDED.tags,
            geom = EXCLUDED.geom
        """
    )

    for row in rows:
        await session.execute(insert_sql, row)

    if way_ids:
        delete_existing_nodes_sql = text(
            """
            DELETE FROM osm_way_nodes
            WHERE way_id = ANY(:way_ids)
            """
        )
        await session.execute(delete_existing_nodes_sql, {"way_ids": way_ids})

    if node_rows:
        existing_way_ids_sql = text(
            """
            SELECT way_id
            FROM osm_ways
            WHERE way_id = ANY(:way_ids)
            """
        )
        result = await session.execute(existing_way_ids_sql, {"way_ids": way_ids})
        existing_way_ids = {int(row[0]) for row in result.fetchall()}
        filtered_node_rows = [
            row for row in node_rows if int(row["way_id"]) in existing_way_ids
        ]
        print(f"Inserting {len(filtered_node_rows)} way nodes")
        if filtered_node_rows:
            insert_nodes_sql = text(
                """
                INSERT INTO osm_way_nodes (way_id, node_id, seq)
                VALUES (:way_id, :node_id, :seq)
                ON CONFLICT DO NOTHING
                """
            )
            for node_row in filtered_node_rows:
                await session.execute(insert_nodes_sql, node_row)
    await session.commit()
    return len(rows)
