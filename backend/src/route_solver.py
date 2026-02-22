from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger


# -------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------

async def compute_scenic_route(
    session: AsyncSession,
    coordinates: List[List[float]],
) -> Dict[str, Any]:
    """Compute a route through *coordinates* and return a GeoJSON
    FeatureCollection whose LineString segments carry direction-corrected
    scenicness values.

    Parameters
    ----------
    session : AsyncSession
        Active database session.
    coordinates : list of [lon, lat]
        Ordered waypoints (at least 2).

    Returns
    -------
    dict – GeoJSON FeatureCollection with a ``route`` feature followed by
    ``segment`` features between consecutive scenicness points.
    """
    if len(coordinates) < 2:
        raise ValueError("At least two coordinates are required")

    # ---- 1. Snap each waypoint to the closest point on the nearest way -
    snap_points: List[Tuple[int, float]] = []  # (edge_gid, fraction)
    for coord in coordinates:
        lon, lat = float(coord[0]), float(coord[1])
        snap = await _snap_to_nearest_way(session, lon, lat)
        snap_points.append(snap)

    logger.debug(f"compute_scenic_route: snap points = {snap_points}")

    # ---- 2. Route between consecutive snap points ----------------------
    edge_gids: List[int] = []
    forwards: List[bool] = []

    for i in range(len(snap_points) - 1):
        start = snap_points[i]
        end = snap_points[i + 1]
        if start[0] == end[0] and abs(start[1] - end[1]) < 1e-9:
            continue  # identical snap – skip
        leg = await _route_leg(session, start, end)
        for gid, fwd in leg:
            # De-duplicate at waypoint junctions
            if edge_gids and edge_gids[-1] == gid:
                continue
            edge_gids.append(gid)
            forwards.append(fwd)

    if not edge_gids:
        return {"type": "FeatureCollection", "features": []}

    logger.debug(f"compute_scenic_route: route has {len(edge_gids)} edges")

    # ---- 3. Detect train reversals (cumulative left/right flip) --------
    flipped = await _compute_flip_states(session, edge_gids, forwards)
    logger.debug(f"compute_scenic_route: reversals detected at edges {[i for i, f in enumerate(flipped) if f]}")

    # ---- 4. Build GeoJSON with scenicness segments ---------------------
    first_snap = snap_points[0]
    last_snap = snap_points[-1]
    return await _build_scenicness_geojson(
        session, edge_gids, forwards, flipped, first_snap, last_snap,
    )


# -------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------

async def _snap_to_nearest_way(
    session: AsyncSession, lon: float, lat: float,
) -> Tuple[int, float]:
    """Return ``(edge_gid, fraction)`` for the closest point on the
    nearest way to *(lon, lat)*."""
    result = await session.execute(
        text("""
            SELECT
                gid,
                ST_LineLocatePoint(
                    the_geom,
                    ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                ) AS fraction
            FROM ways
            ORDER BY the_geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
            LIMIT 1
        """),
        {"lon": lon, "lat": lat},
    )
    row = result.fetchone()
    if row is None:
        raise ValueError(f"No way found near ({lon}, {lat})")
    return int(row[0]), float(row[1])


async def _route_leg(
    session: AsyncSession,
    start: Tuple[int, float],
    end: Tuple[int, float],
) -> List[Tuple[int, bool]]:
    """Route from one snap point to another using ``pgr_withPoints``.

    *start* / *end* are ``(edge_gid, fraction)`` tuples.
    Returns ``[(edge_gid, forward), …]``.
    """
    se, sf = int(start[0]), float(start[1])
    ee, ef = int(end[0]), float(end[1])

    # Build the inner points SQL. Values are int / float so safe to inline.
    points_sql = (
        f"SELECT * FROM (VALUES "
        f"(1, {se}::bigint, {sf}::float8, 'b'::char), "
        f"(2, {ee}::bigint, {ef}::float8, 'b'::char)"
        f") AS t(pid, edge_id, fraction, side)"
    )

    sql = text(f"""
        WITH routing AS (
            SELECT
                seq, node, edge,
                LEAD(node) OVER (ORDER BY seq) AS next_node
            FROM pgr_withPoints(
                'SELECT gid AS id, source, target, cost, reverse_cost FROM ways',
                $wp${points_sql}$wp$,
                -1, -2,
                directed := false
            )
        )
        SELECT r.edge, r.node, r.next_node, w.source, w.target
        FROM routing r
        JOIN ways w ON w.gid = r.edge
        WHERE r.edge > 0
        ORDER BY r.seq
    """)

    result = await session.execute(sql)
    rows = result.fetchall()

    edges: List[Tuple[int, bool]] = []
    for edge, node, next_node, source, target in rows:
        if node == source:
            fwd = True
        elif node == target:
            fwd = False
        elif next_node is not None and next_node == target:
            fwd = True
        elif next_node is not None and next_node == source:
            fwd = False
        else:
            # Both endpoints are virtual (same edge) – compare fractions.
            fwd = sf <= ef
        edges.append((int(edge), fwd))

    return edges


