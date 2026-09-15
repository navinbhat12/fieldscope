-- Indexes, applied after bulk load rather than before it.
--
-- Building a GiST index over 1.48M polygons once at the end is dramatically
-- cheaper than maintaining it across 1.48M inserts, which is why this is a
-- separate file the loader runs as its final step rather than part of
-- schema.sql.

CREATE INDEX IF NOT EXISTS overlay_mukey_idx      ON overlay (mukey);
CREATE INDEX IF NOT EXISTS soil_polygon_mukey_idx ON soil_polygon (mukey);

-- The index the whole §5.6 design rests on: it turns "which map units does
-- this drawn field touch" from a 1.48M-row scan into a bounding-box lookup.
CREATE INDEX IF NOT EXISTS soil_polygon_geom_idx  ON soil_polygon USING GIST (geom);

ANALYZE overlay;
ANALYZE soil_polygon;
ANALYZE mukey_area;
