/**
 * Statistics and session controls.
 *
 * Every figure is scoped to the visible time range, not to all history, so the
 * panel answers "what happened in what I'm looking at" as you pan and zoom.
 */
import { Badge, Box, Button, Divider, Group, Stack, Text, TextInput, Tooltip } from '@mantine/core'
import { IconPlayerPlay, IconPlayerStop, IconPlus } from '@tabler/icons-react'
import { useMemo, useState } from 'react'
import type { TimelineBlock, Workspace } from '../lib/model'
import { type Viewport, formatDuration } from '../lib/time'
import { LANE, SURFACE } from '../theme'

interface Props {
  workspace: Workspace
  viewport: Viewport
  running: TimelineBlock | null
  onStart: (label: string) => void
  onStop: () => void
  onAddLabel: (label: string) => void
  onFocus: (from: number, to: number) => void
}

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Box>
      <Text size="xs" c="slate.4" style={{ letterSpacing: 0.3, textTransform: 'uppercase' }}>
        {label}
      </Text>
      <Tooltip label={hint} disabled={!hint}>
        <Text size="lg" fw={600} c="slate.0" style={{ lineHeight: 1.25 }}>
          {value}
        </Text>
      </Tooltip>
    </Box>
  )
}

export function Sidebar({
  workspace, viewport, running, onStart, onStop, onAddLabel, onFocus,
}: Props) {
  const [label, setLabel] = useState('')

  const stats = useMemo(() => {
    const inRange = workspace.captures.filter(
      (c) => c.until >= viewport.start && c.at <= viewport.end,
    )
    const collections = new Set(
      inRange.map((c) => c.collectionId ?? c.collectionName).filter(Boolean),
    )
    const images = inRange.reduce((sum, c) => sum + c.imageCount, 0)
    const voice = inRange.reduce(
      (sum, c) => sum + c.subMarkers.filter((m) => m.kind === 'voice').length, 0,
    )

    // Only count the part of a session that falls inside the view, so the
    // figure matches the range rather than the session's full length.
    let tracked = 0
    for (const block of workspace.blocks) {
      const end = block.end ?? Date.now()
      const from = Math.max(block.start, viewport.start)
      const to = Math.min(end, viewport.end)
      if (to > from) tracked += to - from
    }

    const span = viewport.end - viewport.start
    const perHour = tracked > 0
      ? (inRange.length / (tracked / 3_600_000))
      : span > 0 ? inRange.length / (span / 3_600_000) : 0

    return {
      books: inRange.length,
      collections: collections.size,
      images,
      voice,
      tracked,
      perHour,
      approximate: inRange.some((c) => c.subMarkers.some((m) => m.approximate)),
    }
  }, [workspace, viewport])

  return (
    <Stack gap="md" p="md" style={{ height: '100%', overflowY: 'auto' }}>
      <Box>
        <Group justify="space-between" mb={6}>
          <Text size="xs" fw={600} c="slate.2" tt="uppercase" style={{ letterSpacing: 0.5 }}>
            Session
          </Text>
          {running && (
            <Badge size="xs" variant="light" color="blue">
              running
            </Badge>
          )}
        </Group>

        {running ? (
          <Stack gap={6}>
            <Text size="sm" c="slate.1">{running.label || 'Untitled session'}</Text>
            <Text size="xs" c="slate.4">
              {formatDuration(Date.now() - running.start)} elapsed
            </Text>
            <Button
              leftSection={<IconPlayerStop size={14} />}
              color="red"
              variant="light"
              onClick={onStop}
            >
              Stop session
            </Button>
          </Stack>
        ) : (
          <Stack gap={6}>
            <TextInput
              size="xs"
              placeholder="What are you working on?"
              value={label}
              onChange={(e) => setLabel(e.currentTarget.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  onStart(label.trim())
                  setLabel('')
                }
              }}
            />
            <Group gap={6} grow>
              <Button
                leftSection={<IconPlayerPlay size={14} />}
                onClick={() => { onStart(label.trim()); setLabel('') }}
              >
                Start
              </Button>
              <Button
                variant="default"
                leftSection={<IconPlus size={14} />}
                disabled={!label.trim()}
                onClick={() => { onAddLabel(label.trim()); setLabel('') }}
              >
                Block
              </Button>
            </Group>
          </Stack>
        )}
      </Box>

      <Divider color={SURFACE.line} />

      <Box>
        <Text size="xs" fw={600} c="slate.2" tt="uppercase" mb={8} style={{ letterSpacing: 0.5 }}>
          In view
        </Text>
        <Stack gap="sm">
          <Group grow align="flex-start">
            <Stat label="Books" value={String(stats.books)} />
            <Stat label="Collections" value={String(stats.collections)} />
          </Group>
          <Group grow align="flex-start">
            <Stat
              label="Images"
              value={String(stats.images)}
              hint={stats.approximate ? 'Some image times are inferred from capture order' : undefined}
            />
            <Stat label="Voice notes" value={String(stats.voice)} />
          </Group>
          <Group grow align="flex-start">
            <Stat label="Tracked" value={formatDuration(stats.tracked)} />
            <Stat
              label="Books/hr"
              value={stats.perHour > 0 ? stats.perHour.toFixed(1) : '—'}
              hint={stats.tracked > 0
                ? 'Against tracked session time in view'
                : 'No session in view — measured against elapsed time'}
            />
          </Group>
        </Stack>
      </Box>

      <Divider color={SURFACE.line} />

      <Box>
        <Text size="xs" fw={600} c="slate.2" tt="uppercase" mb={8} style={{ letterSpacing: 0.5 }}>
          Collections in view
        </Text>
        <Stack gap={3}>
          {workspace.collections
            .filter((s) => s.end >= viewport.start && s.start <= viewport.end)
            .slice(0, 24)
            .map((span) => (
              <Group
                key={span.id}
                justify="space-between"
                wrap="nowrap"
                onClick={() => onFocus(span.start, span.end)}
                style={{ cursor: 'pointer', padding: '2px 4px', borderRadius: 3 }}
              >
                <Group gap={6} wrap="nowrap" style={{ minWidth: 0 }}>
                  <Box style={{ width: 6, height: 6, borderRadius: 1, background: LANE.collection, flex: '0 0 auto' }} />
                  <Text size="xs" c="slate.2" truncate>{span.name}</Text>
                </Group>
                <Text size="xs" c="slate.5" style={{ flex: '0 0 auto' }}>{span.captures.length}</Text>
              </Group>
            ))}
          {workspace.collections.length === 0 && (
            <Text size="xs" c="slate.5">Nothing in range.</Text>
          )}
        </Stack>
      </Box>
    </Stack>
  )
}
