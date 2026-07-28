/**
 * A collection drawn as a bracket spanning its working period, with a compact
 * table of the books added to it sitting over the bracket.
 *
 * The table is clipped to the bracket's on-screen width rather than to the
 * collection's true extent, so a collection that runs off the edge of the
 * viewport still shows its books next to the part you can see. How much the
 * table shows is decided by the detail level, not by the row count — the point
 * is that the same collection reads consistently at a given zoom.
 */
import { Box, Table, Text, Tooltip } from '@mantine/core'
import type { CollectionSpan } from '../lib/model'
import { type DetailLevel, type Viewport, formatDuration, msToPx } from '../lib/time'
import { LANE, SURFACE } from '../theme'

interface Props {
  span: CollectionSpan
  viewport: Viewport
  detail: DetailLevel
  onFocus: (from: number, to: number) => void
}

/** Bracket arms are this tall; the table sits directly above them. */
const ARM = 7
const BRACKET_H = 14

export function CollectionBracket({ span, viewport, detail, onFocus }: Props) {
  const rawLeft = msToPx(viewport, span.start)
  const rawRight = msToPx(viewport, span.end)

  // Clamp to the viewport so a long-running collection keeps its label and
  // table on screen instead of scrolling them off with the true start.
  const left = Math.max(-2, rawLeft)
  const right = Math.min(viewport.width + 2, rawRight)
  const width = Math.max(2, right - left)
  if (right < 0 || left > viewport.width) return null

  const books = span.captures
  const showTable = detail === 'books' || detail === 'full'
  const showName = detail !== 'overview'
  // ~22 px a row, and never more than fits the width sensibly.
  const rowLimit = detail === 'full' ? 14 : 6
  const visible = showTable && width > 90 ? books.slice(0, rowLimit) : []
  const hidden = books.length - visible.length

  const label = `${span.name} · ${books.length} book${books.length === 1 ? '' : 's'}`

  return (
    <Box
      style={{ position: 'absolute', left: 0, top: 0, transform: `translate3d(${left}px, 0, 0)` }}
    >
      <Box style={{ width, cursor: 'zoom-in' }} onDoubleClick={() => onFocus(span.start, span.end)}>
        {visible.length > 0 && (
          <Box
            style={{
              marginBottom: 3,
              border: `1px solid ${SURFACE.line}`,
              borderRadius: 3,
              background: SURFACE.raised,
              overflow: 'hidden',
            }}
          >
            <Table
              withRowBorders={false}
              verticalSpacing={1}
              horizontalSpacing={6}
              style={{ tableLayout: 'fixed', fontSize: 11 }}
            >
              <Table.Tbody>
                {visible.map((book) => (
                  <Table.Tr key={book.id}>
                    <Table.Td
                      style={{
                        whiteSpace: 'nowrap',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        color: '#aab2bd',
                        padding: '1px 6px',
                      }}
                    >
                      <Tooltip label={book.title} disabled={book.title.length < 28}>
                        <span>{book.title}</span>
                      </Tooltip>
                    </Table.Td>
                    {detail === 'full' && width > 260 && (
                      <Table.Td
                        style={{ width: 46, textAlign: 'right', color: '#71798a', padding: '1px 6px' }}
                      >
                        {book.imageCount || '—'}
                      </Table.Td>
                    )}
                  </Table.Tr>
                ))}
                {hidden > 0 && (
                  <Table.Tr>
                    <Table.Td
                      colSpan={2}
                      style={{ color: '#586070', fontStyle: 'italic', padding: '1px 6px' }}
                    >
                      +{hidden} more
                    </Table.Td>
                  </Table.Tr>
                )}
              </Table.Tbody>
            </Table>
          </Box>
        )}

        {/* The bracket: a spanning rule with arms turned down at each end. */}
        <Box style={{ position: 'relative', height: BRACKET_H }}>
          <Box
            style={{
              position: 'absolute', left: 0, right: 0, top: 0, height: 2,
              background: LANE.collection, borderRadius: 1,
              // A collection clipped by the viewport should not look like it
              // genuinely ended there.
              opacity: rawLeft < 0 || rawRight > viewport.width ? 0.7 : 1,
            }}
          />
          {rawLeft >= -2 && (
            <Box style={{ position: 'absolute', left: 0, top: 0, width: 2, height: ARM, background: LANE.collection }} />
          )}
          {rawRight <= viewport.width + 2 && (
            <Box style={{ position: 'absolute', right: 0, top: 0, width: 2, height: ARM, background: LANE.collection }} />
          )}
          {showName && width > 54 && (
            <Text
              size="xs"
              fw={600}
              style={{
                position: 'absolute', left: 5, top: ARM - 2, color: LANE.collection,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                maxWidth: width - 8, userSelect: 'none', pointerEvents: 'none',
              }}
            >
              {label}
            </Text>
          )}
        </Box>
      </Box>

      {/* Kept out of the flow: a title-attribute tooltip on the bracket would
          fight the table's own tooltips. */}
      {!showName && width > 8 && (
        <Tooltip label={`${label} · ${formatDuration(span.end - span.start)}`}>
          <Box style={{ position: 'absolute', inset: 0, width }} />
        </Tooltip>
      )}
    </Box>
  )
}
