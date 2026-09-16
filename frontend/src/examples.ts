/**
 * Example fields, so the map is useful in one click.
 *
 * These are not invented coordinates: each is a real polygon from
 * scripts/bench_polygons_california.json, the same AOI-derived set the serving
 * benchmark runs against. They were chosen by running all 90 through the API
 * and picking three that land on different readings -- ground farmed to its
 * capability, ground capable of far more than it carries, and ground that is
 * marginal and used as such. One example would make the insight layer look
 * like it only ever says one thing.
 *
 * Generated from that fixture; regenerate rather than hand-editing.
 */

import type { DrawnPolygon } from './types'

export interface ExampleField {
  id: string
  label: string
  note: string
  center: [number, number]
  zoom: number
  geometry: DrawnPolygon
}

export const EXAMPLES: ExampleField[] = [
  {
    "id": "almonds",
    "label": "Almond orchard",
    "note": "Fresno County \u2014 prime soil, farmed to its capability",
    "center": [
      -119.51147,
      36.66261
    ],
    "zoom": 13.2,
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [
          [
            -119.5258,
            36.65111
          ],
          [
            -119.48996,
            36.65111
          ],
          [
            -119.48996,
            36.67985
          ],
          [
            -119.5258,
            36.67985
          ],
          [
            -119.5258,
            36.65111
          ]
        ]
      ]
    }
  },
  {
    "id": "idle",
    "label": "Idle cropland",
    "note": "Ten acres of Hanford loam: fully cultivable, almost nothing growing",
    "center": [
      -117.16082,
      33.82962
    ],
    "zoom": 15.6,
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [
          [
            -117.16169,
            33.8289
          ],
          [
            -117.15953,
            33.8289
          ],
          [
            -117.15953,
            33.83069
          ],
          [
            -117.16169,
            33.83069
          ],
          [
            -117.16169,
            33.8289
          ]
        ]
      ]
    }
  },
  {
    "id": "drought",
    "label": "Steep forest in drought",
    "note": "Siskiyou County \u2014 class 5\u20138 ground across three USDM severities",
    "center": [
      -123.27484,
      41.40745
    ],
    "zoom": 14.6,
    "geometry": {
      "type": "Polygon",
      "coordinates": [
        [
          [
            -123.28251,
            41.4017
          ],
          [
            -123.26335,
            41.4017
          ],
          [
            -123.26335,
            41.41607
          ],
          [
            -123.28251,
            41.41607
          ],
          [
            -123.28251,
            41.4017
          ]
        ]
      ]
    }
  }
]
