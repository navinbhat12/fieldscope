import { useEffect, useRef, useState } from 'react'
// maplibre-gl v6 is ESM-only with named exports and no default export.
// `Map` is aliased because the global of that name is very much still in use.
import {
  MapLibreMap,
  NavigationControl,
  ScaleControl,
  type GeoJSONSource,
} from 'maplibre-gl'
import { MaplibreTerradrawControl } from '@watergis/maplibre-gl-terradraw'
import { BASEMAP_STYLE, type Aoi } from '../config'
import { colorForCode } from '../landcover'
import { boundsOf } from '../share'
import type { DrawnPolygon, MapUnitGeometry, PlacedField } from '../types'

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

type Terra = NonNullable<ReturnType<MaplibreTerradrawControl['getTerraDrawInstance']>>

/** ~2s at 60fps. Past this the tool is not coming up and something is wrong. */
const DRAW_READY_FRAMES = 120

/**
 * Call into Terra Draw once it will actually accept the call.
 *
 * The control starts its Terra Draw instance on its own schedule, and calling
 * a method before that throws `Terra Draw is not enabled`. Two things made
 * that a crash rather than a hiccup: the throw happens inside a React effect,
 * where it propagates to the error boundary and takes the entire map down with
 * it, and a field arriving in the URL is ready to be drawn before the control
 * is ready to draw it -- so the one path that had never raced now always did.
 *
 * The control starts its instance from its own `map.once('load')`, and guards
 * each of its own entry points with `terradraw.enabled || terradraw.start()`.
 * Doing the same here makes the common case deterministic rather than a race
 * that happens to resolve; the per-frame retry is the backstop for a control
 * that is not constructed yet, since it exposes no "started" event to wait on.
 * Bounded, so a tool that never comes up says so once instead of spinning.
 *
 * Returns a cancel function: an effect that is torn down mid-retry must not
 * keep poking at a map the next effect has already replaced.
 */
function whenDrawable(
  getTerra: () => Terra | undefined,
  fn: (terra: Terra) => void,
  what: string,
): () => void {
  let cancelled = false
  let frames = 0

  const attempt = () => {
    if (cancelled) return
    const terra = getTerra()
    if (terra) {
      try {
        // Reading `enabled` is safe; assigning it throws by design.
        if (!terra.enabled) terra.start()
        fn(terra)
        return
      } catch (err) {
        // Not-enabled-yet is the expected case and is retried silently. A
        // different failure will still be retried -- there is no way to tell
        // them apart without matching on message text -- but it gets said out
        // loud when the budget runs out.
        if (frames >= DRAW_READY_FRAMES) {
          console.warn(`Fieldscope: gave up ${what}:`, err)
          return
        }
      }
    }
    if (++frames > DRAW_READY_FRAMES) {
      console.warn(`Fieldscope: gave up ${what}; the draw tool never started`)
      return
    }
    requestAnimationFrame(attempt)
  }

  attempt()
  return () => {
    cancelled = true
  }
}

const SRC = 'fieldscope-mapunits'
const FILL = 'fieldscope-mapunits-fill'
const LINE = 'fieldscope-mapunits-line'

interface Props {
  aoi: Aoi
  mapUnits: MapUnitGeometry | null
  onPolygon: (geometry: DrawnPolygon) => void
  onClear: () => void
  /** Set to draw a field the pointer did not trace: an example, or a link. */
  placed: PlacedField | null
  /** Bumped by the parent to fly out to the whole AOI. */
  overviewNonce: number
  /** Bumped by the parent to re-arm the polygon tool. */
  armNonce: number
}

/**
 * Give every map unit the colour of its dominant land cover's group, so the
 * mosaic on the map and the bar in the panel are the same encoding. Done here
 * rather than in a MapLibre `match` expression over 134 CDL codes, because the
 * grouping already exists in TypeScript and duplicating it as a style
 * expression would be two things to keep in step.
 */
function paint(units: MapUnitGeometry): GeoJSON.FeatureCollection {
  return {
    type: 'FeatureCollection',
    features: units.features.map((f) => ({
      type: 'Feature',
      geometry: f.geometry,
      properties: { ...f.properties, color: colorForCode(f.properties.crop_code) },
    })) as GeoJSON.Feature[],
  }
}

