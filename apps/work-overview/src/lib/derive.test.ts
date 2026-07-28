import { describe, expect, it } from 'vitest'
import {
  buildWorkspace, captureInstant, collectionSpans, subMarkersFor, toCaptureEvent,
  type CaptureRow,
} from './derive'

const UPLOADED = '2026-07-21T04:00:00Z'
const CAPTURED = '2026-07-20T23:32:49Z'

function row(over: Partial<CaptureRow> = {}): CaptureRow {
  return {
    id: 'cap-1',
    created_at: UPLOADED,
    device: 'TMRV07P5G',
    status: 'pending',
    photos: ['captures/cap-1/photo_1.jpg'],
    note: `Captured via phone (TMRV07P5G) ${CAPTURED}\nContributor: Andrew Miller`,
    contributor: 'Andrew Miller',
    meta: { title: 'Phantastica', author: 'Lewin' },
    ...over,
  }
}

describe('captureInstant', () => {
  it('prefers the dictated capture time over the upload time', () => {
    // The phone can be offline for hours; the upload time is not when the book
    // was captured, and placing it there would misreport the session.
    expect(captureInstant(row())).toBe(Date.parse(CAPTURED))
  })

  it('falls back to created_at when the note carries no timestamp', () => {
    expect(captureInstant(row({ note: 'no marker here' }))).toBe(Date.parse(UPLOADED))
  })

  it('returns null when nothing is parseable, so the row can be skipped', () => {
    expect(captureInstant(row({ created_at: 'not-a-date', note: '' }))).toBeNull()
    expect(toCaptureEvent(row({ created_at: '', note: '' }))).toBeNull()
  })
})

describe('subMarkersFor', () => {
  const at = Date.parse(CAPTURED)

  it('uses recorded per-image times when present and marks them exact', () => {
    const markers = subMarkersFor(row({
      note: '',
      meta: {
        _capture_photo_assets: {
          assets: [
            { asset_id: 'b', capture_order: 2, captured_at: '2026-07-20T23:33:10Z' },
            { asset_id: 'a', capture_order: 1, captured_at: '2026-07-20T23:32:55Z' },
          ],
        },
      },
    }), at)
    expect(markers.map((m) => m.id)).toEqual(['a', 'b'])
    expect(markers.every((m) => !m.approximate)).toBe(true)
  })

  it('interpolates from capture_order when no time was recorded, flagged approximate', () => {
    const markers = subMarkersFor(row({
      note: '',
      meta: { _capture_photo_assets: { assets: [{ capture_order: 1 }, { capture_order: 2 }] } },
    }), at)
    expect(markers).toHaveLength(2)
    expect(markers.every((m) => m.approximate)).toBe(true)
    expect(markers[0]!.at).toBe(at)
    expect(markers[1]!.at).toBeGreaterThan(markers[0]!.at)
  })

  it('still shows markers when meta lost the assets but photos were uploaded', () => {
    const markers = subMarkersFor(row({
      note: '',
      photos: ['a.jpg', 'b.jpg', 'c.jpg'],
      meta: {},
    }), at)
    expect(markers.filter((m) => m.kind === 'image')).toHaveLength(3)
  })

  it('adds one voice marker when the capture carries a note', () => {
    const markers = subMarkersFor(row(), at)
    expect(markers.filter((m) => m.kind === 'voice')).toHaveLength(1)
  })

  it('returns nothing for a capture with neither photos nor a note', () => {
    expect(subMarkersFor(row({ photos: [], note: '', meta: {} }), at)).toEqual([])
  })
})

