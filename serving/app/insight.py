"""Turn the measured numbers into a reading of the land.

**Why rules and not a model.** Every sentence here is derived from a number
the query already computed, by a threshold written down in this file. That
makes each claim traceable to its input and reproducible across runs, costs
nothing to serve, and cannot invent a fact about ground it has never seen. A
language model asked "what does 59% shrubland mean" would answer from general
knowledge rather than from this field, which is exactly the failure this
avoids.

**The spine is capability against use.** USDA's land capability class already
encodes what ground can support -- 1-4 cultivable, 5-8 not -- so the
interesting question is not "what is growing here" but whether what is growing
matches what the soil could carry. That comparison is the one reading that
serves a grower, a buyer, a lender and a conservation planner at once.

**What it deliberately does not do.** It does not recommend a crop, price a
parcel, or rate fire risk. Those need data this pipeline does not have --
yield history, comparable sales, fuel models, weather -- and asserting them
from soil class and land cover would be dressing a guess as an answer.
"""

from typing import Literal

from pydantic import BaseModel, Field

# Thresholds, named and gathered so the rules can be audited and argued with
# rather than reverse-engineered out of the prose. These are judgement calls,
# not measurements, and they are the only judgement calls in this module.
CULTIVABLE_HIGH = 0.50   # majority of rated ground is class 1-4
CULTIVABLE_LOW = 0.20
FARMED_HIGH = 0.50       # majority of land cover is agricultural
FARMED_LOW = 0.20
IRRIGABLE_LOW = 0.50
SLOPE_ROLLING = 15.0     # percent
SLOPE_STEEP = 30.0
WATER_LOW = 8.0          # cm of available water storage to 150 cm
COVERAGE_PARTIAL = 0.95
RATED_PARTIAL = 0.90
DROUGHT_SEVERE = 2       # USDM D2


class Finding(BaseModel):
    kind: Literal["capability", "use", "irrigation", "drought", "terrain", "water", "caveat"]
    severity: Literal["neutral", "note", "caution"]
    text: str


class Insight(BaseModel):
    """A reading of one field, with the numbers it rests on."""

    headline: str
    findings: list[Finding] = []
    # Restated so a caller never has to trust the prose over the figures.
    cultivable_share: float | None = Field(
        default=None, description="Share of *rated* area in capability class 1-4."
    )
    rated_share: float | None = Field(
        default=None, description="Share of answered area carrying a capability rating."
    )
    agricultural_share: float | None = None


def _pct(x: float) -> str:
    """One decimal below 10%, none above -- 0.4% and 63% both read naturally."""
    p = x * 100
    return f"{p:.1f}%" if p < 10 else f"{p:.0f}%"


def _soil_clause(name: str | None) -> str:
    """SSURGO map unit names sometimes end in a period and sometimes do not."""
    if not name:
        return ""
    return f" Dominant soil: {name.rstrip('.')}."