export function MapView({
  aoi,
  mapUnits,
  onPolygon,
  onClear,
  placed,
  overviewNonce,
  armNonce,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const drawRef = useRef<MaplibreTerradrawControl | null>(null)
  const loadedRef = useRef(false)

  // Loading is also *state*, not only a ref: a field arriving in the URL is
  // known before the map exists, so the effect that draws it has to re-run
  // once the map is ready rather than give up. A ref alone would not re-render
  // and the shared field would never appear.
  const [ready, setReady] = useState(false)

  // The handlers are attached once, when the map is built, but they must call
  // the current render's callbacks -- so they read through a ref rather than
  // closing over the props they saw at mount.
  const handlers = useRef({ onPolygon, onClear })
  handlers.current = { onPolygon, onClear }

  useEffect(() => {
    if (!containerRef.current) return

    const map = new MapLibreMap({
      container: containerRef.current,
      style: BASEMAP_STYLE,
      center: aoi.start.center,
      zoom: aoi.start.zoom,
      attributionControl: { compact: true },
      // The data is a flat choropleth; tilting it buys nothing and makes
      // areas harder to compare by eye.
      pitchWithRotate: false,
      dragRotate: false,
    })
    mapRef.current = map

    // Basemap failures are otherwise silent: the style loads, no tiles
    // arrive, and the map sits black with nothing said. A third-party CDN is
    // the one part of this app that can fail without any of it being wrong,
    // so it gets logged rather than swallowed.
    map.on('error', (e) => {
      console.warn('Fieldscope basemap:', e.error?.message ?? e)
    })

    // MapLibre sizes its canvas from the container once, at construction, and
    // does not watch it afterwards. Vite injects stylesheets asynchronously in
    // development, so this effect can run while the container is still
    // unstyled and collapsed; the canvas would lock in that size and never
    // recover. Watching the element also covers window resizes and rotation.
    const observer = new ResizeObserver(() => map.resize())
    observer.observe(containerRef.current)

    map.addControl(new NavigationControl({ showCompass: false }), 'bottom-right')
    map.addControl(new ScaleControl({ unit: 'imperial' }), 'bottom-left')

    const draw = new MaplibreTerradrawControl({
      modes: ['polygon', 'freehand', 'delete'],
      open: true,
    })
    map.addControl(draw, 'top-right')
    drawRef.current = draw

    let cancelArm: (() => void) | null = null

    const terra = draw.getTerraDrawInstance()
    terra?.on('finish', (id, context) => {
      // 'finish' also fires when an existing shape is dragged; only a
      // completed drawing should trigger a query.
      if (context.action !== 'draw') return
      const feature = terra.getSnapshotFeature(id)
      if (!feature) return
      const { geometry } = feature
      // Terra Draw's store holds only Point, LineString and Polygon, so a
      // completed area is always a plain Polygon.
      if (geometry.type !== 'Polygon') return

      // One field at a time. Leaving old shapes on the map while the panel
      // describes only the newest one would be a lie about what was measured.
      const stale = terra
        .getSnapshot()
        .map((f) => f.id)
        .filter((fid): fid is string | number => fid !== undefined && fid !== id)
      if (stale.length) terra.removeFeatures(stale)

      handlers.current.onPolygon(geometry)
    })

    draw.on('feature-deleted', () => handlers.current.onClear())

    map.on('load', () => {
      loadedRef.current = true
      setReady(true)
      map.addSource(SRC, { type: 'geojson', data: EMPTY })

      map.addLayer({
        id: FILL,
        type: 'fill',
        source: SRC,
        paint: { 'fill-color': ['get', 'color'], 'fill-opacity': 0.5 },
      })
      map.addLayer({
        id: LINE,
        type: 'line',
        source: SRC,
        paint: {
          'line-color': '#0f0e0d',
          'line-width': 0.6,
          'line-opacity': 0.55,
        },
      })

      // Arm the polygon tool immediately.
      //
      // Terra Draw starts in no mode at all, so until the toolbar button is
      // pressed every click and drag is just a pan -- which reads as "drawing
      // is broken" rather than "the tool is not selected". Drawing is the only
      // thing this page asks anyone to do, so it is armed on arrival.
      cancelArm = whenDrawable(
        () => drawRef.current?.getTerraDrawInstance(),
        (t) => t.setMode('polygon'),
        'arming the polygon tool',
      )

      if (mapUnits) {
        ;(map.getSource(SRC) as GeoJSONSource).setData(paint(mapUnits))
      }
    })

    return () => {
      loadedRef.current = false
      setReady(false)
      cancelArm?.()
      observer.disconnect()
      drawRef.current = null
      map.remove()
      mapRef.current = null
    }
    // Built once. View changes fly the camera instead of rebuilding the map.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    const map = mapRef.current
    if (!map || !loadedRef.current) return
    const source = map.getSource(SRC) as GeoJSONSource | undefined
    source?.setData(mapUnits ? paint(mapUnits) : EMPTY)
  }, [mapUnits])

  // Draw a field that arrived from somewhere other than the pointer.
  useEffect(() => {
    if (!placed || !ready) return
    const map = mapRef.current
    if (!map) return

    return whenDrawable(
      () => drawRef.current?.getTerraDrawInstance(),
      (terra) => {
        // Clear first, so a retry after a partial failure replaces the shape
        // rather than stacking a second copy on top of it.
        terra.clear()
        terra.addFeatures([
          {
            id: crypto.randomUUID(),
            type: 'Feature',
            geometry: placed.geometry as GeoJSON.Polygon,
            properties: { mode: 'polygon' },
          },
        ])
        terra.setMode('polygon')
        moveCamera(map, placed)
      },
      'drawing the shared field',
    )
  }, [placed, ready])

  useEffect(() => {
    if (armNonce === 0) return
    return whenDrawable(
      () => drawRef.current?.getTerraDrawInstance(),
      (t) => t.setMode('polygon'),
      're-arming the polygon tool',
    )
  }, [armNonce])

  useEffect(() => {
    if (overviewNonce === 0) return
    mapRef.current?.flyTo({
      center: aoi.overview.center,
      zoom: aoi.overview.zoom,
      duration: 1100,
    })
  }, [overviewNonce, aoi])

  return (
    <div
      ref={containerRef}
      /*
       * Inline, not a Tailwind class, and this is load-bearing.
       *
       * MapLibre adds `.maplibregl-map` to this very element, and
       * maplibre-gl.css is unlayered while Tailwind v4 emits its utilities
       * inside `@layer utilities`. In the cascade, unlayered styles beat
       * layered ones whatever their specificity or order -- so
       * `.maplibregl-map { position: relative }` quietly wins over
       * `.absolute`. The element then lays out as an ordinary block: full
       * width, and zero height because it has no content. MapLibre reads that
       * with `clientHeight || 300` and builds a 300px canvas inside a
       * full-height container, which looks exactly like a map that failed to
       * load.
       *
       * An inline style outranks both layers and unlayered rules. Any future
       * geometry on this element belongs here for the same reason.
       */
      style={{ position: 'absolute', inset: 0 }}
    />
  )
}

/** Frame a placed field: its own curated camera, or fitted to its bounds. */
function moveCamera(map: MapLibreMap, placed: PlacedField): void {
  if (placed.view !== 'fit') {
    map.flyTo({ center: placed.view.center, zoom: placed.view.zoom, duration: 900 })
    return
  }

  const bounds = boundsOf(placed.geometry)
  if (!bounds) return

  map.fitBounds(bounds, {
    // Pad past the panel. The panel is an overlay, so MapLibre knows nothing
    // about it and would centre the field underneath it -- which on a small
    // field means framing it perfectly and then hiding it.
    padding: {
      top: 72,
      bottom: 72,
      left: 72,
      right: Math.min(470, Math.round(window.innerWidth * 0.5)),
    },
    // A ten-acre field fitted to the viewport would sit at zoom 18, past the
    // point where the basemap has anything left to say.
    maxZoom: 16,
    duration: 900,
  })
}
