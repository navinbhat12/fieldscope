/**
 * Deployment and area-of-interest configuration.
 *
 * Nothing here is a California constant buried in a component. The pipeline
 * can be pointed at another state (docs/DESIGN.md §12.4) and the map should
 * follow it without a rewrite -- so the AOI list is data, the initial view is
 * read from it, and the state switcher only appears when there is more than
 * one to switch between.
 */

export interface Aoi {
  id: string
  name: string
  /**
   * Where the map opens.
   *
   * Deliberately NOT the whole state. At a statewide zoom every shape a person
   * traces covers millions of acres, and the API rejects anything over 100,000
   * -- so opening on the AOI makes the first interaction fail by construction.
   * This opens over Central Valley farmland at a scale where a traced shape is
   * a field, which is the question the tool answers.
   */
  start: { center: [number, number]; zoom: number }
  /** The whole AOI, for the "see the state" action. */
  overview: { center: [number, number]; zoom: number }
  /**
   * Which USDM week the drought layer is a snapshot of. It is a labelled
   * snapshot, not a live feed (§5.4, descoped), and the UI has to say so
   * rather than imply currency it does not have.
   */
  droughtWeek: string
}

export const AOIS: Aoi[] = [
  {
    id: 'california',
    name: 'California',
    start: { center: [-119.62, 36.68], zoom: 12.4 },
    overview: { center: [-119.6, 36.9], zoom: 5.4 },
    droughtWeek: 'USDM week of 2026-09-08',
  },
]

export const DEFAULT_AOI = AOIS[0]

/**
 * The serving API's base URL.
 *
 * An environment variable because the deployed API sits behind a Cloudflare
 * quick tunnel whose hostname changes on every restart (§13). Trailing slashes
 * are stripped so callers can join paths without thinking about it.
 */
export const API_BASE: string = (
  import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'
).replace(/\/+$/, '')

/**
 * CARTO's Dark Matter, served from their CDN with no API key.
 *
 * Chosen over OpenFreeMap after measuring both. OpenFreeMap's dark style has a
 * broken sprite -- it references an icon named `circle-11` while its sprite
 * sheet only contains `circle_11` -- so MapLibre raises an error on every
 * render, and its tile endpoint returned intermittent 403s under plain
 * repeated requests. CARTO's sprite is internally consistent, its tiles
 * answered 8/8 in ~0.13s, and Dark Matter is the better-looking map: 93
 * styled layers against 47, in the muted palette this data needs to read on
 * top of.
 *
 * Free to 5M tile requests a month with no account, which a demo will not
 * approach.
 */
export const BASEMAP_STYLE =
  'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json'
