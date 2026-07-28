/**
 * Durable store for the blocks the user authors by hand: work sessions and
 * custom-labelled spans.
 *
 * Everything else on the timeline is derived from catalogue data and is
 * therefore re-derivable; these blocks are the only records that exist nowhere
 * else, so writes are atomic (temp file + rename) and a corrupt file is moved
 * aside rather than silently overwritten.
 */
import fs from 'node:fs'
import path from 'node:path'

export type BlockKind = 'session' | 'label'

export interface TimelineBlock {
  id: string
  kind: BlockKind
  label: string
  /** epoch ms */
  start: number
  /** epoch ms; null while a session is still running */
  end: number | null
  color?: string
  note?: string
  createdAt: number
  updatedAt: number
}

interface StoreDocument {
  version: 1
  blocks: TimelineBlock[]
}

const EMPTY: StoreDocument = { version: 1, blocks: [] }

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

/** Reject anything that would render as a broken block rather than storing it. */
export function normalizeBlock(input: unknown): TimelineBlock | null {
  if (!input || typeof input !== 'object') return null
  const raw = input as Record<string, unknown>
  const id = typeof raw.id === 'string' && raw.id.trim() ? raw.id.trim() : null
  const kind = raw.kind === 'session' || raw.kind === 'label' ? raw.kind : null
  if (!id || !kind || !isFiniteNumber(raw.start)) return null

  // A null end means "still running"; a present end must be after the start,
  // because a negative-width block is unclickable and unexplainable.
  const end = raw.end === null || raw.end === undefined ? null : raw.end
  if (end !== null && (!isFiniteNumber(end) || end <= raw.start)) return null

  const now = Date.now()
  return {
    id,
    kind,
    label: typeof raw.label === 'string' ? raw.label.slice(0, 200) : '',
    start: raw.start,
    end,
    color: typeof raw.color === 'string' ? raw.color.slice(0, 32) : undefined,
    note: typeof raw.note === 'string' ? raw.note.slice(0, 2000) : undefined,
    createdAt: isFiniteNumber(raw.createdAt) ? raw.createdAt : now,
    updatedAt: now,
  }
}

export class SessionStore {
  private readonly file: string
  private cache: StoreDocument | null = null

  constructor(file: string) {
    this.file = file
  }

  private read(): StoreDocument {
    if (this.cache) return this.cache
    try {
      const text = fs.readFileSync(this.file, 'utf8')
      const parsed = JSON.parse(text) as Partial<StoreDocument>
      const blocks = Array.isArray(parsed.blocks)
        ? parsed.blocks.map(normalizeBlock).filter((b): b is TimelineBlock => b !== null)
        : []
      this.cache = { version: 1, blocks }
    } catch (err) {
      const code = (err as NodeJS.ErrnoException)?.code
      if (code !== 'ENOENT') {
        // Hand-authored records: keep the unreadable file for recovery instead
        // of letting the next write destroy it.
        try {
          fs.renameSync(this.file, `${this.file}.corrupt-${Date.now()}`)
        } catch {
          /* best effort — a failed rescue must not stop the app from opening */
        }
      }
      this.cache = { version: 1, blocks: [...EMPTY.blocks] }
    }
    return this.cache
  }

  private write(doc: StoreDocument): void {
    fs.mkdirSync(path.dirname(this.file), { recursive: true })
    const tmp = `${this.file}.tmp${process.pid}`
    fs.writeFileSync(tmp, JSON.stringify(doc, null, 1), 'utf8')
    fs.renameSync(tmp, this.file)
    this.cache = doc
  }

  list(): TimelineBlock[] {
    return this.read().blocks.slice().sort((a, b) => a.start - b.start)
  }

  save(input: unknown): TimelineBlock | null {
    const block = normalizeBlock(input)
    if (!block) return null
    const doc = this.read()
    const blocks = doc.blocks.filter((b) => b.id !== block.id)
    const prior = doc.blocks.find((b) => b.id === block.id)
    if (prior) block.createdAt = prior.createdAt
    blocks.push(block)
    this.write({ version: 1, blocks })
    return block
  }

  remove(id: string): boolean {
    const doc = this.read()
    const blocks = doc.blocks.filter((b) => b.id !== id)
    if (blocks.length === doc.blocks.length) return false
    this.write({ version: 1, blocks })
    return true
  }
}
