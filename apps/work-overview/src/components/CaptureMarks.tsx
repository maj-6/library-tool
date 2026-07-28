/**
 * Capture marks and their per-image / per-voice sub-markers.
 *
 * Sub-markers only appear once the zoom makes them distinguishable — below that
 * they would pile into an unreadable smear and imply precision the data does
 * not have. Approximate positions (interpolated from capture_order, because the
 * phone recorded no per-image time) are drawn hollow; recorded ones are solid.
 * That distinction is the whole point of showing them.
 */
import { Box, Text, Tooltip } from '@mantine/core'
import type { CaptureEvent } from '../lib/model'
import { type Viewport, msToPx, showsBookTitles, showsSubMarkers } from '../lib/time'
import { LANE, SURFACE } from '../theme'

interface Props {
  captures: CaptureEvent[]
  viewport: Viewport
  onFocus: (from: number, to: number) => void
}

const STEM_H = 16
const SUB_Y = 20

/** Cull off-screen work before building any DOM for it. */
function visibleCaptures(captures: CaptureEvent[], viewport: Viewport): CaptureEvent[] {
  const pad = (viewport.end - viewport.start) * 0.1
  return captures.filter((c) => c.until >= viewport.start - pad && c.at <= viewport.end + pad)
}

export function CaptureMarks({ captures, viewport, onFocus }: Props) {
  const visible = visibleCaptures(captures, viewport)
  const withSubs = showsSubMarkers(viewport)
  const withTitles = showsBookTitles(viewport)

  return (
    <Box style={{ position: 'relative', height: withSubs ? 46 : 28 }}>
      {visible.map((capture) => {
        const x = msToPx(viewport, capture.at)
        if (x < -200 || x > viewport.width + 200) return null

        return (
          <Box
            key={capture.id}
            style={{
              position: 'absolute', left: 0, top: 0,
              transform: `translate3d(${x}px, 0, 0)`,
            }}
          >
            <Tooltip
              label={
                <Box>
                  <Text size="xs" fw={600}>{capture.title}</Text>
                  {capture.author && <Text size="xs" c="dimmed">{capture.author}</Text>}
                  <Text size="xs" c="dimmed">
                    {new Date(capture.at).toLocaleString()} · {capture.imageCount} image
                    {capture.imageCount === 1 ? '' : 's'}
                  </Text>
                  {capture.collectionName && (
                    <Text size="xs" c="dimmed">{capture.collectionName}</Text>
                  )}
                </Box>
              }
            >
              <Box
                onDoubleClick={() => onFocus(capture.at, capture.until)}
                style={{
                  position: 'absolute', left: -3, top: 0, width: 6, height: STEM_H,
                  cursor: 'zoom-in',
                }}
              >
                <Box
                  style={{
                    position: 'absolute', left: 2, top: 0, width: 2, height: STEM_H,
                    background: LANE.capture, borderRadius: 1,
                  }}
                />
              </Box>
            </Tooltip>

            {withTitles && (
              <Text
                size="xs"
                c="slate.3"
                style={{
                  position: 'absolute', left: 6, top: 1, whiteSpace: 'nowrap',
                  maxWidth: 190, overflow: 'hidden', textOverflow: 'ellipsis',
                  pointerEvents: 'none', userSelect: 'none',
                }}
              >
                {capture.title}
              </Text>
            )}

            {withSubs &&
              capture.subMarkers.map((mark) => {
                const dx = msToPx(viewport, mark.at) - x
                // Sub-markers belonging to a capture that has scrolled away are
                // not worth the nodes.
                if (x + dx < -40 || x + dx > viewport.width + 40) return null
                const color = mark.kind === 'voice' ? LANE.voice : LANE.image
                return (
                  <Tooltip
                    key={mark.id}
                    label={
                      mark.approximate
                        ? `${mark.label} · position approximate (no time recorded)`
                        : `${mark.label} · ${new Date(mark.at).toLocaleTimeString()}`
                    }
                  >
                    <Box
                      style={{
                        position: 'absolute',
                        left: dx - 3,
                        top: SUB_Y,
                        width: 6,
                        height: 6,
                        borderRadius: mark.kind === 'voice' ? 1 : 3,
                        // hollow = inferred, solid = recorded
                        background: mark.approximate ? 'transparent' : color,
                        border: `1px solid ${mark.approximate ? LANE.approximate : color}`,
                      }}
                    />
                  </Tooltip>
                )
              })}
          </Box>
        )
      })}

      {visible.length === 0 && (
        <Text
          size="xs"
          c="slate.5"
          style={{ position: 'absolute', left: 8, top: 4, userSelect: 'none' }}
        >
          No captures in view
        </Text>
      )}

      <Box
        style={{
          position: 'absolute', left: 0, right: 0, bottom: 0, height: 1,
          background: SURFACE.line,
        }}
      />
    </Box>
  )
}
