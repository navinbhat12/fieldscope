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
        "extends past the soil survey -- open water, or outside Indiana."
    )
    map_units: int
    breakdown: list[LandCoverSlice]
    cached: bool = False

    # Stated in the response rather than only in the docs, because the number
    # above is an estimate and a caller should not have to read a design
    # document to find that out. See docs/DESIGN.md §5.6.
    method: str = (
        "Area-weighted from per-map-unit totals. Land cover is assumed "
        "uniformly distributed within each soil map unit."
    )
