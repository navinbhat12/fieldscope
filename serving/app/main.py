"""Fieldscope serving API (docs/DESIGN.md §5.6).

Endpoints over a precomputed overlay. None of them recomputes the join; the
expensive work happened offline on Spark, and the only geometry left in the
request path is deciding *which* precomputed rows a drawn polygon needs, and
clipping those map units to the polygon so a map can draw them.
"""

import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from .cache import Cache, key_for, normalise
from .config import load_settings
from .db import make_engine
from .models import (
    AreaRequest,
    AreaResponse,
    DroughtSlice,
    LandCoverSlice,
    MapUnitFeature,
    MapUnitGeometry,
    MapUnitProperties,
    MapUnitResponse,
    MapUnitRow,
    drought_label,
)
from .queries import AREA_BREAKDOWN, AREA_MAPUNITS, M2_PER_ACRE, MAPUNIT, QUERY_AREA

log = logging.getLogger("fieldscope")

settings = load_settings()
engine = make_engine(settings)
cache = Cache(settings.redis_url, enabled=settings.cache_enabled)


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

# The frontend is served from a different origin than this API -- a static host
# for the bundle, a tunnel for the API -- and a browser will not make that
# request unless the server opts in. Wide open by default because every
# endpoint here is read-only, unauthenticated and public: there is no session
# for another origin to ride, so the usual reason to narrow this does not
# apply. `allow_credentials` stays False, which is also what a "*" origin
# requires. Narrow via CORS_ORIGINS if any of that changes.
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def _preflight(conn, geojson: str) -> float:
    """Measure and validate a drawn polygon before doing anything expensive.

    Shared by both polygon endpoints so they cannot disagree about what counts
    as a valid request -- a geometry /area rejects must not be one that
    /area/mapunits happily draws. Returns the polygon's area in acres.
    """
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
    return query_acres


@app.get("/health")
def health() -> dict:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT count(*) FROM overlay")).scalar()
    return {"status": "ok", "overlay_rows": rows, "cache": cache.stats()}


@app.post("/ping")
def ping(req: AreaRequest) -> dict:
    """Benchmark control. Does no work; exists to be subtracted.

    Takes the same request body as /area and returns immediately, so a
    benchmark run against it measures everything /area pays that is not the
    query: HTTP, the ASGI server, Pydantic validation of the polygon, JSON
    serialisation, and -- on a development Mac -- Docker's port forwarding.
    Without this floor, any claim about what caching saves is unfounded,
    because the measured difference could be dominated by transport that no
    amount of caching can remove.
    """
    return {"ok": True, "vertices": len(req.geometry.coordinates[0])}


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
    # Normalise first, then use the *normalised* polygon for both the cache key
    # and the query, so one key cannot map to several different answers.
    geometry = normalise(req.geometry.model_dump(), settings.coord_precision)
    geojson = json.dumps(geometry)

    cache_key = key_for(geometry)
    hit = cache.get(cache_key)
    if hit is not None:
        return AreaResponse(**{**hit, "cached": True})

    with engine.connect() as conn:
        # Measure the polygon before touching 484,325 soil polygons with it.
        query_acres = _preflight(conn, geojson)
        rows = conn.execute(AREA_BREAKDOWN, {"geojson": geojson}).mappings().all()

    # A polygon over open water or outside the AOI intersects nothing. That is
    # a real answer, not an error -- report it as empty with zero coverage.
    if not rows:
        empty = AreaResponse(
            query_acres=round(query_acres, 3),
            answered_acres=0.0,
            coverage=0.0,
            map_units=0,
            breakdown=[],
        )
        # Cached deliberately: discovering that a polygon covers open water
        # costs the same spatial lookup as discovering that it covers corn.
        cache.set(cache_key, empty.model_dump())
        return empty

    # The query returns two interleaved aggregations of the same acres (see the
    # GROUPING SETS note in queries.py). Split them before summing anything --
    # a total taken over the raw rows would count every acre twice.
    cover_rows = [r for r in rows if r["row_kind"] == "cover"]
    drought_rows = [r for r in rows if r["row_kind"] == "drought"]

    # Carried on every row by MAX(), so either grouping set can supply them.
    answered_acres = rows[0]["answered_m2"] / M2_PER_ACRE
    total = sum(r["acres"] for r in cover_rows) or 1.0

    response = AreaResponse(
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
            for r in cover_rows
        ],
        # Same denominator as the land cover shares: both grouping sets sum the
        # same weighted acres, so the two breakdowns are directly comparable.
        drought=[
            DroughtSlice(
                drought_class=r["drought_class"],
                label=drought_label(r["drought_class"]),
                acres=round(r["acres"], 3),
                share=round(r["acres"] / total, 4),
            )
            for r in sorted(drought_rows, key=lambda r: r["drought_class"])
        ],
    )
    cache.set(cache_key, response.model_dump())
    return response


@app.post("/area/mapunits", response_model=MapUnitGeometry)
def area_mapunits(req: AreaRequest) -> MapUnitGeometry:
    """The soil map units under a drawn field, clipped to it, as GeoJSON.

    The companion to POST /area: that endpoint says *what* is under a field,
    this one says *where*. Kept separate so the numbers can render before the
    geometry arrives, and so geometry never enters the payload §11 measures.
    """
    geometry = normalise(req.geometry.model_dump(), settings.coord_precision)
    geojson = json.dumps(geometry)

    # A different namespace from /area: same polygon, different question.
    cache_key = key_for(geometry, prefix="mapunits")
    hit = cache.get(cache_key)
    if hit is not None:
        return MapUnitGeometry(**{**hit, "cached": True})

    with engine.connect() as conn:
        _preflight(conn, geojson)
        # Ask for one past the cap so that hitting it is distinguishable from
        # landing exactly on it, and the response can say which happened.
        rows = (
            conn.execute(
                AREA_MAPUNITS,
                {"geojson": geojson, "limit": settings.max_geometry_features + 1},
            )
            .mappings()
            .all()
        )

    truncated = len(rows) > settings.max_geometry_features
    rows = rows[: settings.max_geometry_features]

    response = MapUnitGeometry(
        features=[
            MapUnitFeature(
                geometry=json.loads(r["geojson"]),
                properties=MapUnitProperties(
                    mukey=r["mukey"],
                    musym=r["musym"],
                    areasymbol=r["areasymbol"],
                    land_cover=r["land_cover"],
                    crop_code=r["crop_code"],
                    is_agricultural=r["is_agricultural"],
                    drought_class=r["drought_class"],
                    drought_label=drought_label(r["drought_class"]),
                    acres=round(r["inter_m2"] / M2_PER_ACRE, 3),
                ),
            )
            for r in rows
        ],
        truncated=truncated,
    )
    # Cached like /area, and for the same reason: an empty result costs the
    # same spatial lookup as a full one.
    cache.set(cache_key, response.model_dump())
    return response