async def _compute_flip_states(
    session: AsyncSession,
    edge_gids: List[int],
    forwards: List[bool],
) -> List[bool]:
    """Detect train reversals and return a per-edge flip flag.

    A reversal occurs when the heading change between consecutive edges
    exceeds 90°.  Each reversal toggles the cumulative flip state.
    ``flipped[i] == True`` means that, from the passengers' perspective,
    left and right are swapped compared to the initial travel direction.
    """
    n = len(edge_gids)
    if n <= 1:
        return [False] * n

    result = await session.execute(
        text("""
            WITH edges AS (
                SELECT
                    ordinality AS idx,
                    gid_val    AS edge_gid,
                    fwd_val    AS forward
                FROM unnest(
                    CAST(:edge_gids AS bigint[]),
                    CAST(:forwards  AS boolean[])
                ) WITH ORDINALITY AS t(gid_val, fwd_val)
            )
            SELECT
                e.idx,
                /* exit heading (direction of travel at the end of this edge) */
                CASE WHEN e.forward
                    THEN degrees(ST_Azimuth(
                        ST_PointN(w.the_geom, GREATEST(ST_NPoints(w.the_geom) - 1, 1)),
                        ST_EndPoint(w.the_geom)))
                    ELSE degrees(ST_Azimuth(
                        ST_PointN(w.the_geom, LEAST(2, ST_NPoints(w.the_geom))),
                        ST_StartPoint(w.the_geom)))
                END AS exit_heading,
                /* entry heading (direction of travel at the start of this edge) */
                CASE WHEN e.forward
                    THEN degrees(ST_Azimuth(
                        ST_StartPoint(w.the_geom),
                        ST_PointN(w.the_geom, LEAST(2, ST_NPoints(w.the_geom)))))
                    ELSE degrees(ST_Azimuth(
                        ST_EndPoint(w.the_geom),
                        ST_PointN(w.the_geom, GREATEST(ST_NPoints(w.the_geom) - 1, 1))))
                END AS entry_heading
            FROM edges e
            JOIN ways w ON w.gid = e.edge_gid
            ORDER BY e.idx
        """),
        {"edge_gids": edge_gids, "forwards": forwards},
    )
    rows = result.fetchall()
    exit_headings = [float(r[1]) if r[1] is not None else 0.0 for r in rows]
    entry_headings = [float(r[2]) if r[2] is not None else 0.0 for r in rows]

    flipped = [False] * n
    cumulative = 0
    for i in range(1, n):
        diff = abs(exit_headings[i - 1] - entry_headings[i])
        if diff > 180:
            diff = 360 - diff
        if diff > 90:  # heading reversed
            cumulative += 1
        flipped[i] = (cumulative % 2) == 1

    return flipped


