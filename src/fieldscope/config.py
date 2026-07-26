"""Shared paths and area-of-interest definitions.

The pipeline is built and validated against a single county, then re-run
unchanged against the whole state to produce benchmark numbers. Both are
defined here so scaling up is a config change, not a code change.
"""

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
RAW = DATA / "raw"
INTERIM = DATA / "interim"
PROCESSED = DATA / "processed"

for _d in (RAW, INTERIM, PROCESSED):
    _d.mkdir(parents=True, exist_ok=True)

# Every layer is normalized to this before any join. The SSURGO WFS returns
# geometry with no CRS attached, so this gets assigned explicitly rather than
# inferred — an unlabeled projection is the classic way to get a spatial join
# that runs clean and returns nonsense.
WGS84 = "EPSG:4326"

# Equal-area projection for anything involving area or distance. Lat/lon
# degrees are not a unit of area, so overlap acreage is computed here.
EQUAL_AREA = "EPSG:5070"  # NAD83 / Conus Albers


@dataclass(frozen=True)
class AOI:
    """An area to run the pipeline over."""

    name: str
    state_fips: str
    county_fips: str | None  # None => whole state
    bbox: tuple[float, float, float, float]  # minx, miny, maxx, maxy in WGS84

    @property
    def slug(self) -> str:
        return self.name.lower().replace(" ", "_")


TIPPECANOE = AOI(
    name="Tippecanoe",
    state_fips="18",
    county_fips="157",
    bbox=(-87.10, 40.20, -86.65, 40.57),
)

INDIANA = AOI(
    name="Indiana",
    state_fips="18",
    county_fips=None,
    bbox=(-88.10, 37.75, -84.75, 41.77),
)

DEFAULT_AOI = TIPPECANOE

# CDL release year to pull. 2025 is the current national release.
CDL_YEAR = 2025
