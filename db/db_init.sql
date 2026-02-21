CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Scenicness Points

CREATE TABLE scenicness_points (
    id BIGSERIAL PRIMARY KEY,
    geom GEOMETRY(Point, 4326) NOT NULL,
    bridge BOOLEAN NOT NULL DEFAULT FALSE,
    edge_id BIGINT,
    fraction DOUBLE PRECISION,
    tangent_dx DOUBLE PRECISION,
    tangent_dy DOUBLE PRECISION,
    tangent_deg_4326 DOUBLE PRECISION,
    tangent_deg_3857 DOUBLE PRECISION
);

CREATE INDEX scenicness_points_geom_gix ON scenicness_points USING gist (geom);
CREATE INDEX scenicness_points_edge_id_idx ON scenicness_points (edge_id);

-- Scenicness Factors

CREATE TABLE scenicness_factors (
  id SMALLSERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  weight DOUBLE PRECISION NOT NULL
);

-- Create default factors

INSERT INTO scenicness_factors (name, weight) VALUES
  ('visible_area', 0.1),
  ('average_visibility', 0),
  ('elevation_difference', 0.35),
  ('max_elevation', 0.55)
ON CONFLICT (name)
DO UPDATE SET weight = EXCLUDED.weight;

-- Scenicness Factor Values

CREATE TABLE scenicness_point_factor_values (
  point_id BIGINT NOT NULL REFERENCES scenicness_points(id) ON DELETE CASCADE,
  factor_id SMALLINT NOT NULL REFERENCES scenicness_factors(id) ON DELETE CASCADE,
  left_value DOUBLE PRECISION,
  right_value DOUBLE PRECISION,
  relative_value DOUBLE PRECISION,
  PRIMARY KEY (point_id, factor_id)
);

CREATE INDEX scenicness_point_factor_values_point_id_idx
  ON scenicness_point_factor_values (point_id);

CREATE INDEX scenicness_point_factor_values_factor_id_idx
  ON scenicness_point_factor_values (factor_id);

-- Refresh function

