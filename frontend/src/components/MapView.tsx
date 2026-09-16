import { useEffect, useRef } from 'react'
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
import type { DrawnPolygon, MapUnitGeometry } from '../types'
import type { ExampleField } from '../examples'

const EMPTY: GeoJSON.FeatureCollection = { type: 'FeatureCollection', features: [] }

const SRC = 'fieldscope-mapunits'
const FILL = 'fieldscope-mapunits-fill'
const LINE = 'fieldscope-mapunits-line'

interface Props {
  aoi: Aoi
  mapUnits: MapUnitGeometry | null
  onPolygon: (geometry: DrawnPolygon) => void
  onClear: () => void
  /** Set to draw a stored field; cleared by the parent once handled. */
  example: ExampleField | null
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
  example,
  overviewNonce,
  armNonce,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<MapLibreMap | null>(null)
  const drawRef = useRef<MaplibreTerradrawControl | null>(null)
  const loadedRef = useRef(false)

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
      terra?.setMode('polygon')

      if (mapUnits) {
        ;(map.getSource(SRC) as GeoJSONSource).setData(paint(mapUnits))
      }
    })

    return () => {
      loadedRef.current = false
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

  // Draw a stored example and fly to it.
  useEffect(() => {
    if (!example) return
    const map = mapRef.current
    const terra = drawRef.current?.getTerraDrawInstance()
    if (!map || !terra) return
    terra.clear()
    terra.addFeatures([
      {
        id: crypto.randomUUID(),
        type: 'Feature',
        geometry: example.geometry as GeoJSON.Polygon,
        properties: { mode: 'polygon' },
      },
    ])
    map.flyTo({ center: example.center, zoom: example.zoom, duration: 900 })
    terra.setMode('polygon')
  }, [example])

  useEffect(() => {
    if (armNonce === 0) return
    drawRef.current?.getTerraDrawInstance()?.setMode('polygon')
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
