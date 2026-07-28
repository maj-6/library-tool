/**
 * The renderer's view of the Electron preload bridge.
 *
 * Everything here degrades when the bridge is absent, so the UI can also be run
 * in a plain browser (vite dev, tests) against an in-memory store instead of
 * crashing on `window.workOverview`.
 */
import type { TimelineBlock } from '../lib/model'

interface Bridge {
  listBlocks(): Promise<TimelineBlock[]>
  saveBlock(block: TimelineBlock): Promise<TimelineBlock | null>
  deleteBlock(id: string): Promise<boolean>
  cloudConfig(): Promise<{ url: string; anonKey: string; configured: boolean }>
}

declare global {
  interface Window {
    workOverview?: Bridge
  }
}

/** Volatile stand-in used when running outside Electron. */
class MemoryBridge implements Bridge {
  private blocks = new Map<string, TimelineBlock>()

  listBlocks(): Promise<TimelineBlock[]> {
    return Promise.resolve([...this.blocks.values()].sort((a, b) => a.start - b.start))
  }

  saveBlock(block: TimelineBlock): Promise<TimelineBlock | null> {
    this.blocks.set(block.id, block)
    return Promise.resolve(block)
  }

  deleteBlock(id: string): Promise<boolean> {
    return Promise.resolve(this.blocks.delete(id))
  }

  cloudConfig(): Promise<{ url: string; anonKey: string; configured: boolean }> {
    return Promise.resolve({ url: '', anonKey: '', configured: false })
  }
}

const fallback = new MemoryBridge()

export function bridge(): Bridge {
  return window.workOverview ?? fallback
}

export const isEmbedded = (): boolean => Boolean(window.workOverview)

export function newBlockId(): string {
  return (globalThis.crypto?.randomUUID?.() ?? `b-${Date.now()}-${Math.random().toString(16).slice(2)}`)
}