def build(
    *,
    coverage: float,
    rated_m2: float,
    cultivable_m2: float,
    irrigable_m2: float,
    answered_m2: float,
    agricultural_share: float,
    slope_pct: float | None,
    water_storage: float | None,
    dominant_soil: str | None,
    dominant_drainage: str | None,
    drought: list[tuple[int, float]],
) -> Insight:
    """`drought` is (class, share-of-answered-acres), class -1 meaning none."""

    findings: list[Finding] = []

    rated_share = rated_m2 / answered_m2 if answered_m2 else 0.0
    cultivable = cultivable_m2 / rated_m2 if rated_m2 else None
    irrigable = irrigable_m2 / rated_m2 if rated_m2 else None

    # Nothing to rate: open water, or entirely outside the survey.
    if not rated_m2:
        return Insight(
            headline="No soil rating for this area.",
            findings=[
                Finding(
                    kind="caveat",
                    severity="note",
                    text=(
                        "None of the answered area carries a USDA capability rating — "
                        "typically open water, rock outcrop or made land."
                    ),
                )
            ],
            rated_share=rated_share,
            agricultural_share=agricultural_share,
        )

    # --- the headline: what the ground can carry, against what it carries ---
    farmed = agricultural_share
    if cultivable >= CULTIVABLE_HIGH and farmed >= FARMED_HIGH:
        headline = "Capable ground, and it is being farmed."
    elif cultivable >= CULTIVABLE_HIGH and farmed < FARMED_LOW:
        headline = "Cultivable ground, largely out of production."
    elif cultivable < CULTIVABLE_LOW and farmed < FARMED_LOW:
        headline = "Marginal ground, and used as such."
    elif cultivable < CULTIVABLE_LOW and farmed >= FARMED_HIGH:
        headline = "Farmed well beyond what the soil alone would carry."
    else:
        headline = "Mixed ground, partly in production."

    findings.append(
        Finding(
            kind="capability",
            severity="neutral",
            text=(
                (
                    "None of the rated area is USDA capability class 1–4 "
                    "(capable of cultivation); all of it is class 5–8."
                    if cultivable == 0
                    else "All of the rated area is USDA capability class 1–4 "
                    "(capable of cultivation)."
                    if cultivable == 1
                    else f"{_pct(cultivable)} of rated area is USDA capability "
                    f"class 1–4 (capable of cultivation); {_pct(1 - cultivable)} "
                    "is class 5–8."
                )
                + _soil_clause(dominant_soil)
            ),
        )
    )
    findings.append(
        Finding(
            kind="use",
            severity="neutral",
            text=f"{_pct(farmed)} of land cover is agricultural.",
        )
    )

    # Irrigability is the hinge in California: capability class 1-4 on paper
    # means little if the ground cannot take water.
    if irrigable is not None and irrigable < IRRIGABLE_LOW:
        findings.append(
            Finding(
                kind="irrigation",
                severity="note",
                text=(
                    "None of the rated area can be irrigated."
                    if irrigable == 0
                    else f"Only {_pct(irrigable)} of rated area carries an irrigated "
                    "capability rating; the rest cannot be irrigated."
                ),
            )
        )

    # --- drought, weighted against whether anything is actually farmed ---
    severe = sum(share for cls, share in drought if cls >= DROUGHT_SEVERE)
    any_drought = sum(share for cls, share in drought if cls >= 0)
    if severe > 0:
        findings.append(
            Finding(
                kind="drought",
                severity="caution",
                text=(
                    f"{_pct(severe)} of the field is in severe drought (D2) or worse"
                    + (
                        f", on ground that is {_pct(farmed)} agricultural."
                        if farmed >= FARMED_LOW
                        else "."
                    )
                ),
            )
        )
    elif any_drought > 0:
        findings.append(
            Finding(
                kind="drought",
                severity="note",
                text=f"{_pct(any_drought)} of the field is under some drought classification (D0–D1).",
            )
        )

    # --- terrain and water ---
    if slope_pct is not None:
        if slope_pct >= SLOPE_STEEP:
            findings.append(
                Finding(
                    kind="terrain",
                    severity="caution",
                    text=(
                        f"Mean slope {slope_pct:.0f}% — steep. Limits machinery and "
                        "raises erosion risk."
                    ),
                )
            )
        elif slope_pct >= SLOPE_ROLLING:
            findings.append(
                Finding(
                    kind="terrain",
                    severity="note",
                    text=f"Mean slope {slope_pct:.0f}% — rolling.",
                )
            )

    if water_storage is not None and water_storage < WATER_LOW:
        findings.append(
            Finding(
                kind="water",
                severity="note",
                text=(
                    f"Available water storage {water_storage:.1f} cm to 150 cm — low. "
                    "Crops here depend on irrigation timing rather than stored moisture."
                    + (f" Drainage: {dominant_drainage.lower()}." if dominant_drainage else "")
                ),
            )
        )

    # --- caveats last, so they qualify rather than lead ---
    if coverage < COVERAGE_PARTIAL:
        findings.append(
            Finding(
                kind="caveat",
                severity="note",
                text=(
                    f"Only {_pct(coverage)} of the drawn polygon fell on mapped soil; "
                    "the rest is outside the survey."
                ),
            )
        )
    if rated_share < RATED_PARTIAL:
        findings.append(
            Finding(
                kind="caveat",
                severity="note",
                text=(
                    f"{_pct(1 - rated_share)} of the answered area has no capability "
                    "rating — open water, rock outcrop or made land."
                ),
            )
        )

    return Insight(
        headline=headline,
        findings=findings,
        cultivable_share=cultivable,
        rated_share=rated_share,
        agricultural_share=agricultural_share,
    )
