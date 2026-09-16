/**
 * Typed client for the serving API.
 *
 * Both polygon endpoints take the same body and are called for the same draw,
 * but they are deliberately separate requests: /area answers in a few
 * kilobytes and /area/mapunits can return hundreds, so the numbers render
 * while the geometry is still arriving (docs/DESIGN.md, queries.py).
 */

import { API_BASE } from './config'
import type { AreaResponse, DrawnPolygon, MapUnitGeometry } from './types'

/** An error the API described, as opposed to the network failing. */
export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

async function post<T>(path: string, geometry: DrawnPolygon, signal?: AbortSignal): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ geometry }),
      signal,
    })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err
    // A failed fetch is indistinguishable from a CORS rejection in the
    // browser, and the tunnel hostname rotating is the likeliest cause of
    // both -- so say that rather than "Failed to fetch".
    throw new ApiError(0, `Could not reach the API at ${API_BASE}.`)
  }

  if (!res.ok) {
    // FastAPI puts the human-readable reason in `detail`; the validation
    // errors it raises put a list there instead.
    let detail = `Request failed (${res.status}).`
    try {
      const body = await res.json()
      if (typeof body.detail === 'string') detail = body.detail
      else if (Array.isArray(body.detail) && body.detail[0]?.msg) detail = body.detail[0].msg
    } catch {
      /* a non-JSON error body is not worth failing over */
    }
    throw new ApiError(res.status, detail)
  }

  return (await res.json()) as T
}

export const fetchArea = (geometry: DrawnPolygon, signal?: AbortSignal) =>
  post<AreaResponse>('/area', geometry, signal)

export const fetchMapUnits = (geometry: DrawnPolygon, signal?: AbortSignal) =>
  post<MapUnitGeometry>('/area/mapunits', geometry, signal)
