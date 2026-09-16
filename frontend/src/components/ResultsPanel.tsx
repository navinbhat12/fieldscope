import { useMemo, useState } from 'react'
import { StackedBar, type Segment } from './StackedBar'
import { GROUPS, droughtStep, groupForCode } from '../landcover'
import type { AreaResponse, Finding, Insight, SoilSummary } from '../types'

const acres = (n: number) =>
  n >= 100 ? Math.round(n).toLocaleString() : n >= 1 ? n.toFixed(1) : n.toFixed(2)

const pct = (f: number) => `${(f * 100).toFixed(f >= 0.1 ? 0 : 1)}%`

/** How many individual crops to list before folding the rest away. */
const VISIBLE_ROWS = 8

export function ResultsPanel({ area, aoiDroughtWeek }: { area: AreaResponse; aoiDroughtWeek: string }) {
  const [expanded, setExpanded] = useState(false)

  const { groupSegments, rows, agShare, answered } = useMemo(() => {
    const answered = area.breakdown.reduce((a, s) => a + s.acres, 0) || 1

    // Groups are summed, then emitted in the palette's fixed order -- never in
    // size order. The pairs that touch in the bar are then always the pairs the
    // palette was validated on, and a crop does not change colour between one
    // field and the next.
    const totals = new Map<string, number>()
    for (const s of area.breakdown) {
      const g = groupForCode(s.crop_code)
      totals.set(g.id, (totals.get(g.id) ?? 0) + s.acres)
    }
    const groupSegments: Segment[] = GROUPS.filter((g) => (totals.get(g.id) ?? 0) > 0).map(
      (g) => ({ id: g.id, label: g.label, value: totals.get(g.id)!, color: g.color }),
    )

    const rows = [...area.breakdown].sort((a, b) => b.acres - a.acres)
    const agShare = area.breakdown.reduce((a, s) => a + (s.is_agricultural ? s.acres : 0), 0) / answered

    return { groupSegments, rows, agShare, answered }
  }, [area])

  const droughtSegments: Segment[] = area.drought.map((d) => {
    const step = droughtStep(d.drought_class)
    return { id: String(d.drought_class), label: step.label, value: d.acres, color: step.color }
  })

  const shown = expanded ? rows : rows.slice(0, VISIBLE_ROWS)
  const hidden = rows.length - shown.length
  const hiddenAcres = rows.slice(shown.length).reduce((a, s) => a + s.acres, 0)

  return (
    <div className="flex-1 overflow-y-auto px-5 pb-5">
      {/* Hero. Exactly one per view, and proportional figures -- tabular-nums
          makes a large standalone number look loose. */}
      <div className="pt-4">
        <div className="flex items-baseline gap-2">
          <span className="text-[44px] leading-none font-semibold tracking-tight text-ink">
            {acres(area.query_acres)}
          </span>
          <span className="text-[13px] text-muted">acres</span>
        </div>
        <p className="mt-1.5 text-[11.5px] text-muted">
          {pct(area.coverage)} of it fell on mapped soil, assembled from{' '}
          {area.map_units.toLocaleString()} soil map unit{area.map_units === 1 ? '' : 's'}
          {area.cached && ' · cached'}
        </p>
      </div>

      {area.insight && <Reading insight={area.insight} />}

      {area.soil && area.soil.dominant_name && <SoilFacts soil={area.soil} />}

      {area.breakdown.length === 0 ? (
        <p className="mt-5 text-[13px] leading-relaxed text-ink-2">
          Nothing here. The polygon fell outside the soil survey — open water, or beyond the
          state. That is a real answer, not an error.
        </p>
      ) : (
        <>
          <Section title="Land cover" note={`${area.breakdown.length} categories`}>
            <StackedBar
              segments={groupSegments}
              total={answered}
              formatValue={(v) => `${acres(v)} ac`}
            />

            {/* The legend. Always present, because identity must never rest on
                colour alone -- the swatch sits beside the name, never on it. */}
            <ul className="mt-2 space-y-[3px]">
              {groupSegments
                .slice()
                .sort((a, b) => b.value - a.value)
                .map((g) => (
                  <li key={g.id} className="flex items-center gap-2 text-[12px]">
                    <span
                      className="inline-block h-[9px] w-[9px] shrink-0 rounded-[2px]"
                      style={{ background: g.color }}
                    />
                    <span className="truncate text-ink-2">{g.label}</span>
                    <span className="tabular ml-auto shrink-0 text-muted">
                      {pct(g.value / answered)}
                    </span>
                  </li>
                ))}
            </ul>

            <div className="mt-3.5 text-[10px] font-medium tracking-[0.09em] text-muted uppercase">
              By crop
            </div>
            <ul className="mt-1.5">
              {shown.map((s) => {
                const share = s.acres / answered
                return (
                  <li key={`${s.crop_code}-${s.land_cover}`} className="py-[3px]">
                    <div className="flex items-center gap-2 text-[12px]">
                      <span
                        className="inline-block h-[9px] w-[9px] shrink-0 rounded-[2px]"
                        style={{ background: groupForCode(s.crop_code).color }}
                      />
                      <span className="truncate text-ink-2">{s.land_cover}</span>
                      <span className="tabular ml-auto shrink-0 text-muted">
                        {acres(s.acres)} ac
                      </span>
                      <span className="tabular w-[42px] shrink-0 text-right text-muted">
                        {pct(share)}
                      </span>
                    </div>
                    {/* A share bar per row, so the long tail stays readable
                        where percentages round to the same 0%. */}
                    <div className="mt-[3px] ml-[17px] h-[2px] rounded-[1px] bg-rule">
                      <div
                        className="h-full rounded-[1px]"
                        style={{
                          width: `${Math.max(share * 100, 0.6)}%`,
                          background: groupForCode(s.crop_code).color,
                        }}
                      />
                    </div>
                  </li>
                )
              })}
            </ul>

            {rows.length > VISIBLE_ROWS && (
              <button
                onClick={() => setExpanded((v) => !v)}
                className="mt-2 w-full rounded-lg border border-rule px-3 py-1.5 text-[11.5px] text-ink-2 transition-colors hover:border-muted hover:text-ink"
              >
                {expanded
                  ? 'Show top 8'
                  : `Show all ${rows.length} — ${hidden} more make up ${pct(hiddenAcres / answered)}`}
              </button>
            )}
          </Section>

          <Section title="Drought" note={aoiDroughtWeek}>
            {droughtSegments.length === 0 ? (
              <p className="text-[12px] text-muted">No drought data for this field.</p>
            ) : (
              <>
                <StackedBar
                  segments={droughtSegments}
                  total={answered}
                  formatValue={(v) => `${acres(v)} ac`}
                />
                <ul className="mt-2 space-y-[3px]">
                  {area.drought.map((d) => {
                    const step = droughtStep(d.drought_class)
                    return (
                      <li
                        key={d.drought_class}
                        className="flex items-center gap-2 text-[12px]"
                      >
                        <span
                          className="inline-block h-[9px] w-[9px] shrink-0 rounded-[2px]"
                          style={{ background: step.color }}
                        />
                        <span className="truncate text-ink-2">{step.label}</span>
                        <span className="tabular ml-auto shrink-0 text-muted">
                          {pct(d.acres / answered)}
                        </span>
                      </li>
                    )
                  })}
                </ul>
              </>
            )}
          </Section>

          <div className="mt-5 border-t border-rule pt-3">
            <div className="flex items-center justify-between text-[11.5px]">
              <span className="text-muted">Agricultural land</span>
              <span className="tabular text-ink-2">{pct(agShare)}</span>
            </div>
            <p className="mt-2.5 text-[10.5px] leading-relaxed text-muted">
              {area.method} Drought is a labelled snapshot of one {aoiDroughtWeek.replace('USDM ', 'USDM ')}, not a live feed.
            </p>
          </div>
        </>
      )}
    </div>
  )
}

