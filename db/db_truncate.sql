TRUNCATE TABLE osm_ways CASCADE;
TRUNCATE TABLE osm_way_nodes;
TRUNCATE TABLE scenicness_points;
REFRESH MATERIALIZED VIEW scenicness_segments;