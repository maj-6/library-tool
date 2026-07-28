/**
 * Which cloud the renderer talks to.
 *
 * Baked defaults come from tools/cloud_defaults.py (generated, public
 * identifiers only). Environment variables override them so a fork, or a local
 * Supabase, can be pointed at without a rebuild — the same override story the
 * Python side has in Settings.
 */
import { SUPABASE_URL, SUPABASE_ANON_KEY } from './cloudDefaults.generated.js'

export interface CloudConfig {
  url: string
  anonKey: string
  /** false when neither a baked nor an overridden value is usable */
  configured: boolean
}

export function loadCloudConfig(): CloudConfig {
  const url = (process.env.WORK_OVERVIEW_SUPABASE_URL || SUPABASE_URL || '').trim()
  const anonKey = (process.env.WORK_OVERVIEW_SUPABASE_ANON_KEY || SUPABASE_ANON_KEY || '').trim()
  return { url, anonKey, configured: Boolean(url && anonKey) }
}
