/**
 * Land cover groups, and the colours that encode them.
 *
 * A field in the Central Valley comes back with as many as 74 land cover
 * categories, of which the top ten account for ~91% of the acreage and the
 * remaining sixty-odd split the last 9%. Seventy-four colours is not a
 * palette, it is confetti -- and past about seven classes adjacent hues stop
 * being tellable apart at all.
 *
 * So colour encodes the **group**, not the individual crop, and not the crop's
 * rank within this particular field. Rank would mean every new field repaints
 * the legend: almonds blue here, orange there, which destroys the one thing a
 * colour is for. Groups are stable across every query, there are eight of
 * them, and they carry real meaning -- "this field is orchards and vineyards"
 * is the sentence a reader actually wants.
 *
 * Individual crops keep their identity in the ranked list and the table, where
 * names do the work that colour cannot.
 */

export interface LandCoverGroup {
  id: string
  label: string
  /** Fill colour. Slot order is fixed; see the palette note below. */
  color: string
}

/**
 * Slots 1-7 of the validated dark-mode categorical palette, in their documented
 * order, plus a neutral for the tail.
 *
 * The order is the colourblind-safety mechanism, not a preference: these seven
 * were checked against this panel's actual surface (#191817) rather than
 * eyeballed, and the groups are always stacked in this same order so that the
 * pairs which end up touching are the pairs that were validated as touching.
 *
 *   lightness band   all 7 inside L 0.48-0.67       PASS
 *   chroma floor     all 7 >= 0.1                   PASS
 *   CVD separation   worst adjacent dE 8.4 (protan) PASS
 *   normal vision    worst adjacent dE 19.3         PASS
 *   contrast         all 7 >= 3:1 on #191817        PASS
 *
 * "Other" is deliberately not a series colour. It is a bucket, not a category,
 * and it should recede rather than compete with the seven that mean something.
 */
export const GROUPS: LandCoverGroup[] = [
  { id: 'orchard', label: 'Orchards & vineyards', color: '#3987e5' },
  { id: 'grain', label: 'Grains & oilseeds', color: '#d95926' },
  { id: 'vegetable', label: 'Vegetables & melons', color: '#199e70' },
  { id: 'forage', label: 'Forage & pasture', color: '#c98500' },
  { id: 'developed', label: 'Developed', color: '#d55181' },
  { id: 'forest', label: 'Forest & shrub', color: '#008300' },
  { id: 'water', label: 'Water & wetland', color: '#9085e9' },
  { id: 'other', label: 'Other & fallow', color: '#6b675f' },
]

export const GROUP_BY_ID = Object.fromEntries(GROUPS.map((g) => [g.id, g])) as Record<
  string,
  LandCoverGroup
>

/**
 * CDL code -> group.
 *
 * Written out as explicit code sets rather than ranges, because the CDL legend
 * is not contiguous by meaning: 55 is caneberries sitting in the middle of the
 * field crops, 92 is aquaculture among the developed codes, and the 225-254
 * block is double crops that belong with whatever they double. Ranges would be
 * shorter and wrong in a way nobody would notice.
 */
const CODES: Record<string, number[]> = {
  // Tree fruit, nuts and vines -- California's signature, and the reason the
  // AOI moved here from Indiana.
  orchard: [
    55, 66, 67, 68, 69, 70, 71, 72, 74, 75, 76, 77, 204, 210, 211, 212, 215, 217, 218,
    220, 221, 223, 242, 250,
  ],
  // Row and field crops grown for grain, seed or fibre, including the double
  // crops, which are overwhelmingly grain-on-grain.
  grain: [
    1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33,
    34, 35, 38, 39, 45, 51, 52, 53, 60, 205, 224, 225, 226, 228, 230, 232, 233, 234, 235,
    236, 237, 238, 239, 240, 241, 254,
  ],
  vegetable: [
    14, 41, 42, 43, 46, 47, 48, 49, 50, 54, 56, 57, 206, 207, 208, 209, 213, 214, 216,
    219, 222, 227, 229, 231, 243, 244, 245, 246, 247, 248, 249,
  ],
  forage: [36, 37, 58, 59, 176],
  developed: [82, 121, 122, 123, 124],
  forest: [63, 64, 141, 142, 143, 152],
  water: [83, 87, 92, 111, 112, 190, 195],
  // Background, clouds, barren, fallow and the CDL's own catch-alls. Genuinely
  // "we do not know" or "nothing is growing", which is not a category.
  other: [0, 44, 61, 65, 81, 88, 131],
}

const GROUP_OF_CODE = new Map<number, string>()
for (const [groupId, codes] of Object.entries(CODES)) {
  for (const code of codes) GROUP_OF_CODE.set(code, groupId)
}

/** Anything the legend does not cover falls to "other" rather than inventing a hue. */
export function groupForCode(code: number): LandCoverGroup {
  return GROUP_BY_ID[GROUP_OF_CODE.get(code) ?? 'other']
}

export function colorForCode(code: number): string {
  return groupForCode(code).color
}

/**
 * USDM drought severity.
 *
 * An **ordinal** ramp, not a categorical palette: the classes have an order,
 * and reordering them would change what they mean, so the reader should see
 * that order in the colour. One hue, monotone lightness, validated as a ramp
 * (monotone L, adjacent dL >= 0.06, darkest step 2.62:1 on the surface, hue
 * spread 10 degrees).
 *
 * Amber rather than the default blue ramp for the obvious reason -- blue reads
 * as water on a map about drought -- and because blue is already carrying
 * orchard identity two charts up.
 *
 * "No drought" is grey on purpose: it is the absence of the thing being
 * measured, so it should not sit on the severity ramp at all.
 */
export const DROUGHT_STEPS: Record<number, { label: string; short: string; color: string }> = {
  [-1]: { label: 'No drought', short: 'None', color: '#4a4741' },
  0: { label: 'D0 — Abnormally dry', short: 'D0', color: '#7a5410' },
  1: { label: 'D1 — Moderate drought', short: 'D1', color: '#a2731a' },
  2: { label: 'D2 — Severe drought', short: 'D2', color: '#c99429' },
  3: { label: 'D3 — Extreme drought', short: 'D3', color: '#e2b34b' },
  4: { label: 'D4 — Exceptional drought', short: 'D4', color: '#f3cf7d' },
}

export function droughtStep(cls: number) {
  return DROUGHT_STEPS[cls] ?? { label: `Class ${cls}`, short: String(cls), color: '#6b675f' }
}
