/**
 * Work Overview shell: header controls, collapsible sidebar, timeline.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ActionIcon, Alert, AppShell, Box, Button, Group, Loader, PasswordInput,
  SegmentedControl, Stack, Text, TextInput, Title, Tooltip,
} from '@mantine/core'
import {
  IconAlertTriangle, IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand, IconRefresh,
} from '@tabler/icons-react'

import { Sidebar } from './components/Sidebar'
import { Timeline } from './components/Timeline'
import { bridge, newBlockId } from './data/bridge'
import { currentSession, fetchCaptures, fetchCollections, initCloud, signIn, signOut } from './data/cloud'
import { buildWorkspace } from './lib/derive'
import { EMPTY_WORKSPACE, type TimelineBlock, type Workspace } from './lib/model'
import { DAY, WEEK } from './lib/time'
import { useViewport } from './state/useViewport'
import { SURFACE } from './theme'

type Interval = 'day' | 'week' | 'all'

/** The clock only needs to move often enough for a running session to read live. */
function useNow(periodMs = 15_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), periodMs)
    return () => clearInterval(id)
  }, [periodMs])
  return now
}

export default function App() {
  const now = useNow()
  const [collapsed, setCollapsed] = useState(false)
  const [interval, setInterval] = useState<Interval>('day')
  const [workspace, setWorkspace] = useState<Workspace>(EMPTY_WORKSPACE)
  const [blocks, setBlocks] = useState<TimelineBlock[]>([])
  const [status, setStatus] = useState<'boot' | 'anon' | 'loading' | 'ready' | 'error'>('boot')
  const [error, setError] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')

  const controller = useViewport({ start: now - DAY, end: now, width: 1200 })
  const { viewport, setWidth, wheel, panPx, animateTo, jumpTo } = controller

  const reloadBlocks = useCallback(async () => {
    const list = await bridge().listBlocks()
    setBlocks(list)
    return list
  }, [])

  const load = useCallback(async (existing?: TimelineBlock[]) => {
    setStatus('loading')
    setError('')
    try {
      const [captures, collections] = await Promise.all([fetchCaptures(null), fetchCollections()])
      setWorkspace(buildWorkspace(captures, collections, existing ?? blocks))
      setStatus('ready')
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
      setStatus('error')
    }
  }, [blocks])

  // Boot: local blocks first so the timeline is usable even signed out.
  useEffect(() => {
    void (async () => {
      const list = await reloadBlocks()
      const configured = await initCloud()
      if (!configured) {
        setError('Cloud is not configured for this build.')
        setStatus('error')
        setWorkspace(buildWorkspace([], [], list))
        return
      }
      const session = await currentSession()
      if (!session) {
        setWorkspace(buildWorkspace([], [], list))
        setStatus('anon')
        return
      }
      await load(list)
    })()
    // Boot runs once; `load` closes over blocks that boot itself supplies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Blocks change often (start/stop); fold them in without refetching the cloud.
  useEffect(() => {
    setWorkspace((current) => ({ ...current, blocks }))
  }, [blocks])

  const applyInterval = useCallback((next: Interval) => {
    setInterval(next)
    if (next === 'day') animateTo(now - DAY, now)
    else if (next === 'week') animateTo(now - WEEK, now)
    else if (workspace.extent) animateTo(workspace.extent.start, workspace.extent.end)
  }, [animateTo, now, workspace.extent])

  const running = useMemo(
    () => blocks.find((b) => b.kind === 'session' && b.end === null) ?? null,
    [blocks],
  )

  const saveBlock = useCallback(async (block: TimelineBlock) => {
    await bridge().saveBlock(block)
    await reloadBlocks()
  }, [reloadBlocks])

  const startSession = useCallback(async (label: string) => {
    if (running) return
    await saveBlock({
      id: newBlockId(), kind: 'session', label, start: Date.now(), end: null,
      createdAt: Date.now(), updatedAt: Date.now(),
    })
  }, [running, saveBlock])

  const stopSession = useCallback(async () => {
    if (!running) return
    await saveBlock({ ...running, end: Date.now(), updatedAt: Date.now() })
  }, [running, saveBlock])

  // A label block marks a span you name after the fact; default it to the last
  // hour so it lands somewhere visible and can be dragged later.
  const addLabel = useCallback(async (label: string) => {
    const end = Date.now()
    await saveBlock({
      id: newBlockId(), kind: 'label', label, start: end - 3_600_000, end,
      createdAt: end, updatedAt: end,
    })
  }, [saveBlock])

  const doSignIn = useCallback(async () => {
    setStatus('loading')
    const message = await signIn(email.trim(), password)
    if (message) {
      setError(message)
      setStatus('anon')
      return
    }
    setPassword('')
    await load()
  }, [email, password, load])

  if (status === 'anon') {
    return (
      <Box style={{ height: '100vh', display: 'grid', placeItems: 'center', background: SURFACE.chrome }}>
        <Stack gap="sm" style={{ width: 320 }}>
          <Title order={2} c="slate.1">Work Overview</Title>
          <Text size="xs" c="slate.4">
            Sign in to read your captures. Sessions you record are stored on this machine
            and stay available signed out.
          </Text>
          {error && <Alert color="red" variant="light" p="xs"><Text size="xs">{error}</Text></Alert>}
          <TextInput
            size="xs" label="Email" value={email}
            onChange={(e) => setEmail(e.currentTarget.value)}
          />
          <PasswordInput
            size="xs" label="Password" value={password}
            onChange={(e) => setPassword(e.currentTarget.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') void doSignIn() }}
          />
          <Button onClick={() => void doSignIn()} disabled={!email.trim() || !password}>
            Sign in
          </Button>
        </Stack>
      </Box>
    )
  }

  return (
    <AppShell
      header={{ height: 46 }}
      navbar={{ width: 268, breakpoint: 'sm', collapsed: { desktop: collapsed, mobile: collapsed } }}
      padding={0}
      style={{ background: SURFACE.chrome }}
    >
      <AppShell.Header style={{ background: SURFACE.panel, borderColor: SURFACE.line }}>
        <Group h="100%" px="sm" justify="space-between" wrap="nowrap">
          <Group gap="xs" wrap="nowrap">
            <Tooltip label={collapsed ? 'Show sidebar' : 'Hide sidebar'}>
              <ActionIcon onClick={() => setCollapsed((c) => !c)}>
                {collapsed
                  ? <IconLayoutSidebarLeftExpand size={17} />
                  : <IconLayoutSidebarLeftCollapse size={17} />}
              </ActionIcon>
            </Tooltip>
            <Text size="sm" fw={600} c="slate.1">Work Overview</Text>
          </Group>

          <Group gap="xs" wrap="nowrap">
            {status === 'loading' && <Loader size="xs" />}
            <SegmentedControl
              size="xs"
              value={interval}
              onChange={(value) => applyInterval(value as Interval)}
              data={[
                { label: 'Day', value: 'day' },
                { label: 'Week', value: 'week' },
                { label: 'Overall', value: 'all' },
              ]}
            />
            <Tooltip label="Reload from cloud">
              <ActionIcon onClick={() => void load()}><IconRefresh size={16} /></ActionIcon>
            </Tooltip>
            <Button variant="subtle" size="compact-xs" onClick={() => void signOut().then(() => setStatus('anon'))}>
              Sign out
            </Button>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar style={{ background: SURFACE.panel, borderColor: SURFACE.line }}>
        <Sidebar
          workspace={workspace}
          viewport={viewport}
          running={running}
          onStart={(label) => void startSession(label)}
          onStop={() => void stopSession()}
          onAddLabel={(label) => void addLabel(label)}
          onFocus={animateTo}
        />
      </AppShell.Navbar>

      <AppShell.Main style={{ height: '100vh', display: 'flex', flexDirection: 'column' }}>
        {error && status === 'error' && (
          <Alert
            color="red" variant="light" p="xs" icon={<IconAlertTriangle size={15} />}
            style={{ borderRadius: 0 }}
          >
            <Text size="xs">{error}</Text>
          </Alert>
        )}
        <Box style={{ flex: '1 1 auto', minHeight: 0 }}>
          <Timeline
            workspace={workspace}
            viewport={viewport}
            now={now}
            onWheel={wheel}
            onPan={panPx}
            onWidth={setWidth}
            onFocus={animateTo}
            onSelectBlock={(block) => jumpTo(block.start, block.end ?? Date.now())}
          />
        </Box>
      </AppShell.Main>
    </AppShell>
  )
}
