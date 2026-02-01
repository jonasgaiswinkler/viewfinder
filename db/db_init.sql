CREATE TYPE osm_way_type AS ENUM ('railway', 'road', 'path');

CREATE TABLE osm_ways (
    way_id BIGINT PRIMARY KEY,
    way_type osm_way_type NOT NULL,
    tunnel BOOLEAN,
    bridge BOOLEAN,
    tags JSONB,
    geom GEOMETRY(LINESTRING, 4326)
);

CREATE TABLE osm_way_nodes (
  way_id BIGINT NOT NULL REFERENCES osm_ways(way_id),
  node_id BIGINT NOT NULL,
  seq INTEGER NOT NULL,
  PRIMARY KEY (way_id, seq)
);

CREATE INDEX osm_ways_geom_gix ON osm_ways USING gist (geom);
CREATE INDEX osm_way_nodes_node_id_idx ON osm_way_nodes (node_id);
CREATE INDEX osm_way_nodes_way_id ON osm_way_nodes(way_id);

CREATE OR REPLACE VIEW osm_ways_topo AS
WITH RECURSIVE
node_degree AS (
  SELECT node_id, COUNT(DISTINCT way_id) AS degree
  FROM osm_way_nodes
  GROUP BY node_id
),
break_nodes AS (
  SELECT node_id
  FROM node_degree
  WHERE degree <> 2
),
way_endpoints AS (
  SELECT DISTINCT ON (w.way_id)
    w.way_id,
    w.way_type,
    w.tunnel,
    w.bridge,
    w.tags,
    w.geom,
    FIRST_VALUE(n.node_id) OVER (PARTITION BY n.way_id ORDER BY n.seq) AS start_node,
    FIRST_VALUE(n.node_id) OVER (PARTITION BY n.way_id ORDER BY n.seq DESC) AS end_node
  FROM osm_ways w
  JOIN osm_way_nodes n ON n.way_id = w.way_id
  ORDER BY w.way_id
),
non_break AS (
  SELECT *
  FROM way_endpoints
  WHERE (tunnel IS DISTINCT FROM TRUE)
    AND (bridge IS DISTINCT FROM TRUE)
),
break_or_special AS (
  SELECT *
  FROM way_endpoints
  WHERE (tunnel IS TRUE) OR (bridge IS TRUE)
     OR start_node IN (SELECT node_id FROM break_nodes)
     OR end_node   IN (SELECT node_id FROM break_nodes)
),
chainable AS (
  SELECT *
  FROM non_break
  WHERE start_node NOT IN (SELECT node_id FROM break_nodes)
    AND end_node   NOT IN (SELECT node_id FROM break_nodes)
),
node_edges AS (
  SELECT start_node AS a, end_node AS b
  FROM chainable
),
nodes AS (
  SELECT a AS node FROM node_edges
  UNION
  SELECT b AS node FROM node_edges
),
node_components AS (
  SELECT node, node AS comp
  FROM nodes
  UNION
  SELECT CASE WHEN e.a = nc.node THEN e.b ELSE e.a END AS node, nc.comp
  FROM node_edges e
  JOIN node_components nc ON e.a = nc.node OR e.b = nc.node
),
node_component_min AS (
  SELECT node, MIN(comp) AS component_id
  FROM node_components
  GROUP BY node
),
chainable_with_component AS (
  SELECT c.*, ncm.component_id
  FROM chainable c
  JOIN node_component_min ncm ON ncm.node = c.start_node
),
merged AS (
  SELECT
    way_type,
    FALSE AS tunnel,
    FALSE AS bridge,
    JSONB_AGG(tags) AS tags,
    ST_LineMerge(ST_UnaryUnion(ST_Collect(geom))) AS geom
  FROM chainable_with_component
  GROUP BY way_type, component_id
)
SELECT
  ROW_NUMBER() OVER () AS segment_id,
  way_type,
  tunnel,
  bridge,
  tags,
  geom
FROM break_or_special
UNION ALL
SELECT
  ROW_NUMBER() OVER () + (SELECT COUNT(*) FROM break_or_special) AS segment_id,
  way_type,
  tunnel,
  bridge,
  tags,
  geom
FROM merged;

