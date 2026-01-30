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