async def _build_scenicness_geojson(
    session: AsyncSession,
    edge_gids: List[int],
    forwards: List[bool],
    flipped: List[bool],
    first_snap: Tuple[int, float],
    last_snap: Tuple[int, float],
) -> Dict[str, Any]:
    """Query scenicness data along the route and assemble a GeoJSON
    FeatureCollection of scenicness segments.
    """
    features: List[Dict[str, Any]] = []

    params: Dict[str, Any] = {
        "edge_gids": edge_gids,
        "forwards": forwards,
        "flipped": flipped,
        "first_frac": first_snap[1],
        "last_frac": last_snap[1],
    }

    # ---- Scenicness segment features -----------------------------------
    seg_result = await session.execute(text(_SEGMENTS_SQL), params)

    total_length = 0.0
    weighted_left = 0.0
    weighted_right = 0.0
    weighted_total = 0.0
    weighted_relative = 0.0

    for row in seg_result.fetchall():
        geom = row[2]
        if isinstance(geom, str):
            geom = json.loads(geom)

        sl = _f(row[5]) or 0.0
        sr = _f(row[6]) or 0.0
        st = _f(row[7]) or 0.0
        srel = _f(row[8]) or 0.0
        el = _f(row[9]) or 0.0
        er = _f(row[10]) or 0.0
        et = _f(row[11]) or 0.0
        erel = _f(row[12]) or 0.0
        length_m = float(row[13]) if row[13] is not None else 0.0

        # Accumulate length-weighted sums (segment value = avg of endpoints)
        total_length += length_m
        weighted_left += ((sl + el) / 2) * length_m
        weighted_right += ((sr + er) / 2) * length_m
        weighted_total += ((st + et) / 2) * length_m
        weighted_relative += ((srel + erel) / 2) * length_m

        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "type": "segment",
                "start_id": int(row[0]),
                "end_id": int(row[1]),
                "flipped": bool(row[3]),
                "is_tunnel": bool(row[4]),
                "start_left_value": _f(row[5]),
                "start_right_value": _f(row[6]),
                "start_total_value": _f(row[7]),
                "start_relative_value": _f(row[8]),
                "end_left_value": _f(row[9]),
                "end_right_value": _f(row[10]),
                "end_total_value": _f(row[11]),
                "end_relative_value": _f(row[12]),
                "length_m": length_m,
            },
        })

    # ---- Compute length-weighted averages ------------------------------
    if total_length > 0:
        avg_left = weighted_left / total_length
        avg_right = weighted_right / total_length
        avg_total = weighted_total / total_length
        avg_relative = weighted_relative / total_length
    else:
        avg_left = avg_right = avg_total = avg_relative = 0.0

    return {
        "type": "FeatureCollection",
        "features": features,
        "properties": {
            "total_length_m": round(total_length, 1),
            "avg_left_value": round(avg_left, 4),
            "avg_right_value": round(avg_right, 4),
            "avg_total_value": round(avg_total, 4),
            "avg_relative_value": round(avg_relative, 4),
        },
    }


def _f(val: Any) -> Optional[float]:
    """Coerce a DB value to ``float`` or ``None``."""
    if val is None:
        return None
    return float(val)


# -------------------------------------------------------------------
# SQL templates
#
# Both queries share a common preamble that builds the (trimmed) route
# line from the ordered edge list.  The first and last edges are
# clipped to the snap-point fractions so the route geometry starts /
# ends exactly where the user clicked.
# -------------------------------------------------------------------

# ---- shared CTE preamble (builds ``route_line``) -------------------
_ROUTE_LINE_CTE = """
route_edges AS (
    SELECT
        ordinality AS edge_order,
        gid_val    AS edge_gid,
        fwd_val    AS forward,
        flip_val   AS flipped,
        (fwd_val <> flip_val) AS effective_fwd,
        COALESCE(w0.tunnel, FALSE) AS is_tunnel
    FROM unnest(
        CAST(:edge_gids AS bigint[]),
        CAST(:forwards  AS boolean[]),
        CAST(:flipped   AS boolean[])
    ) WITH ORDINALITY AS t(gid_val, fwd_val, flip_val)
    JOIN ways w0 ON w0.gid = t.gid_val
),
total_edges AS (
    SELECT COUNT(*)::int AS cnt FROM route_edges
),
route_geoms AS (
    SELECT
        re.edge_order,
        CASE
            /* -- single-edge route ---------------------------------- */
            WHEN te.cnt = 1 AND re.forward THEN
                ST_LineSubstring(w.the_geom,
                    CAST(:first_frac AS float8),
                    CAST(:last_frac  AS float8))
            WHEN te.cnt = 1 AND NOT re.forward THEN
                ST_Reverse(ST_LineSubstring(w.the_geom,
                    CAST(:last_frac  AS float8),
                    CAST(:first_frac AS float8)))

            /* -- first edge ----------------------------------------- */
            WHEN re.edge_order = 1 AND re.forward THEN
                ST_LineSubstring(w.the_geom,
                    CAST(:first_frac AS float8), 1.0)
            WHEN re.edge_order = 1 AND NOT re.forward THEN
                ST_Reverse(ST_LineSubstring(w.the_geom,
                    0.0, CAST(:first_frac AS float8)))

            /* -- last edge ------------------------------------------ */
            WHEN re.edge_order = te.cnt AND re.forward THEN
                ST_LineSubstring(w.the_geom,
                    0.0, CAST(:last_frac AS float8))
            WHEN re.edge_order = te.cnt AND NOT re.forward THEN
                ST_Reverse(ST_LineSubstring(w.the_geom,
                    CAST(:last_frac AS float8), 1.0))

            /* -- middle edges --------------------------------------- */
            WHEN re.forward THEN w.the_geom
            ELSE ST_Reverse(w.the_geom)
        END AS geom
    FROM route_edges re
    CROSS JOIN total_edges te
    JOIN ways w ON w.gid = re.edge_gid
),
route_line AS (
    SELECT ST_LineMerge(ST_Collect(geom ORDER BY edge_order)) AS geom
    FROM route_geoms
)
"""