CREATE OR REPLACE VIEW osm_ways_topo_without_bridges AS
WITH RECURSIVE
node_degree AS (
  SELECT node_id, COUNT(DISTINCT way_id) AS degree
  FROM osm_way_nodes
  GROUP BY node_id
),
break_nodes AS (
  SELECT node_id
  FROM node_degree
  WHERE degree <> 2
),
way_endpoints AS (
  SELECT DISTINCT ON (w.way_id)
    w.way_id,
    w.way_type,
    w.tunnel,
    w.bridge,
    w.tags,
    w.geom,
    FIRST_VALUE(n.node_id) OVER (PARTITION BY n.way_id ORDER BY n.seq) AS start_node,
    FIRST_VALUE(n.node_id) OVER (PARTITION BY n.way_id ORDER BY n.seq DESC) AS end_node
  FROM osm_ways w
  JOIN osm_way_nodes n ON n.way_id = w.way_id
  ORDER BY w.way_id
),
non_break AS (
  SELECT *
  FROM way_endpoints
  WHERE (tunnel IS DISTINCT FROM TRUE)
),
break_or_special AS (
  SELECT *
  FROM way_endpoints
  WHERE (tunnel IS TRUE)
     OR start_node IN (SELECT node_id FROM break_nodes)
     OR end_node   IN (SELECT node_id FROM break_nodes)
),
chainable AS (
  SELECT *
  FROM non_break
  WHERE start_node NOT IN (SELECT node_id FROM break_nodes)
    AND end_node   NOT IN (SELECT node_id FROM break_nodes)
),
node_edges AS (
  SELECT start_node AS a, end_node AS b
  FROM chainable
),
nodes AS (
  SELECT a AS node FROM node_edges
  UNION
  SELECT b AS node FROM node_edges
),
node_components AS (
  SELECT node, node AS comp
  FROM nodes
  UNION
  SELECT CASE WHEN e.a = nc.node THEN e.b ELSE e.a END AS node, nc.comp
  FROM node_edges e
  JOIN node_components nc ON e.a = nc.node OR e.b = nc.node
),
node_component_min AS (
  SELECT node, MIN(comp) AS component_id
  FROM node_components
  GROUP BY node
),
chainable_with_component AS (
  SELECT c.*, ncm.component_id
  FROM chainable c
  JOIN node_component_min ncm ON ncm.node = c.start_node
),
merged AS (
  SELECT
    way_type,
    FALSE AS tunnel,
    JSONB_AGG(tags) AS tags,
    ST_LineMerge(ST_UnaryUnion(ST_Collect(geom))) AS geom
  FROM chainable_with_component
  GROUP BY way_type, component_id
)
SELECT
  ROW_NUMBER() OVER () AS segment_id,
  way_type,
  tunnel,
  tags,
  geom
FROM break_or_special
UNION ALL
SELECT
  ROW_NUMBER() OVER () + (SELECT COUNT(*) FROM break_or_special) AS segment_id,
  way_type,
  tunnel,
  tags,
  geom
FROM merged;

CREATE TABLE scenicness_points (
    id bigserial PRIMARY KEY,
    geom geometry(Point, 4326) NOT NULL,
    bridge boolean NOT NULL,
    way_id bigint REFERENCES osm_ways(way_id),
    tangent_dx double precision,
    tangent_dy double precision,
    tangent_deg_4326 double precision,
    tangent_deg_3857 double precision,
    factor_left_visible_area double precision,
    factor_right_visible_area double precision,
    factor_total_visible_area double precision,
    factor_relative_visible_area double precision
);

CREATE INDEX scenicness_points_geom_gix ON scenicness_points USING gist (geom);

