TRUNCATE TABLE osm_ways CASCADE;
TRUNCATE TABLE scenicness_point_factor_values CASCADE;
TRUNCATE TABLE osm_way_nodes;
TRUNCATE TABLE scenicness_points CASCADE;
REFRESH MATERIALIZED VIEW scenicness_segments;