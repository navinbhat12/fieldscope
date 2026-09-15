-- Serving schema. Versioned as plain DDL rather than Alembic migrations: there
-- are three tables, one writer (scripts/load_serving.py), and no data that
-- cannot be regenerated from Parquet in under an hour. Migrations earn their
-- keep when a schema change has to preserve data it would otherwise destroy;
-- here the recovery for any schema change is to drop and reload. Revisit if
-- that ever stops being true -- most likely once a loaded VM exists and an
-- hour of reload time starts to matter.

CREATE EXTENSION IF NOT EXISTS postgis;

-- The precomputed overlay: 155,025 rows, one per
-- (mukey, crop_code, drought_class) combination present in Indiana. This is
-- the output of the Spark join and the only thing a request ultimately reads.
CREATE TABLE IF NOT EXISTS overlay (
    mukey           text             NOT NULL,
    musym           text,
    areasymbol      text,
    crop_code       smallint         NOT NULL,
    drought_class   integer          NOT NULL,
    pixels          bigint           NOT NULL,
    acres           double precision NOT NULL,
    land_cover      text             NOT NULL,
    is_agricultural boolean          NOT NULL
);

-- Soil map unit geometry, 1,482,366 polygons. Stored in EPSG:5070 (Albers
-- equal-area) rather than the EPSG:4326 the source Parquet uses, for two
-- reasons: areas computed from lat/lon degrees are meaningless, and the batch
-- join already ran in 5070 (docs/DESIGN.md §5.2), so acres computed here match
-- acres computed there instead of differing by a projection.
CREATE TABLE IF NOT EXISTS soil_polygon (
    mukey text                          NOT NULL,
    geom  geometry(MultiPolygon, 5070)  NOT NULL
);

-- Total area per map unit, precomputed at load.
--
-- POST /area needs, for each map unit a drawn polygon touches, the fraction of
-- that unit falling inside the polygon. The denominator is the unit's total
-- area across every polygon of it in the state -- a map unit is not one shape
-- but many scattered ones, and some have hundreds. Computing that per request
-- would mean scanning all of a unit's geometry just to divide by it, which is
-- exactly the kind of work the batch/serving split exists to avoid.
CREATE TABLE IF NOT EXISTS mukey_area (
    mukey    text             PRIMARY KEY,
    total_m2 double precision NOT NULL,
    polygons integer          NOT NULL
);
