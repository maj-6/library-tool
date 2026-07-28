/**
 * The time ruler.
 *
 * Ticks come from lib/time so their spacing is chosen once, by the same rule
 * the rest of the view uses. Day boundaries are drawn heavier than intermediate
 * ticks: at hour zoom the only way to keep your bearings is to see where one
 * day ends.
 */
import { Box, Text } from '@mantine/core'
import { DAY, type Viewport, formatTick, msToPx, tickStep, ticks } from '../lib/time'
import { SURFACE } from '../theme'

interface Props {
  viewport: Viewport
  height?: number
}

function isMidnight(ms: number): boolean {
  const d = new Date(ms)
  return d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0
}

export function TimeAxis({ viewport, height = 30 }: Props) {
  const step = tickStep(viewport)
  const marks = ticks(viewport)

  return (
    <Box
      style={{
        position: 'relative',
        height,
        borderBottom: `1px solid ${SURFACE.lineStrong}`,
        background: SURFACE.chrome,
        flex: '0 0 auto',
        overflow: 'hidden',
      }}
    >
      {marks.map((t) => {
        const x = msToPx(viewport, t)
        const major = step < DAY && isMidnight(t)
        return (
          <Box
            key={t}
            style={{
              position: 'absolute',
              left: 0,
              top: 0,
              height: '100%',
              // translate3d keeps tick repositioning on the compositor during
              // a pan instead of forcing layout on every frame
              transform: `translate3d(${x}px, 0, 0)`,
              borderLeft: `1px solid ${major ? SURFACE.lineStrong : SURFACE.line}`,
              paddingLeft: 5,
              pointerEvents: 'none',
              whiteSpace: 'nowrap',
            }}
          >
            <Text
              size="xs"
              c={major ? 'slate.2' : 'slate.4'}
              fw={major ? 600 : 400}
              style={{ lineHeight: `${height}px`, userSelect: 'none' }}
            >
              {formatTick(t, step)}
            </Text>
          </Box>
        )
      })}
    </Box>
  )
}
