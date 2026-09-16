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

-- Map unit attributes, from SSURGO's tabular half (scripts/download_soil_attributes.py).
--
-- The geometry download carries only mukey, musym and areasymbol -- an
-- identifier and a shape. Without this table the serving tier can say how a
-- field divides across map units but nothing about the ground itself, which
-- makes every number it returns uninterpretable: "59% shrubland" means one
-- thing on class 3 cropland and something else entirely on class 7 rangeland.
--
-- A dimension table keyed on the same mukey the join already carries, so it
-- costs one small load and nothing in the batch pipeline.
CREATE TABLE IF NOT EXISTS mapunit_attr (
    mukey         text PRIMARY KEY,
    muname        text,
    -- USDA land capability class, 1-8: 1-4 cultivable, 5-8 not. Rainfed and
    -- irrigated are separate ratings and both are kept -- in California the
    -- irrigated one is usually the meaningful figure, but it is null where
    -- the ground cannot be irrigated at all, and that absence is an answer
    -- rather than a gap.
    cap_rainfed   smallint,
    cap_irrigated smallint,
    drainage      text,
    -- Available water storage to 150 cm, in cm.
    water_storage double precision,
    -- Representative slope gradient, percent. Fractional, not integral.
    slope_pct     double precision
);
