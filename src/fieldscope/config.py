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

# Candidate states for the demo AOI, added 2026-09-15. Indiana proved the
# pipeline at state scale but is agriculturally monotonous -- corn and soy on
# uniform glacial soils, and no drought in any week (§5.9), so the drought
# layer renders empty and the crop layer shows two colours. These two are
# picked for range rather than for size.

# The widest crop range in the country and its most severe drought record, so
# it is the one state that exercises all three datasets at once. Also the
# largest: the bounding box is over seven times Indiana's, though roughly half
# of it is ocean and Nevada and is skipped by the outline test.
CALIFORNIA = AOI(
    name="California",
    state_fips="06",
    county_fips=None,
    bbox=(-124.48, 32.53, -114.13, 42.01),
)

# The fallback if California proves too heavy to join on a laptop. Tree fruit,
# hops, wine grapes, wheat and potatoes give genuine range, the dry east side
# carries real drought, and the bounding box is a little over half
# California's.
WASHINGTON = AOI(
    name="Washington",
    state_fips="53",
    county_fips=None,
    bbox=(-124.85, 45.54, -116.92, 49.00),
)

DEFAULT_AOI = TIPPECANOE

# Every defined area, by slug. Scripts expose this as --aoi so a state-scale
# run can be driven without editing DEFAULT_AOI, which would move every stage
# of the pipeline at once -- unhelpful while county and state scopes are being
# compared against each other.
AOIS = {a.slug: a for a in (TIPPECANOE, INDIANA, CALIFORNIA, WASHINGTON)}


def resolve_aoi(slug: str | None) -> AOI:
    """The AOI named by slug, or DEFAULT_AOI when none is given."""
    return AOIS[slug] if slug else DEFAULT_AOI

# CDL release year to pull. 2025 is the current national release.
CDL_YEAR = 2025
