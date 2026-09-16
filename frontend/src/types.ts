/**
 * The serving API's response shapes, mirroring serving/app/models.py.
 *
 * Hand-written rather than generated: there are four of them, they change when
 * the API changes, and a generator plus its config is more machinery than this
 * earns. If they drift, the typecheck will not catch it -- the API's OpenAPI
 * schema at /docs is the thing to check against.
 */

export interface LandCoverSlice {
  land_cover: string
  crop_code: number
  is_agricultural: boolean
  acres: number
  pixels: number
  /** Fraction of answered acres, 0-1. */
  share: number
}

export interface DroughtSlice {
  /** USDM severity; -1 means no drought. */
  drought_class: number
  label: string
  acres: number
  share: number
}

export interface SoilSummary {
  dominant_name: string | null
  dominant_drainage: string | null
  /** Area-weighted slope, percent. */
  slope_pct: number | null
  /** Area-weighted available water storage to 150 cm, in cm. */
  water_storage: number | null
  /** Share of *rated* area in USDA capability class 1-4. */
  cultivable_share: number | null
  irrigable_share: number | null
  /** Share of answered area carrying any capability rating. */
  rated_share: number
}

export type FindingKind =
  | 'capability'
  | 'use'
  | 'irrigation'
  | 'drought'
  | 'terrain'
  | 'water'
  | 'caveat'

export interface Finding {
  kind: FindingKind
  severity: 'neutral' | 'note' | 'caution'
  text: string
}

/**
 * The server's reading of the field. Rules, not a model -- every sentence is
 * derived from a number by a threshold in serving/app/insight.py, so the UI
 * can present it as a finding rather than as a suggestion.
 */
export interface Insight {
  headline: string
  findings: Finding[]
  cultivable_share: number | null
  rated_share: number | null
  agricultural_share: number | null
}

export interface AreaResponse {
  query_acres: number
  answered_acres: number
  /** answered / query. Below 1 where the polygon leaves the soil survey. */
  coverage: number
  map_units: number
  breakdown: LandCoverSlice[]
  drought: DroughtSlice[]
  soil: SoilSummary | null
  insight: Insight | null
  cached: boolean
  method: string
}

export interface MapUnitProperties {
  mukey: string
  musym: string | null
  areasymbol: string | null
  /** Null for map units with no attribute row: open water, rock, made land. */
  soil_name: string | null
  capability_class: number | null
  /** The unit's single largest land cover class, for colouring. */
  land_cover: string
  crop_code: number
  is_agricultural: boolean
  drought_class: number
  drought_label: string
  /** Acres of this map unit inside the drawn field. */
  acres: number
}

export interface MapUnitFeature {
  type: 'Feature'
  geometry: GeoJSON.MultiPolygon | GeoJSON.Polygon
  properties: MapUnitProperties
}

export interface MapUnitGeometry {
  type: 'FeatureCollection'
  features: MapUnitFeature[]
  /** True when the map unit cap was hit and the smallest slivers were dropped. */
  truncated: boolean
  cached: boolean
}

export type DrawnPolygon = GeoJSON.Polygon | GeoJSON.MultiPolygon
