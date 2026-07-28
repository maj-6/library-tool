/**
 * Hand-authored blocks: work sessions and custom labels.
 *
 * These are the only records on the timeline the user owns outright, so they
 * sit at the top and are the only lane that is directly editable. A running
 * session has no end yet and is drawn open-ended rather than pinned to "now",
 * which would make it look finished the moment you stopped looking.
 */
import { Box, Text, Tooltip } from '@mantine/core'
import type { TimelineBlock } from '../lib/model'
import { type Viewport, formatDuration, msToPx } from '../lib/time'
import { LANE, SURFACE } from '../theme'

interface Props {
  blocks: TimelineBlock[]
  viewport: Viewport
  now: number
  onSelect: (block: TimelineBlock) => void
}

const HEIGHT = 26

export function BlockLane({ blocks, viewport, now, onSelect }: Props) {
  return (
    <Box
      style={{
        position: 'relative',
        height: HEIGHT + 6,
        borderBottom: `1px solid ${SURFACE.line}`,
        flex: '0 0 auto',
      }}
    >
      {blocks.map((block) => {
        const running = block.end === null
        const end = block.end ?? now
        const rawLeft = msToPx(viewport, block.start)
        const rawRight = msToPx(viewport, end)
        if (rawRight < -8 || rawLeft > viewport.width + 8) return null

        const left = Math.max(-4, rawLeft)
        const width = Math.max(3, Math.min(viewport.width + 4, rawRight) - left)
        const color = block.color || (block.kind === 'session' ? LANE.session : LANE.label)
        const duration = formatDuration(end - block.start)

        return (
          <Tooltip
            key={block.id}
            label={`${block.label || (block.kind === 'session' ? 'Session' : 'Block')} · ${duration}${running ? ' · running' : ''}`}
          >
            <Box
              onClick={() => onSelect(block)}
              style={{
                position: 'absolute',
                top: 3,
                left: 0,
                transform: `translate3d(${left}px, 0, 0)`,
                width,
                height: HEIGHT,
                borderRadius: 3,
                cursor: 'pointer',
                background: `${color}22`,
                border: `1px solid ${color}`,
                // An open-ended session fades out at its right edge instead of
                // ending in a hard wall it has not actually reached.
                borderRightStyle: running ? 'dashed' : 'solid',
                display: 'flex',
                alignItems: 'center',
                overflow: 'hidden',
              }}
            >
              {width > 44 && (
                <Text
                  size="xs"
                  fw={500}
                  style={{
                    color,
                    padding: '0 6px',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    userSelect: 'none',
                  }}
                >
                  {block.label || (block.kind === 'session' ? 'Session' : 'Block')}
                  {width > 130 && <span style={{ opacity: 0.65 }}> · {duration}</span>}
                </Text>
              )}
            </Box>
          </Tooltip>
        )
      })}

      {blocks.length === 0 && (
        <Text size="xs" c="slate.5" style={{ padding: '7px 8px', userSelect: 'none' }}>
          No sessions yet — press Start to open one
        </Text>
      )}
    </Box>
  )
}
