from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def save_viewshed_results_to_db(
    viewshed_results: Iterable[Dict[str, Any]],
    session: AsyncSession,
    bounding_box: Optional[Dict[str, float]] = None,
) -> int:
    rows: List[Dict[str, Any]] = []
    for result in viewshed_results:
        if not isinstance(result, dict):
            continue
        lat = result.get("lat")
        lon = result.get("lon")
        if lat is None or lon is None:
            continue
        rows.append(
            {
                "lat": float(lat),
                "lon": float(lon),
                "bridge": result.get("bridge"),
                "tangent_dx": _to_float(result.get("tangent_dx")),
                "tangent_dy": _to_float(result.get("tangent_dy")),
                "tangent_deg_4326": _to_float(result.get("tangent_deg_4326")),
                "tangent_deg_3857": _to_float(result.get("tangent_deg_3857")),
                "factor_left_visible_area": _to_float(result.get("factor_left_visible_area")),
                "factor_right_visible_area": _to_float(result.get("factor_right_visible_area")),
                "factor_total_visible_area": _to_float(result.get("factor_total_visible_area")),
                "factor_relative_visible_area": _to_float(result.get("factor_relative_visible_area")),
            }
        )

    if bounding_box:
        delete_sql = text(
            """
            DELETE FROM scenicness_points
            WHERE geom && ST_MakeEnvelope(:min_lon, :min_lat, :max_lon, :max_lat, 4326)
            """
        )
        await session.execute(
            delete_sql,
            {
                "min_lon": bounding_box["min_lon"],
                "min_lat": bounding_box["min_lat"],
                "max_lon": bounding_box["max_lon"],
                "max_lat": bounding_box["max_lat"],
            },
        )

    if not rows:
        if bounding_box:
            await session.commit()
        return 0

    print(f"Inserting {len(rows)} scenicness points")


    insert_sql = text(
        """
        INSERT INTO scenicness_points (
            geom,
            bridge,
            way_id,
            tangent_dx,
            tangent_dy,
            tangent_deg_4326,
            tangent_deg_3857,
            factor_left_visible_area,
            factor_right_visible_area,
            factor_total_visible_area,
            factor_relative_visible_area
        )
        VALUES (
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
            CAST(:bridge AS boolean),
            (
                SELECT way_id
                FROM osm_ways
                ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                LIMIT 1
            ),
            CAST(:tangent_dx AS double precision),
            CAST(:tangent_dy AS double precision),
            CAST(:tangent_deg_4326 AS double precision),
            CAST(:tangent_deg_3857 AS double precision),
            CAST(:factor_left_visible_area AS double precision),
            CAST(:factor_right_visible_area AS double precision),
            CAST(:factor_total_visible_area AS double precision),
            CAST(:factor_relative_visible_area AS double precision)
        )
        """
    )

    for row in rows:
        await session.execute(insert_sql, row)
    await session.commit()
    print("Refreshing scenicness_segments materialized view")
    await session.execute(text("REFRESH MATERIALIZED VIEW scenicness_segments"))
    await session.commit()
    return len(rows)