_SEGMENTS_SQL = f"""
WITH
{_ROUTE_LINE_CTE},
/* ---- tunnel detection ----------------------------------------- */
tunnel_groups AS (
    SELECT
        re.edge_order,
        re.edge_order
            - ROW_NUMBER() OVER (ORDER BY re.edge_order) AS grp
    FROM route_edges re
    WHERE re.is_tunnel
),
tunnel_sections AS (
    SELECT
        tg.grp,
        MIN(tg.edge_order) AS first_edge_order,
        MAX(tg.edge_order) AS last_edge_order
    FROM tunnel_groups tg
    GROUP BY tg.grp
),
tunnel_fracs AS (
    SELECT
        ts.grp,
        ts.first_edge_order,
        ts.last_edge_order,
        ST_LineLocatePoint(rl.geom, ST_StartPoint(rg_first.geom)) AS start_frac,
        ST_LineLocatePoint(rl.geom, ST_EndPoint(rg_last.geom))    AS end_frac
    FROM tunnel_sections ts
    CROSS JOIN route_line rl
    JOIN route_geoms rg_first ON rg_first.edge_order = ts.first_edge_order
    JOIN route_geoms rg_last  ON rg_last.edge_order  = ts.last_edge_order
),
tunnel_boundary_points AS (
    /* entry boundary */
    SELECT
        -(tf.grp * 2 + 1)::bigint               AS point_id,
        ST_LineInterpolatePoint(rl.geom, tf.start_frac) AS point_geom,
        0.0::float8                              AS edge_frac,
        tf.first_edge_order                      AS edge_order,
        TRUE                                     AS forward,
        COALESCE(re_before.flipped, FALSE)       AS flipped,
        TRUE                                     AS effective_fwd,
        0.0::float8                              AS raw_left,
        0.0::float8                              AS raw_right,
        tf.start_frac                            AS route_frac,
        TRUE                                     AS is_tunnel_boundary
    FROM tunnel_fracs tf
    CROSS JOIN route_line rl
    LEFT JOIN route_edges re_before
        ON re_before.edge_order = tf.first_edge_order - 1
    UNION ALL
    /* exit boundary */
    SELECT
        -(tf.grp * 2 + 2)::bigint               AS point_id,
        ST_LineInterpolatePoint(rl.geom, tf.end_frac) AS point_geom,
        1.0::float8                              AS edge_frac,
        tf.last_edge_order                       AS edge_order,
        TRUE                                     AS forward,
        COALESCE(re_after.flipped, FALSE)        AS flipped,
        TRUE                                     AS effective_fwd,
        0.0::float8                              AS raw_left,
        0.0::float8                              AS raw_right,
        tf.end_frac                              AS route_frac,
        TRUE                                     AS is_tunnel_boundary
    FROM tunnel_fracs tf
    CROSS JOIN route_line rl
    LEFT JOIN route_edges re_after
        ON re_after.edge_order = tf.last_edge_order + 1
),
/* ---- scenicness points + tunnel boundaries -------------------- */
route_points AS (
    SELECT
        sp.id                 AS point_id,
        sp.geom               AS point_geom,
        sp.fraction           AS edge_frac,
        re.edge_order,
        re.forward,
        re.flipped,
        re.effective_fwd,
        COALESCE(SUM(f.weight * pfv.left_value),  0) AS raw_left,
        COALESCE(SUM(f.weight * pfv.right_value), 0) AS raw_right,
        ST_LineLocatePoint(rl.geom, sp.geom)          AS route_frac,
        FALSE AS is_tunnel_boundary
    FROM scenicness_points sp
    CROSS JOIN route_line rl
    JOIN route_edges re ON re.edge_gid = sp.edge_id
    LEFT JOIN scenicness_point_factor_values pfv ON pfv.point_id = sp.id
    LEFT JOIN scenicness_factors f               ON f.id        = pfv.factor_id
    WHERE sp.edge_id  IS NOT NULL
      AND sp.fraction  IS NOT NULL
    GROUP BY sp.id, sp.geom, sp.fraction,
             re.edge_order, re.forward, re.flipped, re.effective_fwd, rl.geom
    UNION ALL
    SELECT * FROM tunnel_boundary_points
),
ordered_points AS (
    SELECT
        rp.*,
        LEAD(rp.point_id)              OVER w AS next_id,
        LEAD(rp.point_geom)            OVER w AS next_geom,
        LEAD(rp.route_frac)            OVER w AS next_frac,
        LEAD(rp.effective_fwd)         OVER w AS next_effective_fwd,
        LEAD(rp.flipped)               OVER w AS next_flipped,
        LEAD(rp.raw_left)              OVER w AS next_raw_left,
        LEAD(rp.raw_right)             OVER w AS next_raw_right,
        LEAD(rp.is_tunnel_boundary)    OVER w AS next_is_tunnel_boundary
    FROM route_points rp
    WINDOW w AS (ORDER BY rp.route_frac)
),
segments AS (
    SELECT
        o.point_id   AS start_id,
        o.next_id    AS end_id,
        o.route_frac,
        o.effective_fwd                                        AS start_eff,
        COALESCE(o.next_effective_fwd, o.effective_fwd)         AS end_eff,
        o.flipped                                              AS start_flipped,
        o.is_tunnel_boundary                                   AS start_is_tb,
        COALESCE(o.next_is_tunnel_boundary, FALSE)              AS end_is_tb,
        ST_LineSubstring(
            rl.geom,
            LEAST(o.route_frac, o.next_frac),
            GREATEST(o.route_frac, o.next_frac)
        ) AS geom,
        o.raw_left       AS start_raw_left,
        o.raw_right      AS start_raw_right,
        o.next_raw_left  AS end_raw_left,
        o.next_raw_right AS end_raw_right
    FROM ordered_points o
    CROSS JOIN route_line rl
    WHERE o.next_id IS NOT NULL
),
segments_with_values AS (
    SELECT
        s.*,
        /* ---- normal (direction-corrected) start point values ---- */
        CASE WHEN s.start_eff THEN s.start_raw_left  ELSE s.start_raw_right END AS n_start_left,
        CASE WHEN s.start_eff THEN s.start_raw_right ELSE s.start_raw_left  END AS n_start_right,
        GREATEST(s.start_raw_left, s.start_raw_right)
            - (GREATEST(s.start_raw_left, s.start_raw_right)
               - LEAST(s.start_raw_left, s.start_raw_right)) / 10               AS n_start_total,
        CASE WHEN s.start_eff
            THEN s.start_raw_right - s.start_raw_left
            ELSE s.start_raw_left  - s.start_raw_right END                       AS n_start_relative,
        /* ---- normal (direction-corrected) end point values ---- */
        CASE WHEN s.end_eff THEN s.end_raw_left  ELSE s.end_raw_right END       AS n_end_left,
        CASE WHEN s.end_eff THEN s.end_raw_right ELSE s.end_raw_left  END       AS n_end_right,
        GREATEST(s.end_raw_left, s.end_raw_right)
            - (GREATEST(s.end_raw_left, s.end_raw_right)
               - LEAST(s.end_raw_left, s.end_raw_right)) / 10                   AS n_end_total,
        CASE WHEN s.end_eff
            THEN s.end_raw_right - s.end_raw_left
            ELSE s.end_raw_left  - s.end_raw_right END                           AS n_end_relative
    FROM segments s
    WHERE ST_GeometryType(s.geom) = 'ST_LineString'
)
SELECT
    sv.start_id,
    sv.end_id,
    ST_AsGeoJSON(sv.geom)::json AS geometry,
    sv.start_flipped AS flipped,
    (sv.start_is_tb AND sv.end_is_tb) AS is_tunnel,

    /* ---- start point (with tunnel-boundary correction) ---- */
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.start_is_tb THEN sv.n_end_left
         ELSE sv.n_start_left END                                  AS start_left_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.start_is_tb THEN sv.n_end_right
         ELSE sv.n_start_right END                                 AS start_right_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.start_is_tb THEN sv.n_end_total
         ELSE sv.n_start_total END                                 AS start_total_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.start_is_tb THEN sv.n_end_relative
         ELSE sv.n_start_relative END                              AS start_relative_value,

    /* ---- end point (with tunnel-boundary correction) ---- */
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.end_is_tb THEN sv.n_start_left
         ELSE sv.n_end_left END                                    AS end_left_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.end_is_tb THEN sv.n_start_right
         ELSE sv.n_end_right END                                   AS end_right_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.end_is_tb THEN sv.n_start_total
         ELSE sv.n_end_total END                                   AS end_total_value,
    CASE WHEN sv.start_is_tb AND sv.end_is_tb THEN 0
         WHEN sv.end_is_tb THEN sv.n_start_relative
         ELSE sv.n_end_relative END                                AS end_relative_value,

    ST_Length(sv.geom::geography)                                   AS length_m

FROM segments_with_values sv
ORDER BY sv.route_frac
"""
