from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger


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
    refresh_segments: bool = True,
) -> int:
    rows: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []
    for result in viewshed_results:
        if not isinstance(result, dict):
            continue
        lat = result.get("lat")
        lon = result.get("lon")
        if lat is None or lon is None:
            continue
        row = {
            "lat": float(lat),
            "lon": float(lon),
            "bridge": result.get("bridge"),
            "edge_id": result.get("edge_id"),
            "fraction": _to_float(result.get("fraction")),
            "tangent_dx": _to_float(result.get("tangent_dx")),
            "tangent_dy": _to_float(result.get("tangent_dy")),
            "tangent_deg_4326": _to_float(result.get("tangent_deg_4326")),
            "tangent_deg_3857": _to_float(result.get("tangent_deg_3857")),
        }
        factors = result.get("factors")
        rows.append((row, factors if isinstance(factors, dict) else {}))



    logger.debug(f"Inserting {len(rows)} scenicness points")

    factor_map: Dict[str, int] = {}
    factor_result = await session.execute(text("SELECT id, name FROM scenicness_factors"))
    for factor_id, name in factor_result.fetchall():
        if isinstance(name, str) and isinstance(factor_id, int):
            factor_map[name] = factor_id

    insert_point_sql = text(
        """
        INSERT INTO scenicness_points (
            geom,
            bridge,
            edge_id,
            fraction,
            tangent_dx,
            tangent_dy,
            tangent_deg_4326,
            tangent_deg_3857
        )
        VALUES (
            ST_SetSRID(ST_MakePoint(:lon, :lat), 4326),
            CAST(:bridge AS boolean),
            CAST(:edge_id AS bigint),
            CAST(:fraction AS double precision),
            CAST(:tangent_dx AS double precision),
            CAST(:tangent_dy AS double precision),
            CAST(:tangent_deg_4326 AS double precision),
            CAST(:tangent_deg_3857 AS double precision)
        )
        RETURNING id
        """
    )

    insert_factor_sql = text(
        """
        INSERT INTO scenicness_point_factor_values (
            point_id,
            factor_id,
            left_value,
            right_value,
            relative_value
        )
        VALUES (
            :point_id,
            :factor_id,
            CAST(:left_value AS double precision),
            CAST(:right_value AS double precision),
            CAST(:relative_value AS double precision)
        )
        """
    )

    for row, factors in rows:
        result = await session.execute(insert_point_sql, row)
        point_id = result.scalar_one()

        if not isinstance(factors, dict):
            continue
        for factor_name, values in factors.items():
            if not isinstance(factor_name, str):
                continue
            factor_id = factor_map.get(factor_name)
            if factor_id is None:
                continue
            if not isinstance(values, dict):
                continue
            await session.execute(
                insert_factor_sql,
                {
                    "point_id": point_id,
                    "factor_id": factor_id,
                    "left_value": _to_float(values.get("left_value")),
                    "right_value": _to_float(values.get("right_value")),
                    "relative_value": _to_float(values.get("relative_value")),
                },
            )

    await session.commit()

    if refresh_segments:
        await refresh_scenicness_segments(session)

    logger.debug(f"Inserted {len(rows)} scenicness points")
    return len(rows)


async def refresh_scenicness_segments(session: AsyncSession) -> None:
    """Refresh the materialised scenicness_segments table."""
    logger.debug("Refreshing scenicness_segments table")
    await session.execute(text("SELECT refresh_scenicness_segments()"))
    await session.commit()