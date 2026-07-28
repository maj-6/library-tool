/**
 * Reading work data out of Supabase.
 *
 * Only two tables are needed: `captures` (owner-scoped by RLS, so a signed-in
 * user sees their own work) and `collections` (for names). `books` is
 * deliberately not read — migration 001 grants it to service_role only with no
 * policy, so an authenticated client gets nothing back from it. Titles
 * therefore come from `captures.meta`, which is what the phone writes anyway.
 */
import { createClient, type Session, type SupabaseClient } from '@supabase/supabase-js'
import type { CaptureRow, CollectionRow } from '../lib/derive'
import { bridge } from './bridge'

let client: SupabaseClient | null = null
let configured = false

export async function initCloud(): Promise<boolean> {
  if (client) return configured
  const config = await bridge().cloudConfig()
  configured = config.configured
  if (!configured) return false
  client = createClient(config.url, config.anonKey, {
    auth: { persistSession: true, autoRefreshToken: true, storageKey: 'work-overview-auth' },
  })
  return true
}

export function cloud(): SupabaseClient {
  if (!client) throw new Error('cloud not initialised — call initCloud() first')
  return client
}

export async function currentSession(): Promise<Session | null> {
  if (!client) return null
  const { data } = await client.auth.getSession()
  return data.session
}

export async function signIn(email: string, password: string): Promise<string | null> {
  if (!client) return 'Cloud is not configured'
  const { error } = await client.auth.signInWithPassword({ email, password })
  return error ? error.message : null
}

export async function signOut(): Promise<void> {
  await client?.auth.signOut()
}

/** PostgREST caps a response; page rather than silently truncating history. */
const PAGE = 1000

export async function fetchCaptures(since: Date | null): Promise<CaptureRow[]> {
  const db = cloud()
  const rows: CaptureRow[] = []
  for (let from = 0; ; from += PAGE) {
    let query = db
      .from('captures')
      .select('id,created_at,device,status,photos,note,contributor,meta')
      .order('created_at', { ascending: true })
      .range(from, from + PAGE - 1)
    if (since) query = query.gte('created_at', since.toISOString())

    const { data, error } = await query
    if (error) throw new Error(error.message)
    const batch = (data ?? []) as CaptureRow[]
    rows.push(...batch)
    if (batch.length < PAGE) break
  }
  return rows
}

export async function fetchCollections(): Promise<CollectionRow[]> {
  const { data, error } = await cloud()
    .from('collections')
    .select('id,name,deleted,merged_into')
  if (error) throw new Error(error.message)
  return (data ?? []) as CollectionRow[]
}