CREATE OR REPLACE FUNCTION refresh_scenicness_segments()
RETURNS void AS $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM information_schema.tables WHERE table_name = 'ways'
  ) THEN
    RAISE NOTICE 'ways table does not exist yet, skipping refresh';
    RETURN;
  END IF;

  -- Step 1: Find junction vertices
  DROP TABLE IF EXISTS _scenicness_tmp_junctions;
  CREATE TABLE _scenicness_tmp_junctions AS
  SELECT vertex_id
  FROM (
    SELECT source AS vertex_id FROM ways WHERE COALESCE(tunnel, FALSE) = FALSE
    UNION ALL
    SELECT target AS vertex_id FROM ways WHERE COALESCE(tunnel, FALSE) = FALSE
  ) v
  GROUP BY vertex_id
  HAVING COUNT(*) != 2;

  CREATE INDEX ON _scenicness_tmp_junctions (vertex_id);

  -- Step 2: Walk edges to build chains
  DROP TABLE IF EXISTS _scenicness_tmp_chain_edges;
  CREATE TABLE _scenicness_tmp_chain_edges (
    chain_id BIGINT,
    edge_id BIGINT,
    seq INT,
    source BIGINT,
    target BIGINT
  );

  WITH RECURSIVE
  seeds AS (
    SELECT e.gid AS edge_id, e.source, e.target, e.gid AS chain_id, 1 AS seq
    FROM ways e
    WHERE COALESCE(e.tunnel, FALSE) = FALSE
      AND e.source IN (SELECT vertex_id FROM _scenicness_tmp_junctions)
  ),
  walk AS (
    SELECT * FROM seeds
    UNION ALL
    SELECT
      next_e.gid,
      CASE WHEN w.target = next_e.source THEN next_e.source ELSE next_e.target END,
      CASE WHEN w.target = next_e.source THEN next_e.target ELSE next_e.source END,
      w.chain_id,
      w.seq + 1
    FROM walk w
    JOIN ways next_e ON (
      COALESCE(next_e.tunnel, FALSE) = FALSE
      AND next_e.gid != w.edge_id
      AND (w.target = next_e.source OR w.target = next_e.target)
    )
    WHERE w.target NOT IN (SELECT vertex_id FROM _scenicness_tmp_junctions)
      AND w.seq < 500
  )
  INSERT INTO _scenicness_tmp_chain_edges
  SELECT chain_id, edge_id, seq, source, target FROM walk;

  CREATE INDEX ON _scenicness_tmp_chain_edges (edge_id);
  CREATE INDEX ON _scenicness_tmp_chain_edges (chain_id, seq);

  -- Step 3: Build chain geometries
  DROP TABLE IF EXISTS _scenicness_tmp_chain_geoms;
  CREATE TABLE _scenicness_tmp_chain_geoms AS
  SELECT
    ce.chain_id,
    ST_LineMerge(ST_Collect(w.the_geom ORDER BY ce.seq)) AS chain_geom
  FROM _scenicness_tmp_chain_edges ce
  JOIN ways w ON w.gid = ce.edge_id
  GROUP BY ce.chain_id;

  CREATE INDEX ON _scenicness_tmp_chain_geoms (chain_id);

  -- Step 4: Build new table (atomic swap)
  DROP TABLE IF EXISTS scenicness_segments_new;

  CREATE TABLE scenicness_segments_new AS
  WITH
  point_chain AS (
    SELECT
      sp.id AS point_id,
      sp.geom AS point_geom,
      sp.tangent_deg_4326,
      sp.tangent_deg_3857,
      ce.chain_id,
      cg.chain_geom,
      ST_LineLocatePoint(cg.chain_geom, sp.geom) AS chain_frac,
      COALESCE(SUM(f.weight * pfv.left_value), 0) AS left_value,
      COALESCE(SUM(f.weight * pfv.right_value), 0) AS right_value
    FROM scenicness_points sp
    JOIN _scenicness_tmp_chain_edges ce ON ce.edge_id = sp.edge_id
    JOIN _scenicness_tmp_chain_geoms cg ON cg.chain_id = ce.chain_id
    LEFT JOIN scenicness_point_factor_values pfv ON pfv.point_id = sp.id
    LEFT JOIN scenicness_factors f ON f.id = pfv.factor_id
    WHERE sp.edge_id IS NOT NULL
      AND sp.fraction IS NOT NULL
    GROUP BY
      sp.id, sp.geom, sp.tangent_deg_4326, sp.tangent_deg_3857,
      ce.chain_id, cg.chain_geom
  ),
  ordered AS (
    SELECT
      pc.*,
      LEAD(pc.point_id) OVER w AS next_id,
      LEAD(pc.point_geom) OVER w AS next_geom,
      LEAD(pc.chain_frac) OVER w AS next_frac,
      LEAD(pc.tangent_deg_4326) OVER w AS next_tangent_4326,
      LEAD(pc.tangent_deg_3857) OVER w AS next_tangent_3857,
      LEAD(pc.left_value) OVER w AS next_left,
      LEAD(pc.right_value) OVER w AS next_right
    FROM point_chain pc
    WINDOW w AS (PARTITION BY pc.chain_id ORDER BY pc.chain_frac)
  ),
  azimuths AS (
    SELECT
      o.*,
      COALESCE(tangent_deg_3857, tangent_deg_4326) AS start_tangent_deg,
      COALESCE(next_tangent_3857, next_tangent_4326) AS end_tangent_deg,
      ST_Azimuth(
        ST_Transform(point_geom, 3857),
        ST_Transform(next_geom, 3857)
      ) AS azimuth_rad,
      ST_LineSubstring(
        chain_geom,
        LEAST(chain_frac, next_frac),
        GREATEST(chain_frac, next_frac)
      ) AS geom
    FROM ordered o
    WHERE next_id IS NOT NULL
  )
  SELECT
    point_id AS start_id,
    next_id AS end_id,
    chain_id,
    geom::geometry(LineString, 4326),
    CASE
      WHEN start_tangent_deg IS NULL THEN left_value
      WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN left_value
      ELSE right_value
    END AS start_left_value,
    CASE
      WHEN start_tangent_deg IS NULL THEN right_value
      WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN right_value
      ELSE left_value
    END AS start_right_value,
    (
      GREATEST(left_value, right_value)
      - ((GREATEST(left_value, right_value) - LEAST(left_value, right_value)) / 10)
    ) AS start_total_value,
    CASE
      WHEN start_tangent_deg IS NULL THEN right_value - left_value
      ELSE (
        CASE
          WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN right_value - left_value
          ELSE left_value - right_value
        END
      )
    END AS start_relative_value,
    CASE
      WHEN end_tangent_deg IS NULL THEN next_left
      WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN next_left
      ELSE next_right
    END AS end_left_value,
    CASE
      WHEN end_tangent_deg IS NULL THEN next_right
      WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN next_right
      ELSE next_left
    END AS end_right_value,
    (
      GREATEST(next_left, next_right)
      - ((GREATEST(next_left, next_right) - LEAST(next_left, next_right)) / 10)
    ) AS end_total_value,
    CASE
      WHEN end_tangent_deg IS NULL THEN next_right - next_left
      ELSE (
        CASE
          WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN next_right - next_left
          ELSE next_left - next_right
        END
      )
    END AS end_relative_value
  FROM azimuths;

  CREATE INDEX scenicness_segments_new_geom_gix
    ON scenicness_segments_new USING gist (geom);

  -- Step 5: Atomic swap
  IF EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_name = 'scenicness_segments'
      AND table_type = 'BASE TABLE'
  ) THEN
    ALTER TABLE scenicness_segments RENAME TO scenicness_segments_old;
    ALTER INDEX IF EXISTS scenicness_segments_geom_gix RENAME TO scenicness_segments_old_geom_gix;
    ALTER TABLE scenicness_segments_new RENAME TO scenicness_segments;
    ALTER INDEX scenicness_segments_new_geom_gix RENAME TO scenicness_segments_geom_gix;
    DROP TABLE scenicness_segments_old;
  ELSE
    -- Drop the old table if it exists (from previous schema)
    DROP TABLE IF EXISTS scenicness_segments;
    ALTER TABLE scenicness_segments_new RENAME TO scenicness_segments;
    ALTER INDEX scenicness_segments_new_geom_gix RENAME TO scenicness_segments_geom_gix;
  END IF;

  -- Cleanup temp tables
  DROP TABLE _scenicness_tmp_chain_edges;
  DROP TABLE _scenicness_tmp_chain_geoms;
  DROP TABLE _scenicness_tmp_junctions;

  RAISE NOTICE 'scenicness_segments table refreshed';
END;
$$ LANGUAGE plpgsql;