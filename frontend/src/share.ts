/**
 * Putting a drawn field in the URL, so a field is a link.
 *
 * The whole point of the tool is the answer under one particular shape, and
 * until now that shape lived only in the browser's memory: you could describe
 * what you found but not show anyone. A link that restores the field is the
 * difference between a demo and something a person can send.
 *
 * **Why polyline encoding rather than JSON.** A traced field is 5-20 vertices.
 * Measured on a 24-vertex field: 135 characters encoded, against 803 as
 * URL-escaped GeoJSON -- a link that mail clients wrap and chat clients
 * truncate. The committed four-corner examples come to 24-27 characters. The
 * saving is that successive vertices of one field differ in the fourth decimal
 * place, so storing deltas costs two characters where an absolute coordinate
 * costs eleven.
 *
 * **Precision 6, matching the API.** `Settings.coord_precision` is 6, so the
 * server already rounds to six places before it builds a cache key. Encoding
 * at 1e6 therefore loses nothing the server would have kept, and a shared link
 * hits the same cache entry as the draw that produced it rather than
 * recomputing a field that differs in the eighth decimal.
 *
 * **Latitude first, as the algorithm specifies**, not GeoJSON's lng/lat. One
 * swap at each boundary, and it keeps the byte layout recognisable to anyone
 * who has seen the algorithm before.
 *
 * **A URL-safe alphabet instead of the classic `chr(n + 63)`.** That range,
 * 63-126, contains `?`, `|`, `~`, `{`, `}`, `\` and backtick, every one of
 * which a query string percent-escapes into three characters. Measured on the
 * committed examples, escaping inflated a 22-character field to 41 -- undoing
 * most of what the encoding was chosen for. Mapping the same six-bit chunks
 * onto `A-Za-z0-9-_` costs nothing (a chunk is six bits either way, and all 64
 * are unreserved in a URL) and the string survives a query string untouched.
 * The cost is that it no longer pastes into a stock polyline decoder; the
 * scheme is otherwise unchanged, so `decodeField` is the reference.
 */

import type { DrawnPolygon } from './types'

const PRECISION = 1e6

/**
 * Six bits per character, all unreserved in a URL so nothing is escaped.
 * Order is arbitrary but fixed: changing it silently invalidates every link
 * anyone has already sent.
 */
const ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'
const REVERSE = new Map([...ALPHABET].map((c, i) => [c, i]))

/** The parameter a field travels in. */
const FIELD_PARAM = 'f'

function encodeSigned(value: number, out: string[]): void {
  // Zig-zag: fold the sign into the low bit so that small negative deltas cost
  // as little as small positive ones.
  let v = value < 0 ? ~(value << 1) : value << 1
  // Five data bits per character, with the sixth marking "another follows".
  while (v >= 0x20) {
    out.push(ALPHABET[0x20 | (v & 0x1f)])
    v >>= 5
  }
  out.push(ALPHABET[v])
}

/**
 * Encode a polygon's outer ring.
 *
 * The closing vertex is dropped: a ring is closed by definition, so storing
 * the repeat of the first point spends characters on something the decoder
 * already knows. Holes are dropped too -- nothing in this app draws them, and
 * silently encoding only the outer ring of a shape that had holes would be a
 * link that lies about what was measured.
 */
export function encodeField(geometry: DrawnPolygon): string | null {
  if (geometry.type !== 'Polygon') return null
  const ring = geometry.coordinates[0]
  if (!ring || ring.length < 4) return null

  // Drop the closing vertex only when it is genuinely a repeat of the first.
  const last = ring.length - 1
  const closed =
    ring[0][0] === ring[last][0] && ring[0][1] === ring[last][1]
  const points = closed ? ring.slice(0, last) : ring

  const out: string[] = []
  let prevLat = 0
  let prevLng = 0
  for (const [lng, lat] of points) {
    const iLat = Math.round(lat * PRECISION)
    const iLng = Math.round(lng * PRECISION)
    encodeSigned(iLat - prevLat, out)
    encodeSigned(iLng - prevLng, out)
    prevLat = iLat
    prevLng = iLng
  }
  return out.join('')
}

/**
 * Decode a ring back into a polygon.
 *
 * Returns null for anything malformed rather than throwing. A link is
 * attacker-controlled in the ordinary sense that anyone can edit one before
 * sending it, and the failure a person will actually hit is a chat client
 * clipping the last few characters. Neither should be a blank page: the caller
 * treats null as "no field in this URL" and shows the ordinary empty state.
 */
export function decodeField(encoded: string): DrawnPolygon | null {
  const coords: [number, number][] = []
  let i = 0
  let lat = 0
  let lng = 0

  while (i < encoded.length) {
    const pair: number[] = []
    for (let k = 0; k < 2; k++) {
      let result = 0
      let shift = 0
      let byte: number
      do {
        if (i >= encoded.length) return null // truncated mid-number
        const index = REVERSE.get(encoded[i++])
        if (index === undefined) return null // not a character this alphabet emits
        byte = index
        result |= (byte & 0x1f) << shift
        shift += 5
      } while (byte >= 0x20)
      pair.push(result & 1 ? ~(result >> 1) : result >> 1)
    }
    lat += pair[0]
    lng += pair[1]
    const y = lat / PRECISION
    const x = lng / PRECISION
    // A decoder that has lost sync produces plausible-looking numbers far
    // outside the world, and a polygon at longitude 400 renders as nothing at
    // all with no explanation. Reject it here instead.
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null
    if (x < -180 || x > 180 || y < -90 || y > 90) return null
    coords.push([x, y])
  }

  if (coords.length < 3) return null
  return { type: 'Polygon', coordinates: [[...coords, coords[0]]] }
}

/** The field this URL carries, or null if it carries none. */
export function fieldFromUrl(search: string = window.location.search): DrawnPolygon | null {
  const raw = new URLSearchParams(search).get(FIELD_PARAM)
  return raw ? decodeField(raw) : null
}

/**
 * Point the address bar at the current field.
 *
 * `replaceState`, not `pushState`: drawing is an edit to one view rather than
 * navigation between views, and a person who traces six fields while exploring
 * should not have to press Back six times to leave the page.
 */
export function writeFieldToUrl(geometry: DrawnPolygon | null): void {
  const url = new URL(window.location.href)
  const encoded = geometry ? encodeField(geometry) : null
  if (encoded) url.searchParams.set(FIELD_PARAM, encoded)
  else url.searchParams.delete(FIELD_PARAM)
  window.history.replaceState(null, '', url)
}

/** Bounding box of a polygon, as MapLibre's [[w, s], [e, n]]. */
export function boundsOf(
  geometry: DrawnPolygon,
): [[number, number], [number, number]] | null {
  const rings =
    geometry.type === 'Polygon' ? geometry.coordinates : geometry.coordinates.flat()
  let w = Infinity
  let s = Infinity
  let e = -Infinity
  let n = -Infinity
  for (const ring of rings) {
    for (const [x, y] of ring) {
      if (x < w) w = x
      if (x > e) e = x
      if (y < s) s = y
      if (y > n) n = y
    }
  }
  return Number.isFinite(w) ? [[w, s], [e, n]] : null
}
