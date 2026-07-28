/**
 * The timeline surface: measurement, gestures, and lane layout.
 *
 * Gestures are attached imperatively rather than through React props because
 * wheel-zoom must call preventDefault, and React attaches `onWheel` passively —
 * a passive listener cannot stop the page from scrolling underneath the zoom.
 */
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Box, Text } from '@mantine/core'
import type { CollectionSpan, TimelineBlock, Workspace } from '../lib/model'
import { type Viewport, detailLevel, msToPx } from '../lib/time'
import { SURFACE } from '../theme'
import { BlockLane } from './BlockLane'
import { CaptureMarks } from './CaptureMarks'
import { CollectionBracket } from './CollectionBracket'
import { TimeAxis } from './TimeAxis'

interface Props {
  workspace: Workspace
  viewport: Viewport
  now: number
  onWheel: (deltaY: number, deltaMode: number, anchorPx: number) => void
  onPan: (dx: number) => void
  onWidth: (width: number) => void
  onFocus: (from: number, to: number) => void
  onSelectBlock: (block: TimelineBlock) => void
}

/**
 * Pack overlapping brackets into rows.
 *
 * Collections worked in parallel must not be drawn on top of each other, and
 * the row a collection lands in should not jump as you pan — so packing is done
 * on the full set in start order, independent of the viewport.
 */
function packRows(spans: CollectionSpan[]): CollectionSpan[][] {
  const rows: CollectionSpan[][] = []
  for (const span of spans) {
    const row = rows.find((candidate) => {
      const last = candidate[candidate.length - 1]
      return last !== undefined && last.end < span.start
    })
    if (row) row.push(span)
    else rows.push([span])
  }
  return rows
}

/** Row height has to cover the tallest thing a bracket can carry at this zoom. */
function rowHeight(detail: ReturnType<typeof detailLevel>): number {
  if (detail === 'full') return 210
  if (detail === 'books') return 118
  return 34
}

export function Timeline({
  workspace, viewport, now, onWheel, onPan, onWidth, onFocus, onSelectBlock,
}: Props) {
  const surface = useRef<HTMLDivElement | null>(null)
  const dragging = useRef<{ pointerId: number; x: number } | null>(null)

  // Width drives every coordinate, so measure the real box rather than assuming
  // it matches the window: the sidebar collapses and this must follow.
  useEffect(() => {
    const node = surface.current
    if (!node) return
    const observer = new ResizeObserver(([entry]) => {
      if (entry) onWidth(entry.contentRect.width)
    })
    observer.observe(node)
    onWidth(node.getBoundingClientRect().width)
    return () => observer.disconnect()
  }, [onWidth])

  useEffect(() => {
    const node = surface.current
    if (!node) return
    const handler = (event: WheelEvent) => {
      event.preventDefault()
      const rect = node.getBoundingClientRect()
      // Shift-wheel pans, matching the convention in editors and DAWs.
      if (event.shiftKey) onPan(-event.deltaY)
      else onWheel(event.deltaY, event.deltaMode, event.clientX - rect.left)
    }
    node.addEventListener('wheel', handler, { passive: false })
    return () => node.removeEventListener('wheel', handler)
  }, [onWheel, onPan])

  const onPointerDown = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    // Let clicks on marks through; only the empty surface drags.
    if (event.button !== 0) return
    dragging.current = { pointerId: event.pointerId, x: event.clientX }
    event.currentTarget.setPointerCapture(event.pointerId)
  }, [])

  const onPointerMove = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    const drag = dragging.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const dx = event.clientX - drag.x
    if (dx === 0) return
    drag.x = event.clientX
    onPan(dx)
  }, [onPan])

  const endDrag = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    if (dragging.current?.pointerId !== event.pointerId) return
    dragging.current = null
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
  }, [])

  const detail = detailLevel(viewport)
  const rows = useMemo(() => packRows(workspace.collections), [workspace.collections])
  const height = rowHeight(detail)
  const nowX = msToPx(viewport, now)

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', height: '100%', minWidth: 0 }}>
      <TimeAxis viewport={viewport} />

      <Box
        ref={surface}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        style={{
          position: 'relative',
          flex: '1 1 auto',
          overflowX: 'hidden',
          overflowY: 'auto',
          cursor: 'grab',
          touchAction: 'none',
          background: SURFACE.chrome,
        }}
      >
        <BlockLane
          blocks={workspace.blocks}
          viewport={viewport}
          now={now}
          onSelect={onSelectBlock}
        />

        {rows.map((row, index) => (
          <Box
            key={index}
            style={{
              position: 'relative',
              height,
              borderBottom: `1px solid ${SURFACE.line}`,
              paddingTop: detail === 'overview' ? 8 : 6,
            }}
          >
            {row.map((span) => (
              <CollectionBracket
                key={span.id}
                span={span}
                viewport={viewport}
                detail={detail}
                onFocus={onFocus}
              />
            ))}
          </Box>
        ))}

        {rows.length === 0 && (
          <Text size="xs" c="slate.5" style={{ padding: 12, userSelect: 'none' }}>
            No collections in this range.
          </Text>
        )}

        <CaptureMarks captures={workspace.captures} viewport={viewport} onFocus={onFocus} />

        {nowX >= 0 && nowX <= viewport.width && (
          <Box
            style={{
              position: 'absolute', top: 0, bottom: 0, left: 0,
              transform: `translate3d(${nowX}px, 0, 0)`,
              width: 1, background: '#e0574f', opacity: 0.55, pointerEvents: 'none',
            }}
          />
        )}
      </Box>
    </Box>
  )
}
