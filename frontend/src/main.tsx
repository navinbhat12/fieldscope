import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { setWorkerUrl } from 'maplibre-gl'
// `?worker&url` makes the bundler build the worker as its own chunk -- with
// its `./maplibre-gl-shared.mjs` import resolved -- and hand back the URL.
import maplibreWorkerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url'

import 'maplibre-gl/dist/maplibre-gl.css'
import '@watergis/maplibre-gl-terradraw/dist/maplibre-gl-terradraw.css'
import './index.css'

import App from './App'

/**
 * Tell MapLibre where its worker actually is.
 *
 * MapLibre derives the worker URL from its own `import.meta.url`, expecting to
 * find `maplibre-gl-worker.mjs` as a sibling. That holds when the package is
 * served as authored, which is why `optimizeDeps.exclude` was enough to fix
 * development (§14.5). It does not hold in a production build: MapLibre is
 * bundled into `assets/index-<hash>.js`, so `import.meta.url` is the bundle's
 * URL and the sibling it looks for has never existed. The worker 404s, the
 * `Map` constructor throws, and the error boundary reports that the map could
 * not start -- which is exactly what the deployed site did.
 *
 * The worker path is built as a ternary on a dev/prod filename, so no bundler
 * can follow it statically and none emits the file. Pointing MapLibre at a
 * worker the bundler *did* emit is the fix, and it works the same way in
 * development and production rather than relying on two different mechanisms.
 *
 * Runs before `createRoot`, and every `Map` is constructed inside an effect,
 * so it is set well before anything reads it.
 */
setWorkerUrl(maplibreWorkerUrl)

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
