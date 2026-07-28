import { describe, expect, it } from 'vitest'
import {
  DAY, HOUR, MAX_SPAN, MIN_SPAN, MINUTE,
  detailLevel, fitTo, formatDuration, msToPx, panByPx, pxToMs,
  showsSubMarkers, spanOf, tickStep, ticks, zoomAt,
} from './time'

const base = new Date('2026-07-20T12:00:00Z').getTime()
const view = { start: base, end: base + HOUR, width: 1200 }

describe('coordinate mapping', () => {
  it('maps edges to edges and round-trips', () => {
    expect(msToPx(view, view.start)).toBe(0)
    expect(msToPx(view, view.end)).toBe(1200)
    expect(pxToMs(view, 600)).toBe(base + HOUR / 2)
    expect(pxToMs(view, msToPx(view, base + 12345))).toBeCloseTo(base + 12345, 6)
  })

  it('survives a zero width instead of dividing by zero', () => {
    const collapsed = { ...view, width: 0 }
    expect(Number.isFinite(msToPx(collapsed, base))).toBe(true)
    expect(Number.isFinite(pxToMs(collapsed, 10))).toBe(true)
  })
})

describe('zoomAt', () => {
  it('holds the instant under the cursor still', () => {
    const anchor = 300
    const before = pxToMs(view, anchor)
    const zoomed = zoomAt(view, 0.5, anchor)
    expect(pxToMs(zoomed, anchor)).toBeCloseTo(before, 6)
  })

  it('holds the anchor still at both edges too', () => {
    for (const anchor of [0, 1200]) {
      const before = pxToMs(view, anchor)
      expect(pxToMs(zoomAt(view, 2, anchor), anchor)).toBeCloseTo(before, 6)
    }
  })

  it('clamps without letting the anchor drift', () => {
    const tiny = zoomAt({ ...view, end: base + MIN_SPAN }, 0.001, 600)
    expect(spanOf(tiny)).toBe(MIN_SPAN)
    // still centred on what was under the cursor
    expect(pxToMs(tiny, 600)).toBeCloseTo(base + MIN_SPAN / 2, 6)

    const huge = zoomAt({ ...view, end: base + MAX_SPAN }, 1000, 600)
    expect(spanOf(huge)).toBe(MAX_SPAN)
  })

  it('ignores an out-of-range anchor rather than inverting the window', () => {
    const out = zoomAt(view, 0.5, -500)
    expect(out.end).toBeGreaterThan(out.start)
  })
})

describe('panByPx', () => {
  it('preserves the span exactly', () => {
    const panned = panByPx(view, 250)
    expect(spanOf(panned)).toBe(spanOf(view))
  })

  it('moves content with the drag direction', () => {
    // dragging right (positive dx) should reveal earlier time
    expect(panByPx(view, 100).start).toBeLessThan(view.start)
  })
})

describe('fitTo', () => {
  it('contains the requested range', () => {
    const fitted = fitTo(view, base, base + 3 * HOUR)
    expect(fitted.start).toBeLessThanOrEqual(base)
    expect(fitted.end).toBeGreaterThanOrEqual(base + 3 * HOUR)
  })

  it('gives a single instant a usable span instead of a zero-width view', () => {
    const fitted = fitTo(view, base, base)
    expect(spanOf(fitted)).toBeGreaterThanOrEqual(MIN_SPAN)
    expect(fitted.start).toBeLessThan(base)
    expect(fitted.end).toBeGreaterThan(base)
  })

  it('clamps an absurd range to the maximum span', () => {
    expect(spanOf(fitTo(view, 0, 100 * 365 * DAY))).toBe(MAX_SPAN)
  })
})

describe('detail level', () => {
  const at = (span: number) => detailLevel({ start: base, end: base + span, width: 1200 })

  it('coarsens as the window widens', () => {
    expect(at(2 * MINUTE)).toBe('full')
    expect(at(2 * HOUR)).toBe('books')
    expect(at(2 * DAY)).toBe('collections')
    expect(at(60 * DAY)).toBe('overview')
  })

  it('depends on width, not span alone', () => {
    const span = 8 * HOUR
    expect(detailLevel({ start: base, end: base + span, width: 400 })).toBe('collections')
    expect(detailLevel({ start: base, end: base + span, width: 2400 })).toBe('books')
  })

  it('gates sub-markers on the same scale it draws them at', () => {
    expect(showsSubMarkers({ start: base, end: base + 2 * MINUTE, width: 1200 })).toBe(true)
    expect(showsSubMarkers({ start: base, end: base + 2 * DAY, width: 1200 })).toBe(false)
  })
})

describe('ticks', () => {
  it('leaves at least the requested spacing', () => {
    for (const span of [MINUTE, HOUR, DAY, 30 * DAY, 365 * DAY]) {
      const v = { start: base, end: base + span, width: 1200 }
      const step = tickStep(v, 110)
      expect(msToPx(v, base + step) - msToPx(v, base)).toBeGreaterThanOrEqual(110)
    }
  })

  it('aligns ticks to the local-midnight grid, not to the epoch', () => {
    // The invariant is the grid, not the hour: a 5-day span picks a 12-hour
    // step, so ticks land at 00:00 and 12:00 — never at 07:23. Anchoring on the
    // epoch instead would misplace every tick in a half-hour-offset timezone.
    const v = { start: base, end: base + 5 * DAY, width: 1200 }
    const step = tickStep(v)
    const midnight = new Date(base)
    midnight.setHours(0, 0, 0, 0)
    for (const t of ticks(v)) {
      expect((t - midnight.getTime()) % step).toBe(0)
    }
  })

  it('lands ticks on local midnight once the step is a whole day', () => {
    const v = { start: base, end: base + 40 * DAY, width: 1200 }
    expect(tickStep(v)).toBeGreaterThanOrEqual(DAY)
    for (const t of ticks(v)) {
      const d = new Date(t)
      expect([d.getHours(), d.getMinutes(), d.getSeconds()]).toEqual([0, 0, 0])
    }
  })

  it('stays inside the viewport and terminates on a degenerate window', () => {
    const v = { start: base, end: base + DAY, width: 1200 }
    for (const t of ticks(v)) {
      expect(t).toBeGreaterThanOrEqual(v.start)
      expect(t).toBeLessThanOrEqual(v.end)
    }
    expect(ticks({ start: base, end: base, width: 1200 }).length).toBeLessThanOrEqual(512)
  })
})

describe('formatDuration', () => {
  it('reads compactly at each magnitude', () => {
    expect(formatDuration(12_000)).toBe('12s')
    expect(formatDuration(3 * MINUTE + 4000)).toBe('3m 04s')
    expect(formatDuration(HOUR + 12 * MINUTE)).toBe('1h 12m')
  })

  it('does not render nonsense for bad input', () => {
    expect(formatDuration(Number.NaN)).toBe('—')
    expect(formatDuration(-1)).toBe('—')
  })
})
