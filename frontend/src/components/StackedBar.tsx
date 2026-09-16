import { useState } from 'react'

export interface Segment {
  id: string
  label: string
  value: number
  color: string
  /** Optional second line in the tooltip. */
  note?: string
}

interface Props {
  segments: Segment[]
  /** Denominator for the shares. Defaults to the sum of the segments. */
  total?: number
  formatValue: (value: number) => string
  /** Bar thickness. Kept thin: the mark spec caps bars at 24px. */
  height?: number
}

/**
 * A single horizontal 100% stacked bar.
 *
 * Horizontal because the categories have long names, and part-to-whole with
 * many long-named categories is the case horizontal stacking exists for.
 *
 * Two details that are specs rather than taste: segments are separated by a
 * 2px gap **in the surface colour**, never by a border -- a stroke adds ink
 * that is not data -- and only the two outer ends are rounded, so the bar
 * reads as one quantity divided up rather than as a row of separate pills.
 */
export function StackedBar({ segments, total, formatValue, height = 14 }: Props) {
  const [hover, setHover] = useState<string | null>(null)
  const sum = total ?? segments.reduce((acc, s) => acc + s.value, 0)
  if (sum <= 0) return null

  const active = segments.find((s) => s.id === hover)

  return (
    <div>
      {/* Vertical padding enlarges the hit target well past the 14px mark. */}
      <div
        className="flex w-full gap-[2px] py-[5px]"
        onMouseLeave={() => setHover(null)}
        role="img"
        aria-label={segments
          .map((s) => `${s.label} ${((s.value / sum) * 100).toFixed(1)}%`)
          .join(', ')}
      >
        {segments.map((s, i) => {
          const share = s.value / sum
          if (share <= 0) return null
          return (
            <div
              key={s.id}
              onMouseEnter={() => setHover(s.id)}
              style={{
                flexGrow: share,
                flexBasis: 0,
                height,
                background: s.color,
                opacity: hover === null || hover === s.id ? 1 : 0.45,
                borderTopLeftRadius: i === 0 ? 4 : 0,
                borderBottomLeftRadius: i === 0 ? 4 : 0,
                borderTopRightRadius: i === segments.length - 1 ? 4 : 0,
                borderBottomRightRadius: i === segments.length - 1 ? 4 : 0,
                transition: 'opacity 120ms ease',
                minWidth: 2,
              }}
            />
          )
        })}
      </div>

      {/* One reserved line, so hovering never reflows the panel below it. */}
      <div className="mt-1 flex h-[18px] items-center gap-2 text-[11.5px]">
        {active ? (
          <>
            <span
              className="inline-block h-[9px] w-[9px] shrink-0 rounded-[2px]"
              style={{ background: active.color }}
            />
            <span className="truncate text-ink-2">{active.label}</span>
            <span className="tabular ml-auto shrink-0 text-muted">
              {formatValue(active.value)} · {((active.value / sum) * 100).toFixed(1)}%
            </span>
          </>
        ) : (
          <span className="text-muted">Hover a segment for detail</span>
        )}
      </div>
    </div>
  )
}
