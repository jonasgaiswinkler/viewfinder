from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger


async def sample_points_along_ways(
    session: AsyncSession,
    spacing_m: float = 200.0,
    bounding_box: Optional[Dict[str, float]] = None,
) -> List[Dict[str, float | int | str | None]]:
    if spacing_m <= 0:
        raise ValueError("spacing_m must be greater than 0")

    tangent_step_m = max(1.0, min(10.0, spacing_m * 0.05))
    segmentize_m = max(2.0, min(10.0, spacing_m * 0.05))

    params: Dict[str, float | None] = {
        "spacing_m": float(spacing_m),
        "tangent_step_m": float(tangent_step_m),
        "segmentize_m": float(segmentize_m),
        "min_lon": None,
        "min_lat": None,
        "max_lon": None,
        "max_lat": None,
    }

    if bounding_box:
        params.update(
            {
                "min_lon": bounding_box.get("min_lon"),
                "min_lat": bounding_box.get("min_lat"),
                "max_lon": bounding_box.get("max_lon"),
                "max_lat": bounding_box.get("max_lat"),
            }
        )

    sql = text(
        """
        WITH filtered AS (
            SELECT
                gid,
                COALESCE(bridge, FALSE) AS bridge,
                the_geom AS geom
            FROM ways
            WHERE COALESCE(tunnel, FALSE) IS DISTINCT FROM TRUE
              AND (
                    CAST(:min_lon AS double precision) IS NULL
                    OR the_geom && ST_MakeEnvelope(
                        CAST(:min_lon AS double precision),
                        CAST(:min_lat AS double precision),
                        CAST(:max_lon AS double precision),
                        CAST(:max_lat AS double precision),
                        4326
                    )
              )
        ),
        lengths AS (
            SELECT
                gid,
                bridge,
                geom,
                ST_Segmentize(geom::geography, CAST(:segmentize_m AS double precision)) AS geom_geog,
                ST_Length(ST_Segmentize(geom::geography, CAST(:segmentize_m AS double precision))) AS length_m,
                CASE
                    WHEN ST_Length(geom::geography) > 0 THEN
                        GREATEST(
                            0.0,
                            (ST_Length(geom::geography)
                             - (FLOOR(ST_Length(geom::geography) / CAST(:spacing_m AS double precision))
                                * CAST(:spacing_m AS double precision))) / 2.0
                        )
                    ELSE 0.0
                END AS offset_m
            FROM filtered
            WHERE geom IS NOT NULL
              AND ST_GeometryType(geom) = 'ST_LineString'
        ),
        samples AS (
            SELECT
                gid,
                bridge,
                length_m,
                gs.dist_m::double precision AS dist_m,
                (gs.dist_m::double precision) / length_m AS frac
            FROM lengths
            JOIN LATERAL generate_series(
                length_m::numeric * 0.0 + CAST(offset_m AS numeric),
                length_m::numeric,
                CAST(:spacing_m AS numeric)
            ) AS gs(dist_m) ON true
            WHERE length_m > 0
        )
        SELECT
            ST_Y(point::geometry) AS lat,
            ST_X(point::geometry) AS lon,
            ST_Y(point_next::geometry) - ST_Y(point_prev::geometry) AS tangent_lat,
            ST_X(point_next::geometry) - ST_X(point_prev::geometry) AS tangent_lon,
            DEGREES(ST_Azimuth(point_prev, point_next)) AS tangent_azimuth_deg,
            DEGREES(ST_Azimuth(point_prev_3857, point_next_3857)) AS tangent_azimuth_deg_3857,
            s.bridge,
            s.gid AS edge_id,
            s.frac AS fraction
        FROM (
            SELECT
                samples.gid,
                samples.dist_m,
                samples.frac,
                samples.bridge,
                ST_LineInterpolatePoint(lengths.geom_geog, samples.frac) AS point,
                ST_LineInterpolatePoint(
                    lengths.geom_geog,
                    GREATEST(0.0, (samples.dist_m - :tangent_step_m) / lengths.length_m)
                ) AS point_prev,
                ST_LineInterpolatePoint(
                    lengths.geom_geog,
                    LEAST(1.0, (samples.dist_m + :tangent_step_m) / lengths.length_m)
                ) AS point_next,
                ST_Transform(
                    ST_LineInterpolatePoint(
                        lengths.geom_geog,
                        GREATEST(0.0, (samples.dist_m - :tangent_step_m) / lengths.length_m)
                    )::geometry,
                    3857
                ) AS point_prev_3857,
                ST_Transform(
                    ST_LineInterpolatePoint(
                        lengths.geom_geog,
                        LEAST(1.0, (samples.dist_m + :tangent_step_m) / lengths.length_m)
                    )::geometry,
                    3857
                ) AS point_next_3857
            FROM samples
            JOIN lengths ON lengths.gid = samples.gid
        ) AS s
        WHERE (
            CAST(:min_lon AS double precision) IS NULL
            OR ST_Intersects(
                s.point,
                ST_MakeEnvelope(
                    CAST(:min_lon AS double precision),
                    CAST(:min_lat AS double precision),
                    CAST(:max_lon AS double precision),
                    CAST(:max_lat AS double precision),
                    4326
                )
            )
        )
        ORDER BY s.gid, s.dist_m
        """
    )

    result = await session.execute(sql, params)
    rows: Sequence[Row] = result.fetchall()
    logger.debug(f"sample_points_along_ways: {len(rows)} points sampled")
    return [
        {
            "lat": float(row[0]),
            "lon": float(row[1]),
            "tangent_lat": float(row[2]) if row[2] is not None else None,
            "tangent_lon": float(row[3]) if row[3] is not None else None,
            "tangent_deg_4326": float(row[4]) if row[4] is not None else None,
            "tangent_deg_3857": float(row[5]) if row[5] is not None else None,
            "bridge": bool(row[6]) if row[6] is not None else False,
            "edge_id": int(row[7]),
            "fraction": float(row[8]),
        }
        for row in rows
    ]
