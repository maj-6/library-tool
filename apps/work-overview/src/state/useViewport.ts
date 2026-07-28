/**
 * Viewport state for the timeline: wheel zoom, drag pan, and animated moves.
 *
 * Two things make this feel smooth rather than steppy:
 *   - zoom is exponential in wheel delta, so a trackpad pinch and a mouse notch
 *     both feel proportional rather than one being unusably fast;
 *   - programmatic moves (fit to a session, switch interval) are eased over
 *     time rather than snapped, and any user gesture cancels the animation
 *     immediately instead of fighting it.
 *
 * Drag panning writes straight through without animation — interposing easing
 * between the pointer and the content is what makes a timeline feel laggy.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  type Viewport, fitTo, panByPx, spanOf, zoomAt,
} from '../lib/time'

/** Wheel notches vary wildly by device; normalise before exponentiating. */
const ZOOM_PER_LINE = 0.0016
const ANIMATION_MS = 260

function easeOutCubic(t: number): number {
  return 1 - Math.pow(1 - t, 3)
}

function sameView(a: Viewport, b: Viewport): boolean {
  return a.start === b.start && a.end === b.end && a.width === b.width
}

export interface ViewportController {
  viewport: Viewport
  setWidth: (width: number) => void
  /** Zoom about a pixel position, e.g. the cursor. */
  zoom: (factor: number, anchorPx: number) => void
  /** Raw wheel handling: normalises deltaMode and applies exponential zoom. */
  wheel: (deltaY: number, deltaMode: number, anchorPx: number) => void
  panPx: (dx: number) => void
  /** Animated move to a range. */
  animateTo: (from: number, to: number) => void
  /** Immediate, unanimated move to a range. */
  jumpTo: (from: number, to: number) => void
  isAnimating: boolean
}

export function useViewport(initial: Viewport): ViewportController {
  const [viewport, setViewport] = useState<Viewport>(initial)
  const [isAnimating, setAnimating] = useState(false)
  const frame = useRef<number | null>(null)
  const latest = useRef(viewport)
  latest.current = viewport

  const cancel = useCallback(() => {
    if (frame.current !== null) {
      cancelAnimationFrame(frame.current)
      frame.current = null
      setAnimating(false)
    }
  }, [])

  useEffect(() => cancel, [cancel])

  const setWidth = useCallback((width: number) => {
    if (!Number.isFinite(width) || width <= 0) return
    setViewport((v) => (v.width === width ? v : { ...v, width }))
  }, [])

  // A user gesture always wins: cancel any in-flight animation first, so the
  // view does not spring back to a destination the user has moved away from.
  const zoom = useCallback((factor: number, anchorPx: number) => {
    cancel()
    setViewport((v) => zoomAt(v, factor, anchorPx))
  }, [cancel])

  const wheel = useCallback((deltaY: number, deltaMode: number, anchorPx: number) => {
    // deltaMode: 0 = pixels, 1 = lines, 2 = pages. Lines and pages arrive as
    // small integers and would otherwise zoom imperceptibly.
    const pixels = deltaMode === 1 ? deltaY * 16 : deltaMode === 2 ? deltaY * 400 : deltaY
    if (!Number.isFinite(pixels) || pixels === 0) return
    cancel()
    const factor = Math.exp(pixels * ZOOM_PER_LINE)
    setViewport((v) => zoomAt(v, factor, anchorPx))
  }, [cancel])

  const panPx = useCallback((dx: number) => {
    if (!Number.isFinite(dx) || dx === 0) return
    cancel()
    setViewport((v) => panByPx(v, dx))
  }, [cancel])

  const jumpTo = useCallback((from: number, to: number) => {
    cancel()
    setViewport((v) => fitTo(v, from, to))
  }, [cancel])

  const animateTo = useCallback((from: number, to: number) => {
    cancel()
    const start = latest.current
    const target = fitTo(start, from, to)
    if (sameView(start, target)) return

    // Interpolate the span geometrically and the centre linearly. Lerping the
    // edges directly makes a large zoom change accelerate wrongly — it looks
    // like the view lurches and then crawls.
    const startSpan = spanOf(start)
    const targetSpan = spanOf(target)
    const startMid = start.start + startSpan / 2
    const targetMid = target.start + targetSpan / 2
    const begun = performance.now()

    setAnimating(true)
    const step = (now: number) => {
      const t = Math.min(1, (now - begun) / ANIMATION_MS)
      const eased = easeOutCubic(t)
      const span = startSpan * Math.pow(targetSpan / startSpan, eased)
      const mid = startMid + (targetMid - startMid) * eased
      setViewport((v) => ({ ...v, start: mid - span / 2, end: mid + span / 2 }))
      if (t < 1) {
        frame.current = requestAnimationFrame(step)
      } else {
        frame.current = null
        setAnimating(false)
      }
    }
    frame.current = requestAnimationFrame(step)
  }, [cancel])

  return { viewport, setWidth, zoom, wheel, panPx, animateTo, jumpTo, isAnimating }
}
