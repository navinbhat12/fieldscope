"""The SQL behind the two endpoints.

Kept as literal SQL rather than built through the ORM. These are the whole
product -- a spatial intersection and a weighted aggregation -- and they want
to be read, reviewed and EXPLAINed directly, not assembled at runtime out of
Python objects.
"""

from sqlalchemy import text

# One map unit's breakdown. A plain indexed lookup on the 311,726 California
# overlay rows; this is the endpoint that needs no cache (§5.7).
MAPUNIT = text(
    """
    SELECT mukey, musym, areasymbol, crop_code, drought_class,
           land_cover, is_agricultural, pixels, acres
    FROM overlay
    WHERE mukey = :mukey
    ORDER BY acres DESC
    """
)

# Cheap pre-flight for POST /area: reproject the drawn polygon and measure it,
# touching no table at all. Run before the real query so an absurdly large
# polygon is rejected *before* the expensive spatial work rather than after it,
# and so an unparseable geometry fails fast.
# It also reports emptiness and validity. PostGIS accepts a degenerate ring
# such as [[0,0],[1,1]] without raising -- it yields an empty geometry of zero
# area, which would otherwise flow through the whole endpoint and emerge as a
# perfectly ordinary "no data here" answer, indistinguishable from a polygon
# drawn over open water. Bad input has to fail as bad input.
QUERY_AREA = text(
    """
    SELECT ST_Area(g)     AS query_m2,
           ST_IsEmpty(g)  AS is_empty,
           ST_IsValid(g)  AS is_valid
    FROM (
        SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), 5070) AS g
    ) s
    """
)