describe('toCaptureEvent', () => {
  it('reads the collection off the phone wire contract', () => {
    const event = toCaptureEvent(row({
      meta: {
        title: 'Peyote',
        scan_collection_id: 'c-1',
        scan_collection: 'Fungi 3',
      },
    }))!
    expect(event.collectionId).toBe('c-1')
    expect(event.collectionName).toBe('Fungi 3')
    expect(event.title).toBe('Peyote')
  })

  it('degrades to a placeholder title rather than an empty row', () => {
    expect(toCaptureEvent(row({ meta: {} }))!.title).toBe('(untitled capture)')
  })

  it('tolerates a malformed meta blob', () => {
    const event = toCaptureEvent(row({ meta: { _capture_photo_assets: 'nonsense' } }))
    expect(event).not.toBeNull()
    expect(event!.subMarkers.some((m) => m.kind === 'image')).toBe(true)
  })
})

describe('collectionSpans', () => {
  const make = (id: string, name: string, at: number, collectionId: string | null) =>
    toCaptureEvent(row({
      id,
      created_at: new Date(at).toISOString(),
      note: '',
      meta: { title: name, scan_collection_id: collectionId, scan_collection: 'Fungi 3' },
    }))!

  const t0 = Date.parse('2026-07-20T10:00:00Z')

  it('spans first to last capture and keeps its books', () => {
    const spans = collectionSpans(
      [make('a', 'A', t0, 'c1'), make('b', 'B', t0 + 3_600_000, 'c1')],
      new Map([['c1', 'Fungi 3']]),
    )
    expect(spans).toHaveLength(1)
    expect(spans[0]!.name).toBe('Fungi 3')
    expect(spans[0]!.start).toBe(t0)
    expect(spans[0]!.end).toBeGreaterThanOrEqual(t0 + 3_600_000)
    expect(spans[0]!.captures.map((c) => c.id)).toEqual(['a', 'b'])
  })

  it('marks interleaved collections concurrent so they can be stacked', () => {
    const spans = collectionSpans(
      [
        make('a', 'A', t0, 'c1'),
        make('b', 'B', t0 + 60_000, 'c2'),
        make('c', 'C', t0 + 120_000, 'c1'),
      ],
      new Map([['c1', 'One'], ['c2', 'Two']]),
    )
    expect(spans.every((s) => s.concurrent)).toBe(true)
  })

  it('leaves sequential collections unmarked', () => {
    const spans = collectionSpans(
      [make('a', 'A', t0, 'c1'), make('b', 'B', t0 + 86_400_000, 'c2')],
      new Map([['c1', 'One'], ['c2', 'Two']]),
    )
    expect(spans.every((s) => !s.concurrent)).toBe(true)
  })

  it('groups by name when the id is missing, rather than dropping the work', () => {
    const spans = collectionSpans([make('a', 'A', t0, null)], new Map())
    expect(spans).toHaveLength(1)
    expect(spans[0]!.name).toBe('Fungi 3')
  })
})

describe('buildWorkspace', () => {
  it('skips undated rows and reports the extent of what remains', () => {
    const ws = buildWorkspace(
      [row({ id: 'good' }), row({ id: 'bad', created_at: '', note: '' })],
      [{ id: 'c1', name: 'Fungi 3' }],
      [{
        id: 'b1', kind: 'session', label: 'Morning', start: Date.parse(CAPTURED) - 60_000,
        end: null, createdAt: 0, updatedAt: 0,
      }],
    )
    expect(ws.captures.map((c) => c.id)).toEqual(['good'])
    expect(ws.extent).not.toBeNull()
    expect(ws.extent!.start).toBeLessThanOrEqual(Date.parse(CAPTURED) - 60_000)
  })

  it('ignores deleted collections when resolving names', () => {
    const ws = buildWorkspace(
      [row({ meta: { title: 'X', scan_collection_id: 'c1', scan_collection: 'Fallback' } })],
      [{ id: 'c1', name: 'Deleted', deleted: true }],
      [],
    )
    expect(ws.collections[0]!.name).toBe('Fallback')
  })

  it('returns an empty extent for no data instead of NaN bounds', () => {
    expect(buildWorkspace([], [], []).extent).toBeNull()
  })
})
