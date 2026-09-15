"""The SQL behind the two endpoints.

Kept as literal SQL rather than built through the ORM. These are the whole
product -- a spatial intersection and a weighted aggregation -- and they want
to be read, reviewed and EXPLAINed directly, not assembled at runtime out of
Python objects.
"""

from sqlalchemy import text

# One map unit's breakdown. A plain indexed lookup on 155,025 rows; this is the
# endpoint that needs no cache (§5.7).
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
    -- The soil table holds 10,013 map units but the Indiana overlay covers
    -- 7,534: border WFS tiles brought down soil from Illinois, Ohio, Kentucky
    -- and Michigan (§5.10), which no Indiana land cover record can describe.
    -- Counting those in the total would report coverage 1.0 for a field drawn
    -- on the state line while quietly omitting half of it from the breakdown.
    totals AS (
        SELECT COALESCE(SUM(w.inter_m2), 0) AS answered_m2,
               COUNT(*)                     AS map_units
        FROM weighted w
        WHERE EXISTS (SELECT 1 FROM overlay o WHERE o.mukey = w.mukey)
    )
    SELECT o.land_cover,
           o.crop_code,
           o.is_agricultural,
           SUM(o.acres  * w.fraction) AS acres,
           SUM(o.pixels * w.fraction) AS pixels,
           t.answered_m2,
           t.map_units
    FROM weighted w
    JOIN overlay o ON o.mukey = w.mukey
    CROSS JOIN totals t
    GROUP BY o.land_cover, o.crop_code, o.is_agricultural, t.answered_m2, t.map_units
    ORDER BY acres DESC
    """
)

# Square meters to acres. The batch pipeline's acres come from the same
# constant applied to 30 m CDL pixels, so the two halves agree by construction.
M2_PER_ACRE = 4046.8564224
