/**
 * What the timeline draws.
 *
 * These types are deliberately independent of both the Supabase row shapes and
 * the Electron store: `derive.ts` maps cloud rows onto them, and every view
 * component reads only from here.
 */

export type SubMarkerKind = 'image' | 'voice'

export interface SubMarker {
  id: string
  kind: SubMarkerKind
  /** epoch ms */
  at: number
  /**
   * True when `at` was interpolated from capture order rather than recorded.
   *
   * The phone's photo-asset contract carries `capture_order` but no per-image
   * timestamp, so historic captures can only be placed by rank inside the
   * capture window. Shown differently in the UI: a guess must not read as a
   * measurement.
   */
  approximate: boolean
  order: number
  label: string
}

export interface CaptureEvent {
  id: string
  title: string
  author: string
  /** epoch ms — when the capture happened */
  at: number
  /** epoch ms — end of the capture window, `at` when unknown */
  until: number
  collectionId: string | null
  collectionName: string | null
  contributor: string
  device: string
  status: string
  imageCount: number
  subMarkers: SubMarker[]
  note: string
}

/**
 * A collection's working span: first to last capture filed into it.
 *
 * Drawn as a bracket with a table of its books over it, so it carries the book
 * list rather than making the view re-group captures.
 */
export interface CollectionSpan {
  id: string
  name: string
  start: number
  end: number
  captures: CaptureEvent[]
  /** collections whose captures are interleaved with this one's in time */
  concurrent: boolean
}

export type BlockKind = 'session' | 'label'

export interface TimelineBlock {
  id: string
  kind: BlockKind
  label: string
  start: number
  /** null while still running */
  end: number | null
  color?: string
  note?: string
  createdAt: number
  updatedAt: number
}

export interface Workspace {
  captures: CaptureEvent[]
  collections: CollectionSpan[]
  blocks: TimelineBlock[]
  /** epoch ms bounds of everything present, or null when empty */
  extent: { start: number; end: number } | null
}

export const EMPTY_WORKSPACE: Workspace = {
  captures: [],
  collections: [],
  blocks: [],
  extent: null,
}