# The aggregation POST /area exists for.
#
# Three stages. `hits` uses the GiST index to find the map-unit polygons the
# drawn field touches and measures each overlap. `weighted` converts each
# overlap into the fraction of that entire map unit it represents, using the
# precomputed statewide total. The final SELECT multiplies every precomputed
# overlay row for that unit by its fraction and sums across units.
#
# The approximation is in the second stage: knowing that a field covers 30% of
# a map unit, this reports 30% of that unit's land cover. That is exact only if
# land cover is spread evenly within the unit, which it is not. The batch join
# collapsed pixel locations into per-unit totals (§5.1), so this is the most
# the precomputed table can support -- recovering the true answer would mean
# putting the raster back in the request path, which is the entire thing the
# design exists to avoid.
AREA_BREAKDOWN = text(
    """
    WITH q AS (
        SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), 5070) AS geom
    ),
    hits AS (
        SELECT sp.mukey,
               SUM(ST_Area(ST_Intersection(sp.geom, q.geom))) AS inter_m2
        FROM soil_polygon sp
        JOIN q ON sp.geom && q.geom AND ST_Intersects(sp.geom, q.geom)
        GROUP BY sp.mukey
    ),
    weighted AS (
        SELECT h.mukey,
               h.inter_m2,
               h.inter_m2 / NULLIF(ma.total_m2, 0) AS fraction
        FROM hits h
        JOIN mukey_area ma ON ma.mukey = h.mukey
    ),
    -- Only map units that actually have overlay rows count as "answered".
    -- The soil table holds more map units than the overlay covers: the
    -- bounding-box download legitimately brought down Arizona, Nevada and
    -- Oregon survey areas (§5.10), which no California land cover record can
    -- describe. Counting those in the total would report coverage 1.0 for a
    -- field drawn on the state line while quietly omitting half of it from
    -- the breakdown.
    totals AS (
        SELECT COALESCE(SUM(w.inter_m2), 0) AS answered_m2,
               COUNT(*)                     AS map_units
        FROM weighted w
        WHERE EXISTS (SELECT 1 FROM overlay o WHERE o.mukey = w.mukey)
    ),
    -- The soil half of the answer, as one row.
    --
    -- Weighted by intersected area per map unit -- not by overlay acres --
    -- because capability is a property of the map unit, and `weighted` has
    -- exactly one row per unit while the overlay has one per crop-drought
    -- combination within it. Weighting by the latter would count a unit once
    -- per crop growing on it.
    --
    -- Filtered to units that have overlay rows, for the same reason `totals`
    -- is: the bounding-box download brought in Arizona, Nevada and Oregon
    -- survey areas (§5.10) that no California land cover record describes,
    -- and rating a field against ground the breakdown refuses to count would
    -- make the two halves disagree.
    soil AS (
        SELECT
            COALESCE(SUM(w.inter_m2) FILTER (WHERE a.mukey IS NOT NULL), 0) AS rated_m2,
            -- USDA capability class 1-4 is "capable of cultivation". The
            -- irrigated rating is preferred where it exists because the AOI
            -- is California, where most farmed ground is irrigated; the
            -- rainfed rating is the fallback, not the default.
            COALESCE(SUM(w.inter_m2) FILTER (
                WHERE COALESCE(a.cap_irrigated, a.cap_rainfed) BETWEEN 1 AND 4), 0)
                AS cultivable_m2,
            -- A null irrigated class means the ground cannot be irrigated at
            -- all, which is an answer rather than a gap.
            COALESCE(SUM(w.inter_m2) FILTER (WHERE a.cap_irrigated IS NOT NULL), 0)
                AS irrigable_m2,
            SUM(w.inter_m2 * a.slope_pct)
                / NULLIF(SUM(w.inter_m2) FILTER (WHERE a.slope_pct IS NOT NULL), 0)
                AS slope_pct,
            SUM(w.inter_m2 * a.water_storage)
                / NULLIF(SUM(w.inter_m2) FILTER (WHERE a.water_storage IS NOT NULL), 0)
                AS water_storage,
            (SELECT a2.muname
               FROM weighted w2 JOIN mapunit_attr a2 ON a2.mukey = w2.mukey
              ORDER BY w2.inter_m2 DESC LIMIT 1) AS dominant_soil,
            (SELECT a2.drainage
               FROM weighted w2 JOIN mapunit_attr a2 ON a2.mukey = w2.mukey
              ORDER BY w2.inter_m2 DESC LIMIT 1) AS dominant_drainage
        FROM weighted w
        LEFT JOIN mapunit_attr a ON a.mukey = w.mukey
        WHERE EXISTS (SELECT 1 FROM overlay o WHERE o.mukey = w.mukey)
    )
    -- Two aggregations of the same rows, in one pass.
    --
    -- The frontend needs land cover *and* drought, but grouping by both at
    -- once would return their cross product -- a Central Valley field already
    -- yields ~67 land cover categories, and multiplying that by the drought
    -- classes present inflates the response for no added information, since
    -- nothing in the UI asks "how many acres of almonds are in D2".
    --
    -- Running two queries instead would be worse: each would re-execute the
    -- `hits` CTE, and that spatial intersection against 484,325 polygons is
    -- the expensive part of this endpoint. GROUPING SETS pays for it once.
    -- `row_kind` says which shape each row is; the totals ride along via
    -- MAX() so they survive both sets without joining the grouping keys.
    SELECT CASE WHEN GROUPING(o.land_cover) = 0 THEN 'cover' ELSE 'drought' END
               AS row_kind,
           o.land_cover,
           o.crop_code,
           o.is_agricultural,
           o.drought_class,
           SUM(o.acres  * w.fraction) AS acres,
           SUM(o.pixels * w.fraction) AS pixels,
           MAX(t.answered_m2)         AS answered_m2,
           MAX(t.map_units)           AS map_units,
           -- `soil` is a single row, so every aggregate over it returns that
           -- row's value; MAX() is only the mechanism for carrying it past
           -- the grouping sets, as it is for the totals above.
           MAX(s.rated_m2)            AS rated_m2,
           MAX(s.cultivable_m2)       AS cultivable_m2,
           MAX(s.irrigable_m2)        AS irrigable_m2,
           MAX(s.slope_pct)           AS slope_pct,
           MAX(s.water_storage)       AS water_storage,
           MAX(s.dominant_soil)       AS dominant_soil,
           MAX(s.dominant_drainage)   AS dominant_drainage
    FROM weighted w
    JOIN overlay o ON o.mukey = w.mukey
    CROSS JOIN totals t
    CROSS JOIN soil s
    GROUP BY GROUPING SETS (
        (o.land_cover, o.crop_code, o.is_agricultural),
        (o.drought_class)
    )
    ORDER BY acres DESC
    """
)


