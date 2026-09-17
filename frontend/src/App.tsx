import { useCallback, useEffect, useRef, useState } from 'react'
import { MapView } from './components/MapView'
import { ErrorBoundary } from './components/ErrorBoundary'
import { ResultsPanel } from './components/ResultsPanel'
import { ApiError, fetchArea, fetchMapUnits } from './api'
import { DEFAULT_AOI, AOIS, API_BASE } from './config'
import { EXAMPLES, type ExampleField } from './examples'
import { fieldFromUrl, writeFieldToUrl } from './share'
import type { AreaResponse, DrawnPolygon, MapUnitGeometry, PlacedField } from './types'

type Status = 'idle' | 'loading' | 'ready' | 'error'

export default function App() {
  const [aoi] = useState(DEFAULT_AOI)
  const [status, setStatus] = useState<Status>('idle')
  const [error, setError] = useState<{ message: string; tooLarge: boolean } | null>(null)
  const [area, setArea] = useState<AreaResponse | null>(null)
  const [mapUnits, setMapUnits] = useState<MapUnitGeometry | null>(null)
  const [placed, setPlaced] = useState<PlacedField | null>(null)
  const [overviewNonce, setOverviewNonce] = useState(0)
  const [armNonce, setArmNonce] = useState(0)

  // A redraw while a request is in flight must not race: the older response
  // could land second and describe a field that is no longer on the map.
  const inflight = useRef<AbortController | null>(null)

  const runQuery = useCallback(async (geometry: DrawnPolygon) => {
    inflight.current?.abort()
    const ctl = new AbortController()
    inflight.current = ctl

    setStatus('loading')
    setError(null)

    // The link describes the field, not the answer, so it is written when the
    // question is asked rather than when it comes back. A field that turns out
    // to be too large still produces a link that reproduces exactly that --
    // which is the honest thing for a link to do.
    writeFieldToUrl(geometry)

    try {
      // The numbers are small and arrive first; the geometry can be hundreds
      // of kilobytes, so it is not allowed to hold up the readout.
      const areaPromise = fetchArea(geometry, ctl.signal)

      // The geometry is decoration over an answer that stands without it, so
      // it is not allowed to fail the request either -- a panel reporting "that
      // did not work" over numbers it successfully fetched would be a lie.
      //
      // It also must not reject on its own. Both requests start together but
      // are awaited one at a time, so whichever is not being awaited when an
      // abort lands would surface as an unhandled rejection -- and every
      // redraw aborts.
      const unitsPromise = fetchMapUnits(geometry, ctl.signal).catch((err) => {
        if (!(err instanceof DOMException && err.name === 'AbortError')) {
          console.warn('Fieldscope: the map unit geometry did not load.', err)
        }
        return null
      })

      const areaResult = await areaPromise
      if (ctl.signal.aborted) return
      setArea(areaResult)
      setStatus('ready')

      const unitsResult = await unitsPromise
      if (ctl.signal.aborted || !unitsResult) return
      setMapUnits(unitsResult)
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return
      const apiErr = err instanceof ApiError ? err : null
      setError({
        message: apiErr?.message ?? 'Something went wrong.',
        tooLarge: apiErr?.status === 413,
      })
      setStatus('error')
      setArea(null)
      setMapUnits(null)
    }
  }, [])

  const handleClear = useCallback(() => {
    inflight.current?.abort()
    writeFieldToUrl(null)
    setPlaced(null)
    setStatus('idle')
    setError(null)
    setArea(null)
    setMapUnits(null)
  }, [])

  const loadExample = (ex: ExampleField) => {
    setPlaced((prev) => ({
      geometry: ex.geometry,
      view: { center: ex.center, zoom: ex.zoom },
      nonce: (prev?.nonce ?? 0) + 1,
    }))
    runQuery(ex.geometry)
  }

  // A field arriving in the URL. Read once, on arrival: later edits to the
  // address bar are this app's own `replaceState` calls, and re-reading them
  // would put the page in a loop with itself.
  useEffect(() => {
    const shared = fieldFromUrl()
    if (!shared) return
    setPlaced({ geometry: shared, view: 'fit', nonce: 1 })
    runQuery(shared)
  }, [runQuery])

  return (
    <div className="relative h-full w-full overflow-hidden">
      <ErrorBoundary
        fallback={(message) => (
          <div className="absolute inset-0 flex items-center justify-center p-8">
            <div className="max-w-md text-center">
              <p className="text-[13px] text-warn">The map could not start.</p>
              <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted">{message}</p>
            </div>
          </div>
        )}
      >
        <MapView
          aoi={aoi}
          mapUnits={mapUnits}
          onPolygon={runQuery}
          onClear={handleClear}
          placed={placed}
          overviewNonce={overviewNonce}
          armNonce={armNonce}
        />
      </ErrorBoundary>

      {/* Wordmark. Deliberately small: the map is the page. */}
      <div className="pointer-events-none absolute top-4 left-4 z-10">
        <h1 className="text-[15px] font-semibold tracking-tight text-ink">Fieldscope</h1>
        <p className="mt-0.5 text-[11px] text-muted">
          {aoi.name}
          {AOIS.length > 1 && ' · switchable'}
        </p>
      </div>

      <aside className="absolute top-4 right-16 bottom-4 z-10 flex w-[380px] max-w-[calc(100vw-5rem)] flex-col overflow-hidden rounded-2xl border border-rule bg-card/92 backdrop-blur-md">
        <div className="border-b border-rule px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <div className="text-[10.5px] font-medium tracking-[0.09em] text-muted uppercase">
              {status === 'idle' && 'Draw a field'}
              {status === 'loading' && 'Measuring…'}
              {status === 'ready' && 'Under this field'}
              {status === 'error' && 'That did not work'}
            </div>
            {status === 'ready' && <CopyLinkButton />}
          </div>

          {status === 'idle' && (
            <>
              <p className="mt-2 text-[12.5px] leading-relaxed text-ink-2">
                <span className="text-ink">Click corners on the map</span>, then double-click
                to close the shape. The polygon tool is already active.
              </p>
              <ExampleButtons onPick={loadExample} />
            </>
          )}

          {status === 'error' && error && (
            <>
              <p className="mt-2 text-[12.5px] leading-relaxed text-warn">{error.message}</p>
              {error.tooLarge && (
                <p className="mt-2 text-[11.5px] leading-relaxed text-muted">
                  This tool answers questions about fields, not regions. Zoom in until a
                  block or two fills the screen, then trace one field.
                </p>
              )}
              <button
                onClick={() => {
                  handleClear()
                  setArmNonce((n) => n + 1)
                }}
                className="mt-3 w-full rounded-lg border border-rule px-3 py-1.5 text-[11.5px] text-ink-2 transition-colors hover:border-muted hover:text-ink"
              >
                Try again
              </button>
            </>
          )}

          {status === 'loading' && (
            <p className="mt-2 text-[12.5px] text-muted">
              Intersecting against 484,325 soil polygons…
            </p>
          )}
        </div>

        {area && status === 'ready' && (
          <ResultsPanel area={area} aoiDroughtWeek={aoi.droughtWeek} />
        )}

        {status === 'ready' && (
          <div className="border-t border-rule px-5 py-3">
            <ExampleButtons onPick={loadExample} compact />
          </div>
        )}

        <div className="flex items-center justify-between border-t border-rule px-5 py-2.5">
          <button
            onClick={() => setOverviewNonce((n) => n + 1)}
            className="text-[10.5px] text-muted transition-colors hover:text-ink-2"
          >
            See all of {aoi.name}
          </button>
          <span className="font-mono text-[10px] text-muted">{API_BASE.replace(/^https?:\/\//, '')}</span>
        </div>
      </aside>
    </div>
  )
}

function ExampleButtons({
  onPick,
  compact = false,
}: {
  onPick: (ex: ExampleField) => void
  compact?: boolean
}) {
  return (
    <div className={compact ? '' : 'mt-3.5'}>
      {!compact && (
        <div className="mb-1.5 text-[10px] font-medium tracking-[0.09em] text-muted uppercase">
          Or try one
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        {EXAMPLES.map((ex) => (
          <button
            key={ex.id}
            onClick={() => onPick(ex)}
            className="group rounded-lg border border-rule px-3 py-2 text-left transition-colors hover:border-muted"
          >
            <div className="text-[12px] text-ink-2 transition-colors group-hover:text-ink">
              {ex.label}
            </div>
            {!compact && <div className="mt-0.5 text-[10.5px] text-muted">{ex.note}</div>}
          </button>
        ))}
      </div>
    </div>
  )
}

/**
 * Copy the current field's link.
 *
 * The URL is already correct before this is pressed -- `runQuery` writes it on
 * every draw -- so this is a convenience over the address bar, not the
 * mechanism. That is why a copy failure is not an error state: the link is
 * still there to be copied by hand, and saying so is more useful than an
 * apology.
 *
 * `navigator.clipboard` needs a secure context, which covers https and
 * localhost but not a bare-IP origin over http, so the failure path is real
 * rather than defensive.
 */
function CopyLinkButton() {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')

  // A component that unmounts while the "Copied" timer is pending would
  // otherwise set state on nothing.
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href)
      setState('copied')
    } catch {
      setState('failed')
    }
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setState('idle'), 2400)
  }

  return (
    <button
      onClick={copy}
      title="A link that reopens this exact field"
      className="shrink-0 rounded-md border border-rule px-2 py-1 text-[10.5px] whitespace-nowrap text-muted transition-colors hover:border-muted hover:text-ink"
    >
      {state === 'idle' && 'Copy link'}
      {state === 'copied' && 'Link copied'}
      {state === 'failed' && 'In the address bar'}
    </button>
  )
}
