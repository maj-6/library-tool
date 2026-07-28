/**
 * Turn raw `captures` rows into what the timeline draws.
 *
 * The wire contract this reads is set by the Android app
 * (CaptureSession.kt writes `scan_collection_id` / `scan_collection` into
 * `meta`; PhotoAssets.kt writes the `_capture_photo_assets` envelope). Every
 * field is read defensively — these rows are produced by shipped APKs of
 * several vintages, and a missing key must degrade one marker, not blank the
 * whole view.
 */
import type {
  CaptureEvent, CollectionSpan, SubMarker, TimelineBlock, Workspace,
} from './model'

export interface CaptureRow {
  id: string
  created_at: string
  device?: string | null
  status?: string | null
  photos?: unknown
  note?: string | null
  contributor?: string | null
  meta?: Record<string, unknown> | null
}

export interface CollectionRow {
  id: string
  name: string
  deleted?: boolean | null
  merged_into?: string | null
}

/** Voice notes are dictated per book; the capture note is the transcript. */
const NOTE_CAPTURE_RE = /^Captured via phone \(([^)]*)\)\s*(\S+)?/m

function str(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback
}

function parseTime(value: unknown): number | null {
  if (typeof value !== 'string' || !value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

/**
 * The best available capture instant.
 *
 * `created_at` is supplied by the phone at upload, but the note carries the
 * moment the book was actually captured — which can be much earlier when the
 * phone was offline. Prefer the note when both are present and sane.
 */
export function captureInstant(row: CaptureRow): number | null {
  const uploaded = parseTime(row.created_at)
  const noted = parseTime(NOTE_CAPTURE_RE.exec(str(row.note))?.[2])
  if (noted !== null && uploaded !== null) return Math.min(noted, uploaded)
  return noted ?? uploaded
}

function photoCount(row: CaptureRow): number {
  return Array.isArray(row.photos) ? row.photos.length : 0
}

interface AssetLike {
  asset_id?: unknown
  capture_order?: unknown
  captured_at?: unknown
}

/**
 * Per-image sub-markers.
 *
 * `captured_at` is honoured when present — new captures will carry it. Older
 * ones only have `capture_order`, so images are spread evenly across an assumed
 * capture window and flagged `approximate`.
 *
 * The window is derived from the image count rather than fixed, because a
 * 12-image capture genuinely took longer than a 2-image one; ~6 s per image
 * matches the observed dictation cadence in the voice transcripts.
 */
const ASSUMED_MS_PER_IMAGE = 6_000

export function subMarkersFor(row: CaptureRow, at: number): SubMarker[] {
  const meta = row.meta ?? {}
  const envelope = meta['_capture_photo_assets']
  const assets: AssetLike[] =
    envelope && typeof envelope === 'object' && Array.isArray((envelope as { assets?: unknown }).assets)
      ? ((envelope as { assets: AssetLike[] }).assets)
      : []

  const ordered = assets
    .map((asset, index) => ({
      asset,
      order: typeof asset.capture_order === 'number' ? asset.capture_order : index + 1,
    }))
    .sort((a, b) => a.order - b.order)

  const count = ordered.length || photoCount(row)
  if (count === 0) return []

  const markers: SubMarker[] = ordered.map(({ asset, order }, index) => {
    const recorded = parseTime(asset.captured_at)
    return {
      id: str(asset.asset_id) || `${row.id}:img:${order}`,
      kind: 'image' as const,
      at: recorded ?? at + index * ASSUMED_MS_PER_IMAGE,
      approximate: recorded === null,
      order,
      label: `Image ${order}`,
    }
  })

  // A capture whose assets never made it into meta still had photos uploaded;
  // show them rather than pretending the capture had no images.
  if (!markers.length) {
    for (let i = 0; i < count; i += 1) {
      markers.push({
        id: `${row.id}:img:${i + 1}`,
        kind: 'image',
        at: at + i * ASSUMED_MS_PER_IMAGE,
        approximate: true,
        order: i + 1,
        label: `Image ${i + 1}`,
      })
    }
  }

  if (str(row.note).trim()) {
    markers.push({
      id: `${row.id}:voice`,
      kind: 'voice',
      at,
      approximate: false,
      order: 0,
      label: 'Voice note',
    })
  }

  return markers.sort((a, b) => a.at - b.at || a.order - b.order)
}

/** Bibliographic title, however this vintage of the app spelled it. */
function titleOf(meta: Record<string, unknown>, fallback: string): string {
  for (const key of ['title', 'book_title', 'Title']) {
    const value = str(meta[key]).trim()
    if (value) return value
  }
  return fallback
}

export function toCaptureEvent(row: CaptureRow): CaptureEvent | null {
  const at = captureInstant(row)
  if (at === null) return null
  const meta = row.meta ?? {}
  const subMarkers = subMarkersFor(row, at)
  const until = subMarkers.length ? Math.max(at, subMarkers[subMarkers.length - 1]!.at) : at
  return {
    id: row.id,
    title: titleOf(meta, '(untitled capture)'),
    author: str(meta['author']).trim(),
    at,
    until,
    collectionId: str(meta['scan_collection_id']).trim() || null,
    collectionName: str(meta['scan_collection']).trim() || null,
    contributor: str(row.contributor).trim(),
    device: str(row.device).trim(),
    status: str(row.status, 'pending'),
    imageCount: Math.max(photoCount(row), subMarkers.filter((m) => m.kind === 'image').length),
    subMarkers,
    note: str(row.note),
  }
}

/**
 * Group captures into collection spans.
 *
 * A collection is drawn as one bracket from its first to its last capture. Two
 * collections whose spans overlap were genuinely worked in parallel — the view
 * needs to know, because their brackets have to be stacked rather than drawn on
 * one row.
 */
export function collectionSpans(
  captures: CaptureEvent[],
  names: Map<string, string>,
): CollectionSpan[] {
  const groups = new Map<string, CaptureEvent[]>()
  for (const capture of captures) {
    // Fall back to the name when the id is absent: early captures recorded a
    // collection name only, and dropping them would lose real work.
    const key = capture.collectionId ?? (capture.collectionName ? `name:${capture.collectionName}` : null)
    if (!key) continue
    const bucket = groups.get(key)
    if (bucket) bucket.push(capture)
    else groups.set(key, [capture])
  }

  const spans: CollectionSpan[] = []
  for (const [key, members] of groups) {
    members.sort((a, b) => a.at - b.at)
    const first = members[0]!
    spans.push({
      id: key,
      name: names.get(key) ?? first.collectionName ?? 'Untitled collection',
      start: first.at,
      end: Math.max(...members.map((m) => m.until)),
      captures: members,
      concurrent: false,
    })
  }

  spans.sort((a, b) => a.start - b.start || a.end - b.end)
  for (let i = 0; i < spans.length; i += 1) {
    for (let j = i + 1; j < spans.length; j += 1) {
      if (spans[j]!.start > spans[i]!.end) break
      spans[i]!.concurrent = true
      spans[j]!.concurrent = true
    }
  }
  return spans
}

export function buildWorkspace(
  captureRows: CaptureRow[],
  collectionRows: CollectionRow[],
  blocks: TimelineBlock[],
): Workspace {
  const names = new Map<string, string>()
  for (const row of collectionRows) {
    if (row.deleted) continue
    names.set(row.id, row.name)
  }

  const captures = captureRows
    .map(toCaptureEvent)
    .filter((c): c is CaptureEvent => c !== null)
    .sort((a, b) => a.at - b.at)

  const collections = collectionSpans(captures, names)

  const times: number[] = []
  for (const capture of captures) times.push(capture.at, capture.until)
  for (const block of blocks) times.push(block.start, block.end ?? block.start)

  const extent = times.length
    ? { start: Math.min(...times), end: Math.max(...times) }
    : null

  return { captures, collections, blocks, extent }
}