CREATE MATERIALIZED VIEW scenicness_segments AS
WITH points AS (
  SELECT
    sp.id,
    sp.way_id,
    topo.segment_id,
    topo.geom AS topo_geom,
    sp.geom,
    sp.tangent_deg_4326,
    sp.tangent_deg_3857,
    sp.factor_left_visible_area,
    sp.factor_right_visible_area,
    sp.factor_total_visible_area,
    sp.factor_relative_visible_area,
    ST_LineLocatePoint(topo.geom, sp.geom) AS frac
  FROM scenicness_points sp
  JOIN LATERAL (
    SELECT segment_id, geom
    FROM osm_ways_topo_without_bridges
    WHERE (tunnel IS DISTINCT FROM TRUE)
      AND geom IS NOT NULL
    ORDER BY geom <-> sp.geom
    LIMIT 1
  ) AS topo ON true
),
ordered AS (
  SELECT
    p.*,
    LEAD(p.id) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_id,
    LEAD(p.geom) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_geom,
    LEAD(p.frac) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_frac,
    LEAD(p.tangent_deg_4326) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_tangent_deg_4326,
    LEAD(p.tangent_deg_3857) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_tangent_deg_3857,
    LEAD(p.factor_left_visible_area) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_factor_left_visible_area,
    LEAD(p.factor_right_visible_area) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_factor_right_visible_area,
    LEAD(p.factor_total_visible_area) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_factor_total_visible_area,
    LEAD(p.factor_relative_visible_area) OVER (PARTITION BY p.segment_id ORDER BY p.frac) AS next_factor_relative_visible_area
  FROM points p
),
pairs AS (
  SELECT
    segment_id,
    topo_geom,
    id AS start_id,
    next_id AS end_id,
    geom AS start_geom,
    next_geom AS end_geom,
    frac AS start_frac,
    next_frac AS end_frac,
    factor_left_visible_area AS start_left,
    factor_right_visible_area AS start_right,
    factor_total_visible_area AS start_total,
    factor_relative_visible_area AS start_relative,
    next_factor_left_visible_area AS end_left,
    next_factor_right_visible_area AS end_right,
    next_factor_total_visible_area AS end_total,
    next_factor_relative_visible_area AS end_relative,
    COALESCE(tangent_deg_3857, tangent_deg_4326) AS start_tangent_deg,
    COALESCE(next_tangent_deg_3857, next_tangent_deg_4326) AS end_tangent_deg
  FROM ordered
  WHERE next_id IS NOT NULL AND next_geom IS NOT NULL
),
azimuths AS (
  SELECT
    *,
    ST_Azimuth(
      ST_Transform(start_geom, 3857),
      ST_Transform(end_geom, 3857)
    ) AS azimuth_rad
  FROM pairs
)
SELECT
  start_id,
  end_id,
  segment_id,
  ST_LineSubstring(
    topo_geom,
    LEAST(start_frac, end_frac),
    GREATEST(start_frac, end_frac)
  )::geometry(LineString, 4326) AS geom,
  CASE
    WHEN start_tangent_deg IS NULL THEN start_left
    WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN start_left
    ELSE start_right
  END AS start_factor_left_visible_area,
  CASE
    WHEN start_tangent_deg IS NULL THEN start_right
    WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN start_right
    ELSE start_left
  END AS start_factor_right_visible_area,
  CASE
    WHEN start_tangent_deg IS NULL THEN start_total
    ELSE start_total
  END AS start_factor_total_visible_area,
  CASE
    WHEN start_tangent_deg IS NULL THEN start_relative
    ELSE (
      CASE
        WHEN COS(RADIANS(start_tangent_deg) - azimuth_rad) >= 0 THEN start_right - start_left
        ELSE start_left - start_right
      END
    )
  END AS start_factor_relative_visible_area,
  CASE
    WHEN end_tangent_deg IS NULL THEN end_left
    WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN end_left
    ELSE end_right
  END AS end_factor_left_visible_area,
  CASE
    WHEN end_tangent_deg IS NULL THEN end_right
    WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN end_right
    ELSE end_left
  END AS end_factor_right_visible_area,
  CASE
    WHEN end_tangent_deg IS NULL THEN end_total
    ELSE end_total
  END AS end_factor_total_visible_area,
  CASE
    WHEN end_tangent_deg IS NULL THEN end_relative
    ELSE (
      CASE
        WHEN COS(RADIANS(end_tangent_deg) - azimuth_rad) >= 0 THEN end_right - end_left
        ELSE end_left - end_right
      END
    )
  END AS end_factor_relative_visible_area
FROM azimuths;

CREATE INDEX scenicness_segments_geom_gix ON scenicness_segments USING gist (geom);