function Section({
  title,
  note,
  children,
}: {
  title: string
  note?: string
  children: React.ReactNode
}) {
  return (
    <section className="mt-5 border-t border-rule pt-3.5">
      <div className="mb-1.5 flex items-baseline justify-between">
        <h2 className="text-[10.5px] font-medium tracking-[0.09em] text-muted uppercase">
          {title}
        </h2>
        {note && <span className="text-[10.5px] text-muted">{note}</span>}
      </div>
      {children}
    </section>
  )
}


const SEVERITY: Record<Finding['severity'], string> = {
  neutral: 'var(--color-muted)',
  note: 'var(--color-note)',
  caution: 'var(--color-caution)',
}

/**
 * The server's reading of the field.
 *
 * Placed directly under the acreage because it is the answer; the charts below
 * are the evidence for it. Each finding carries a severity dot, but the dot
 * only ever repeats what the sentence already says -- colour is never the
 * carrier here, which is also why these use the reserved status steps rather
 * than any land cover hue.
 */
function Reading({ insight }: { insight: Insight }) {
  return (
    <section className="mt-4 rounded-xl border border-rule bg-bg/50 px-4 py-3.5">
      <h2 className="text-[13.5px] leading-snug font-semibold text-ink">{insight.headline}</h2>
      <ul className="mt-2.5 space-y-2">
        {insight.findings.map((f, i) => (
          <li key={i} className="flex gap-2.5">
            <span
              className="mt-[6px] inline-block h-[5px] w-[5px] shrink-0 rounded-full"
              style={{ background: SEVERITY[f.severity] }}
              aria-hidden
            />
            <span className="text-[11.5px] leading-relaxed text-ink-2">{f.text}</span>
          </li>
        ))}
      </ul>
      <p className="mt-3 border-t border-rule pt-2 text-[10px] leading-relaxed text-muted">
        Derived by rule from the measurements below — USDA land capability class,
        land cover and the drought snapshot. No model generated this.
      </p>
    </section>
  )
}

/** The measured soil properties the reading rests on. */
function SoilFacts({ soil }: { soil: SoilSummary }) {
  const rows: [string, string][] = []
  if (soil.dominant_drainage) rows.push(['Drainage', soil.dominant_drainage])
  if (soil.slope_pct != null) rows.push(['Mean slope', `${soil.slope_pct.toFixed(1)}%`])
  if (soil.water_storage != null)
    rows.push(['Water storage', `${soil.water_storage.toFixed(1)} cm`])
  if (soil.irrigable_share != null)
    rows.push(['Irrigable', `${(soil.irrigable_share * 100).toFixed(0)}%`])

  return (
    <Section title="Soil">
      <p className="text-[12px] leading-relaxed text-ink-2">{soil.dominant_name}</p>
      {rows.length > 0 && (
        <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5">
          {rows.map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-2">
              <dt className="text-[11px] text-muted">{k}</dt>
              <dd className="tabular text-[11.5px] text-ink-2">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {soil.rated_share < 1 && (
        <p className="mt-2 text-[10.5px] text-muted">
          {((1 - soil.rated_share) * 100).toFixed(0)}% of the answered area carries no
          capability rating.
        </p>
      )}
    </Section>
  )
}
