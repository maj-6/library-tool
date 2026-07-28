/**
 * Timeline viewport maths: the mapping between milliseconds and pixels, the
 * zoom/pan operations over it, and the detail level derived from it.
 *
 * All of it is pure so the feel of the timeline can be unit-tested rather than
 * eyeballed. Nothing here touches React or the DOM.
 */

export const MINUTE = 60_000
export const HOUR = 60 * MINUTE
export const DAY = 24 * HOUR
export const WEEK = 7 * DAY

/** The visible time window and the pixel width it is drawn into. */
export interface Viewport {
  /** epoch ms at the left edge */
  start: number
  /** epoch ms at the right edge */
  end: number
  /** drawing width in CSS pixels */
  width: number
}

/**
 * Zoom limits, expressed as the visible span.
 *
 * The floor is 30 s across the whole width: below that, sub-markers a few
 * seconds apart stop being distinguishable by position and the view is just
 * jitter. The ceiling is 10 years, which is far beyond any real archive but
 * stops a wheel-spin from sending the domain to infinity.
 */
export const MIN_SPAN = 30_000
export const MAX_SPAN = 10 * 365 * DAY

export function spanOf(v: Viewport): number {
  return v.end - v.start
}

export function msPerPx(v: Viewport): number {
  return spanOf(v) / Math.max(1, v.width)
}

export function msToPx(v: Viewport, ms: number): number {
  return ((ms - v.start) / Math.max(1, spanOf(v))) * v.width
}

export function pxToMs(v: Viewport, px: number): number {
  return v.start + (px / Math.max(1, v.width)) * spanOf(v)
}

function clampSpan(span: number): number {
  return Math.min(MAX_SPAN, Math.max(MIN_SPAN, span))
}

/**
 * Zoom by `factor` while holding the instant under `anchorPx` still.
 *
 * Anchoring on the cursor is what makes wheel-zoom feel like the content is
 * being pulled rather than the window jumping. Clamping the span *before*
 * recomputing the edges keeps the anchor exact at the limits too, instead of
 * letting the view drift once it saturates.
 */
export function zoomAt(v: Viewport, factor: number, anchorPx: number): Viewport {
  const span = spanOf(v)
  const next = clampSpan(span * factor)
  const ratio = Math.min(1, Math.max(0, anchorPx / Math.max(1, v.width)))
  const anchorMs = v.start + ratio * span
  return { ...v, start: anchorMs - ratio * next, end: anchorMs + (1 - ratio) * next }
}

/** Slide the window by a pixel delta, preserving the span exactly. */
export function panByPx(v: Viewport, dx: number): Viewport {
  const dt = dx * msPerPx(v)
  return { ...v, start: v.start - dt, end: v.end - dt }
}

/**
 * Fit a window to [from, to] with breathing room.
 *
 * A zero-width range (one instant, or one book) would divide by zero and blank
 * the view, so it is given a default span centred on the instant.
 */
export function fitTo(v: Viewport, from: number, to: number, padFraction = 0.04): Viewport {
  if (!Number.isFinite(from) || !Number.isFinite(to)) return v
  const raw = to - from
  if (raw <= 0) {
    const span = clampSpan(HOUR)
    const mid = Number.isFinite(from) ? from : Date.now()
    return { ...v, start: mid - span / 2, end: mid + span / 2 }
  }
  const pad = raw * padFraction
  const span = clampSpan(raw + pad * 2)
  const mid = (from + to) / 2
  return { ...v, start: mid - span / 2, end: mid + span / 2 }
}

/**
 * How much detail the timeline should draw.
 *
 * Driven by ms-per-pixel rather than by the span, because that is what actually
 * decides whether two marks can be told apart on screen — it stays correct when
 * the sidebar collapses and the drawing width changes underneath.
 */
export type DetailLevel = 'overview' | 'collections' | 'books' | 'full'

/**
 * Scale boundaries, in ms per pixel.
 *
 * Derived from what has to be separable on screen, not from round time units:
 *   FULL   — images are dictated roughly 6 s apart, so 2 s/px leaves them ~3 px
 *            apart, the least that reads as distinct marks.
 *   BOOKS  — a book title needs ~100 px, and books land ~1-2 min apart.
 *   COLLECTIONS — beyond ~10 min/px a day is 144 px, so individual books are
 *            hopeless but a collection's bracket still has real width.
 * Above COLLECTIONS only aggregate shapes survive.
 */
export const SCALE_FULL = 2_000
export const SCALE_BOOKS = 20_000
export const SCALE_COLLECTIONS = 10 * MINUTE

export function detailLevel(v: Viewport): DetailLevel {
  const scale = msPerPx(v)
  if (scale > SCALE_COLLECTIONS) return 'overview'
  if (scale > SCALE_BOOKS) return 'collections'
  if (scale > SCALE_FULL) return 'books'
  return 'full'
}

/** Sub-markers are only legible once single images are more than a hair apart. */
export function showsSubMarkers(v: Viewport): boolean {
  return msPerPx(v) <= SCALE_FULL
}

export function showsBookTitles(v: Viewport): boolean {
  return msPerPx(v) <= SCALE_BOOKS
}

/**
 * Tick spacing for the time axis: the smallest step from a human-friendly
 * ladder that still leaves at least `minPx` between ticks. Arbitrary "nice
 * number" rounding would produce 2.5-hour ticks, which nobody reads.
 */
const TICK_STEPS = [
  1_000, 5_000, 15_000, 30_000,
  MINUTE, 2 * MINUTE, 5 * MINUTE, 15 * MINUTE, 30 * MINUTE,
  HOUR, 2 * HOUR, 3 * HOUR, 6 * HOUR, 12 * HOUR,
  DAY, 2 * DAY, WEEK, 2 * WEEK, 30 * DAY, 90 * DAY, 180 * DAY, 365 * DAY,
]

export function tickStep(v: Viewport, minPx = 110): number {
  const wanted = msPerPx(v) * minPx
  return TICK_STEPS.find((step) => step >= wanted) ?? TICK_STEPS[TICK_STEPS.length - 1]!
}

/**
 * Tick instants covering the viewport, aligned to local midnight rather than to
 * the epoch. Epoch alignment puts "daily" ticks at the wrong time of day for
 * any timezone with a non-whole-hour offset.
 */
export function ticks(v: Viewport, minPx = 110): number[] {
  const step = tickStep(v, minPx)
  const anchor = new Date(v.start)
  anchor.setHours(0, 0, 0, 0)
  const base = anchor.getTime()
  const first = base + Math.floor((v.start - base) / step) * step
  const out: number[] = []
  // Bounded so a degenerate viewport cannot spin here forever.
  for (let t = first; t <= v.end && out.length < 512; t += step) {
    if (t >= v.start) out.push(t)
  }
  return out
}

export function formatTick(ms: number, step: number): string {
  const d = new Date(ms)
  if (step >= 30 * DAY) return d.toLocaleDateString(undefined, { month: 'short', year: 'numeric' })
  if (step >= DAY) return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
  if (step >= HOUR) {
    const midnight = d.getHours() === 0 && d.getMinutes() === 0
    return midnight
      ? d.toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
      : d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  }
  if (step >= MINUTE) return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })
}

/** "1h 12m", "3m 04s", "12s" — compact enough for a chip. */
export function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const s = Math.round(ms / 1000)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`
  if (m > 0) return `${m}m ${String(sec).padStart(2, '0')}s`
  return `${sec}s`
}
