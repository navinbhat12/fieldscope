"""Request and response shapes.

The response deliberately reports coverage and map-unit counts alongside the
breakdown itself. A caller that cannot see how much of its polygon was
answerable, or how many map units the answer was assembled from, has no way to
judge the number it is being handed.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Geometry(BaseModel):
    """A GeoJSON geometry in EPSG:4326, as a web map produces it."""

    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list

    @field_validator("coordinates")
    @classmethod
    def rings_must_be_closeable(cls, v: list) -> list:
        """Reject rings that cannot describe an area.

        A GeoJSON linear ring needs at least four positions, the last repeating
        the first. PostGIS will silently accept a shorter one and hand back an
        empty geometry, so catching it here turns a confusing empty result into
        an explicit 422.
        """

        def check(ring: list) -> None:
            if len(ring) < 4:
                raise ValueError(
                    f"a polygon ring needs at least 4 positions, got {len(ring)}"
                )
            if list(ring[0]) != list(ring[-1]):
                raise ValueError("a polygon ring must close: last position must equal first")

        if not v:
            raise ValueError("coordinates must not be empty")
        # Polygon is a list of rings; MultiPolygon a list of those.
        first = v[0][0]
        if isinstance(first, (int, float)):
            check(v)          # a bare ring
        elif isinstance(first[0], (int, float)):
            for ring in v:    # Polygon
                check(ring)
        else:
            for poly in v:    # MultiPolygon
                for ring in poly:
                    check(ring)
        return v


class AreaRequest(BaseModel):
    geometry: Geometry


# USDM severity, as the batch join stored it: the shapefile's DM attribute cast
# to an integer, with -1 filled in for ground the drought layer does not cover
# (scripts/run_join.py). -1 is therefore "not in drought", not "unknown".
DROUGHT_LABELS: dict[int, str] = {
    -1: "No drought",
    0: "D0 — Abnormally dry",
    1: "D1 — Moderate drought",
    2: "D2 — Severe drought",
    3: "D3 — Extreme drought",
    4: "D4 — Exceptional drought",
}


def drought_label(code: int) -> str:
    return DROUGHT_LABELS.get(code, f"Unknown ({code})")


class DroughtSlice(BaseModel):
    drought_class: int = Field(description="USDM severity; -1 means no drought.")
    label: str
    acres: float
    share: float = Field(description="Fraction of answered acres, 0-1.")


class LandCoverSlice(BaseModel):
    land_cover: str
    crop_code: int
    is_agricultural: bool
    acres: float
    pixels: float
    share: float = Field(description="Fraction of answered acres, 0-1.")


class MapUnitRow(BaseModel):
    crop_code: int
    land_cover: str
    is_agricultural: bool
    drought_class: int
    pixels: int
    acres: float


class MapUnitResponse(BaseModel):
    mukey: str
    musym: str | None
    areasymbol: str | None
    total_acres: float
    rows: list[MapUnitRow]


class AreaResponse(BaseModel):
    query_acres: float = Field(description="Area of the drawn polygon.")
    answered_acres: float = Field(
        description="Area of the drawn polygon that fell on mapped soil."
    )
    coverage: float = Field(
        description="answered_acres / query_acres. Below 1 where the polygon "
        "extends past the soil survey -- open water, or outside the AOI."
    )
    map_units: int
    breakdown: list[LandCoverSlice]

    # Aggregated separately from `breakdown` rather than as a field on it: the
    # two are independent views of the same acres, and crossing them would
    # multiply an already long list (§ the GROUPING SETS note in queries.py).
    # Both sum to answered_acres.
    drought: list[DroughtSlice] = []

    cached: bool = False

    # Stated in the response rather than only in the docs, because the number
    # above is an estimate and a caller should not have to read a design
    # document to find that out. See docs/DESIGN.md §5.6.
    method: str = (
        "Area-weighted from per-map-unit totals. Land cover is assumed "
        "uniformly distributed within each soil map unit."
    )


# ---------------------------------------------------------------------------
# POST /area/mapunits
#
# Plain GeoJSON rather than a bespoke shape, because the consumer is a web map
# and every mapping library reads a FeatureCollection directly. Modelled
# explicitly instead of returned as a bare dict so the OpenAPI schema describes
# what is in `properties` -- a caller should not have to send a request to find
# out what it can colour a polygon by.


class MapUnitProperties(BaseModel):
    mukey: str
    musym: str | None
    areasymbol: str | None

    # The map unit's single largest land cover class by acreage, for colouring.
    # A unit typically contains many; the full breakdown is what /area returns,
    # and GET /mapunit/{mukey} has the per-unit detail.
    land_cover: str
    crop_code: int
    is_agricultural: bool

    drought_class: int
    drought_label: str

    acres: float = Field(description="Acres of this map unit inside the drawn field.")


class MapUnitFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    geometry: dict
    properties: MapUnitProperties


class MapUnitGeometry(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[MapUnitFeature]

    # True when the map unit cap was hit and the smallest slivers were dropped.
    # Stated rather than silently applied: a map missing pieces of its own
    # answer should say so.
    truncated: bool = False
    cached: bool = False