# The geometry behind POST /area/mapunits.
#
# Deliberately a separate query and a separate endpoint rather than another
# column on AREA_BREAKDOWN. The two share the `hits` CTE, so this does repeat
# the spatial work -- but folding geometry into the /area response would
# inflate the payload that §11's cache benchmark measures, and every latency
# figure already published for that endpoint would silently stop being
# comparable. Geometry is also the part a client may not want: the numbers
# render immediately, the map fills in behind them.
#
# What comes back is one feature per map unit, clipped to the drawn field, in
# EPSG:4326 because that is what a web map consumes. The `dominant` CTE picks
# each unit's largest land cover class by acreage, so the frontend can colour
# a polygon without being handed every overlay row behind it.
AREA_MAPUNITS = text(
    """
    WITH q AS (
        SELECT ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), 5070) AS geom
    ),
    hits AS (
        -- ST_Collect, not ST_Union: polygons of the same map unit do not
        -- overlap each other, so there is nothing to dissolve and the cheaper
        -- of the two is correct. CollectionExtract(_, 3) drops the degenerate
        -- points and lines that ST_Intersection emits where a field boundary
        -- merely grazes a polygon edge, keeping the result a clean MultiPolygon.
        SELECT sp.mukey,
               ST_CollectionExtract(
                   ST_Collect(ST_Intersection(sp.geom, q.geom)), 3
               ) AS clipped,
               SUM(ST_Area(ST_Intersection(sp.geom, q.geom))) AS inter_m2
        FROM soil_polygon sp
        JOIN q ON sp.geom && q.geom AND ST_Intersects(sp.geom, q.geom)
        GROUP BY sp.mukey
    ),
    dominant AS (
        SELECT DISTINCT ON (o.mukey)
               o.mukey, o.musym, o.areasymbol,
               o.land_cover, o.crop_code, o.is_agricultural, o.drought_class
        FROM overlay o
        JOIN hits h ON h.mukey = o.mukey
        ORDER BY o.mukey, o.acres DESC
    )
    -- An inner join, so map units with no overlay rows fall out here exactly
    -- as they fall out of `totals` above. A polygon the breakdown refuses to
    -- count must not be drawn on the map as though it had been counted.
    SELECT h.mukey,
           d.musym,
           d.areasymbol,
           a.muname       AS soil_name,
           COALESCE(a.cap_irrigated, a.cap_rainfed) AS capability_class,
           d.land_cover,
           d.crop_code,
           d.is_agricultural,
           d.drought_class,
           h.inter_m2,
           ST_AsGeoJSON(ST_Transform(h.clipped, 4326), 6) AS geojson
    FROM hits h
    JOIN dominant d ON d.mukey = h.mukey
    -- LEFT, not inner: a map unit with no attribute row (open water, rock
    -- outcrop, dams) still has geometry and still belongs on the map.
    LEFT JOIN mapunit_attr a ON a.mukey = h.mukey
    WHERE h.inter_m2 > 0
    ORDER BY h.inter_m2 DESC
    LIMIT :limit
    """
)

# Square meters to acres. The batch pipeline's acres come from the same
# constant applied to 30 m CDL pixels, so the two halves agree by construction.
M2_PER_ACRE = 4046.8564224
