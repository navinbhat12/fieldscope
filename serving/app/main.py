"""Fieldscope serving API (docs/DESIGN.md §5.6).

Two endpoints over a precomputed overlay. Neither recomputes the join; the
expensive work happened offline on Spark, and the only geometry left in the
request path is deciding *which* precomputed rows a drawn polygon needs.
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from sqlalchemy import text

from .config import load_settings
from .db import make_engine
from .models import (
    AreaRequest,
    AreaResponse,
    LandCoverSlice,
    MapUnitResponse,
    MapUnitRow,
)
from .queries import AREA_BREAKDOWN, M2_PER_ACRE, MAPUNIT, QUERY_AREA

log = logging.getLogger("fieldscope")

settings = load_settings()
engine = make_engine(settings)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail loudly at startup rather than on the first request: a container that
    # cannot reach its database should not report itself healthy.
    with engine.connect() as conn:
        version = conn.execute(text("SELECT postgis_version()")).scalar()
    log.info("connected to PostGIS %s", version)
    yield
    engine.dispose()


app = FastAPI(
    title="Fieldscope",
    description=(
        "Soil, crop cover and drought for any field boundary, served from a "
        "precomputed overlay rather than computed per request."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM overlay")).scalar()
    return {"status": "ok", "overlay_rows": rows}


@app.get("/mapunit/{mukey}", response_model=MapUnitResponse)
def mapunit(mukey: str) -> MapUnitResponse:
    """Everything known about one soil map unit."""
    with engine.connect() as conn:
        rows = conn.execute(MAPUNIT, {"mukey": mukey}).mappings().all()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No map unit {mukey}")

    return MapUnitResponse(
        mukey=mukey,
        musym=rows[0]["musym"],
        areasymbol=rows[0]["areasymbol"],
        total_acres=round(sum(r["acres"] for r in rows), 3),
        rows=[
            MapUnitRow(
                crop_code=r["crop_code"],
                land_cover=r["land_cover"],
                is_agricultural=r["is_agricultural"],
                drought_class=r["drought_class"],
                pixels=r["pixels"],
                acres=round(r["acres"], 3),
            )
            for r in rows
        ],
    )


@app.post("/area", response_model=AreaResponse)
def area(req: AreaRequest) -> AreaResponse:
    """What is under a drawn field boundary.

    The expensive endpoint, and the reason the cache in §5.7 exists: the query
    space is unbounded because a caller can draw anything.
    """
    geojson = json.dumps(req.geometry.model_dump())

    with engine.connect() as conn:
        # Pre-flight: measure the polygon before touching 1.48M rows with it.
        try:
            pre = conn.execute(QUERY_AREA, {"geojson": geojson}).mappings().one()
        except Exception as exc:  # unparseable GeoJSON reaches PostGIS as an error
            raise HTTPException(status_code=422, detail=f"Bad geometry: {exc}") from exc

        # An empty or self-intersecting polygon would otherwise sail through and
        # return a zero-coverage result identical to one drawn over open water.
        if pre["is_empty"]:
            raise HTTPException(
                status_code=422,
                detail="Geometry is empty or degenerate — it encloses no area.",
            )
        if not pre["is_valid"]:
            raise HTTPException(
                status_code=422,
                detail="Geometry is invalid (self-intersecting or malformed).",
            )

        query_acres = (pre["query_m2"] or 0) / M2_PER_ACRE
        if query_acres > settings.max_query_acres:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"Polygon covers {query_acres:,.0f} acres; the limit is "
                    f"{settings.max_query_acres:,.0f}. Draw a smaller area."
                ),
            )

        rows = conn.execute(AREA_BREAKDOWN, {"geojson": geojson}).mappings().all()

    # A polygon over open water or outside Indiana intersects nothing. That is
    # a real answer, not an error -- report it as empty with zero coverage.
    if not rows:
        return AreaResponse(
            query_acres=round(query_acres, 3),
            answered_acres=0.0,
            coverage=0.0,
            map_units=0,
            breakdown=[],
        )

    answered_acres = rows[0]["answered_m2"] / M2_PER_ACRE
    total = sum(r["acres"] for r in rows) or 1.0

    return AreaResponse(
        query_acres=round(query_acres, 3),
        answered_acres=round(answered_acres, 3),
        coverage=round(answered_acres / query_acres, 4) if query_acres else 0.0,
        map_units=rows[0]["map_units"],
        breakdown=[
            LandCoverSlice(
                land_cover=r["land_cover"],
                crop_code=r["crop_code"],
                is_agricultural=r["is_agricultural"],
                acres=round(r["acres"], 3),
                pixels=round(r["pixels"], 1),
                share=round(r["acres"] / total, 4),
            )
            for r in rows
        ],
    )
