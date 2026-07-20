# Collections on the desktop: two-way sync

Status: **implemented and deployed on 2026-07-19.** Migrations 009–011 are
applied to the `library-tool-store` Supabase project and verified against the
live catalogue, grants, policies, function ACL, and both migration ledgers.

## Implementation outcome

All four suggested stages below are implemented together:

- the phone sends `scan_collection_id` while preserving the frozen name and
  origin snapshots;
- migration 009 adds the shared collection rows, authenticated RLS/grants, and
  a transactional merge RPC;
- the Android store performs crash-safe, paginated two-way synchronization and
  remains fully usable signed out;
- the desktop displays read-only `Collection` and `From` snapshot columns,
  filters by collection identity, and includes a collection manager with CRUD,
  duplicate warnings, counts, and human-confirmed merges.

The implementation keeps catalogue provenance derived from `entry.extra`; it
does not widen the manual-entry schema. Desktop-created rows are pulled by an
authenticated background worker, so no blocking spinner is added to the
offline-first Collections screen. An archive state remains outside this
change's scope.

One refinement was required during adversarial review: an ordinary soft delete
is still last-write-wins and can be superseded by a later edit, but a
human-confirmed duplicate merge must be permanent. Migration 009 therefore
adds `merged_into` and performs merges through `merge_collections(...)`, which
locks both rows, validates their revisions, and atomically writes an
authoritative loser-to-survivor marker. Both clients consume that marker and
never treat an arbitrary deleted row as a merge.

## Deployment verification

The live rollout was database-first so neither client can encounter a missing
`collections` table. Verification after applying migrations 009–011
confirmed:

- RLS is enabled, with authenticated select/insert/update policies and no anon
  table access;
- authenticated writes are column-scoped, so `id`, `created_by`, and
  `merged_into` cannot be rewritten through ordinary table updates;
- update access requires a non-null signed-in identity while remaining shared
  across contributors;
- all checks, foreign keys, and covering indexes are present;
- `merge_collections(...)` has a pinned empty `search_path`, requires a non-null
  user identity from authenticated callers, and is executable only by
  `authenticated` and `service_role` (not `anon` or `PUBLIC`);
- the merge RPC completed an authenticated, rollback-only missing-row smoke
  test; and
- `009_collections`, `010_collections_authenticated_identity`, and
  `011_collection_merge_authenticated_identity` appear in the project ledger
  as well as the Supabase migration history.

The Supabase advisor intentionally reports the authenticated
`SECURITY DEFINER` merge RPC: signed-in callers must be able to invoke that
narrow transactional operation, while its pinned search path, JWT check,
revision checks, deterministic row locks, and restricted ACL bound its
authority. Newly created collection indexes are also reported as unused while
the table is empty. Other advisor notices predate this feature.

Book Capture 0.5.1-alpha.6 shipped phone-local collections (see
`android/BookCapture/README.md` → "Collections and provenance"). This document
specifies promoting them to shared cloud rows, editable from either the desktop
or the phone, and surfacing them in the desktop catalogue.

## Decisions already made

These were settled by the owner; do not relitigate them.

1. **Collections sync two-way.** Create, rename, re-origin and delete work from
   the desktop or the phone, shared across devices and contributors.
2. **A book's recorded origin is read-only on the desktop.** The `From` a book
   carries is a record of what was true at capture time, not an editable field.
   The desktop displays it and never writes it.
3. **Collections stay usable signed-out.** The phone explicitly supports local
   mode; a contributor with no account must still be able to create a
   collection and scan into it. Sync is additive, never a precondition.

Decision 2 has a consequence worth stating plainly, because it will otherwise
surprise someone: **renaming a collection does not relabel books already
scanned into it.** The phone freezes each book's collection name and origin at
`start()`, before the first photo (`CaptureSession.kt` → `writeProvenance`), so
that changing collection mid-shelf cannot retroactively rewrite history. That
property is deliberate and must survive this work. An entry therefore holds a
*snapshot*; the `collections` table holds *current* state; the two are allowed
to disagree and the UI must not pretend otherwise.

## Where things stand today

**Phone.** `android/BookCapture/app/src/main/java/org/whl/bookcapture/`

| File | Role |
|---|---|
| `Collections.kt` | `BookCollection(id, name, from)`, pure edit/validate functions, `filesDir/collections.json` via `Entries.atomicWrite` |
| `Prefs.kt` | `current_collection` — a pointer only |
| `CaptureSession.kt` | freezes `CaptureProvenance` per entry into `collection.json`; `applyProvenance` (manifest, nested, keeps the id) and `applyProvenanceToPayload` (wire, flat strings) |
| `UploadWorker.kt` | folds provenance into the outgoing `meta` for both transports |

**Wire.** Provenance rides inside the capture's `meta` as `scan_collection` and
`scan_from`.

**Desktop.** `tools/whl_explorer/server.py`

- `PHONE_PROVENANCE_KEYS = {"scan_collection", "scan_from"}`
- `_capture_provenance(cap)` merges them into `entry["extra"]` on **both**
  import paths
- `_phone_result` excludes them from its `has_metadata` test

> **The `scan_` prefix is load-bearing on both ends.** It keeps provenance from
> colliding with a model-extracted `collection`, and it is how the desktop tells
> passthrough provenance from real extraction output. Without that exclusion a
> phone with no API key looks like it already extracted a bibliography, the
> desktop skips its own OCR, and every LAN capture files blank. Guarded by
> `tests/test_phone_capture.py` and `CollectionsTest`. Renaming these keys means
> changing both sides in the same commit.

So today a collection reaches the desktop only as two untyped strings inside a
generic `extra` blob, rendered as "scan collection" / "scan from".

## Gap: the wire carries no collection id

`applyProvenanceToPayload` sends the collection **name** only. The manifest keeps
the id (`applyProvenance`), but it never leaves the phone.

Without an id the desktop cannot tell a renamed collection from a different one,
so it cannot link an entry to a synced collection row, count books per
collection reliably, or filter by collection across a rename.

**Required change:** add `scan_collection_id` to the wire payload and to
`PHONE_PROVENANCE_KEYS`, keeping `scan_collection` (the name snapshot) as well.
Both are needed: the id links, the name records what the book was actually filed
under at the time.

Entries imported before this change have a name but no id. Treat a missing id as
"unlinked" — match by name only as a display convenience, never as identity.

## Data model — migration 009

Migration 009 introduced the table and merge function; migrations 010 and 011
harden authenticated identity checks. The contract is in
`docs/cloud/schema.sql`: append-only, idempotent DDL only, explicit
revoke/grant + RLS beside every new table, ending with the `schema_migrations`
insert. Verify with `python3 tools/cloud_setup.py check`.

```sql
create table if not exists collections (
  id          uuid primary key,          -- the phone's own id; NOT generated here
  name        text not null,
  from_place  text not null default '',
  created_by  uuid references auth.users(id),
  updated_at  timestamptz not null default now(),
  deleted     boolean not null default false,
  merged_into uuid references collections(id)
);
create index if not exists collections_updated_idx on collections (updated_at desc);
```

Notes on each choice:

- **`id` is the phone's UUID, not `gen_random_uuid()`.** A collection is created
  offline and may be scanned into for days before it ever syncs. The local id
  must be the identity, or the first sync forks it.
- **`from_place`, not `from`** — `from` is a SQL keyword and a needless quoting
  hazard. Map it to `from` in the API layer.
- **Soft delete.** A hard delete would orphan entries that reference the id and
  would resurrect on the next push from a phone that never saw the delete.

### Grants and RLS — two traps

**Trap 1: do not copy the `taxonomy` pattern.** `taxonomy` is the obvious
precedent for a synced desktop list, but it is `revoke all ... from anon,
authenticated` and granted to `service_role` only. It is a desktop working store
pushed by `tools/store_sync.py` with the service key. **The phone must never
hold the service key**, so collections cannot work that way. Follow `captures`
instead: RLS plus column-level grants for `authenticated`.

**Trap 2: column-level UPDATE grants are enumerated, and RLS will not save
you.** `001_baseline.sql:105-106` grants update on `captures` column by column.
A column omitted there is silently unwritable — RLS passes, the column grant
rejects. Any column added to `collections` must be added to its grant list too.

```sql
alter table collections enable row level security;
revoke all on public.collections from anon, authenticated;
grant select on public.collections to authenticated;
grant insert (id, name, from_place, created_by, updated_at, deleted)
  on public.collections to authenticated;
grant update (name, from_place, updated_at, deleted)
  on public.collections to authenticated;
grant select, insert, update, delete on public.collections to service_role;
```

Deliberately **not** granted to `authenticated`: `id` and `created_by` on
update — the ownership boundary, mirroring `revoke update (id, created_at,
created_by) on captures`.

Policy shape: collections are a **shared vocabulary of physical batches**, not
private data — two contributors emptying the same storage room need the same
crate list. So: any authenticated member may select and insert; update and
soft-delete likewise. If the member gate from migration 005 is ever enabled
(it is deliberately held back by `007_unreleased_member_gate_holdback.sql`),
these policies should be revisited to require an approved member.

## Sync semantics

**Last-write-wins on `updated_at`**, matching `tools/store_sync.py`'s existing
merge (that module's docstring describes the shadow-ledger approach; read it
before inventing a second one). Concretely:

- Every local mutation stamps `updated_at`.
- On sync, per id, the higher `updated_at` wins for the whole row.
- `deleted = true` is a value like any other, so a delete propagates and a
  concurrent rename loses to a later delete.
- `merged_into` is the deliberate exception: only the transactional merge RPC
  may write it, and once present it authoritatively aliases the loser to the
  survivor on every client.
- Clock skew: the phone's clock is not trustworthy. Prefer the server's
  `now()` on write where possible and treat local stamps as a tiebreak only.

**Name collisions across devices.** The phone rejects duplicate names
case-insensitively (`collectionNameTaken`). Two phones can still create "Blue
crate" independently, producing two ids with the same name. Do **not**
auto-merge — they may be genuinely different crates. Surface the duplicate in
the desktop Collections view and let a human merge. A merge is: pick a
surviving id, repoint entries' `scan_collection_id`, soft-delete the other.

**Local mode.** A signed-out phone keeps `collections.json` as the sole store
and syncs nothing. On first sign-in, push local collections that have no cloud
counterpart. This is the same shape as the anonymous-capture claim flow in
`CaptureOwnership.kt` — read it for precedent.

## Desktop work

1. **Read the table.** New sync path; service key on the desktop is already
   available for working-store tables, but prefer the signed-in user's session
   so it works the same way the phone's does.
2. **Promote the fields.** `Collection` and `From` become real table columns
   rather than keys in `extra`. `BOOK_COLS` and `CHECKED_COLS` in
   `static/app.js` are parallel lists that must stay in step; `manualToBook`
   needs the passthrough. Whether these become entries in
   `lib.MANUAL_ENTRY_FIELDS` or stay derived from `extra` is an open
   implementation choice — see "Open questions".
3. **Filter.** Filter the catalogue by collection. There is an existing facet
   system (`tests/facets_behavior.test.js`) and an existing source filter for
   phone captures — mirror those rather than inventing a new control.
4. **Collections view.** List collections with book counts. Counts come from
   entries' `scan_collection_id`, not from the phone (the phone only ever knows
   what is still on that phone — its recent list is pruned).
5. **Editing.** Collection name and origin are editable here. A book's recorded
   `From` is **not** (decision 2). Make that visibly obvious rather than
   silently ignoring edits.

## Backfill

- **Existing entries** carry `scan_collection` (name) but no id. Leave them
  unlinked. Optionally offer a one-time "link by exact name" action in the
  Collections view, human-confirmed, never automatic.
- **Existing phone collections** push on first sync after upgrade.
- No migration of `manual_entries.json` is required if the fields stay derived
  from `extra`; if they are promoted to first-class entry fields, that file is
  an id-keyed dict written under `_manual_lock` and needs a forward-compatible
  read (absent key ⇒ empty string), not a rewrite.

## Test plan

Mirror the existing two-sided pattern — a test on each side that names the
other, as `test_phone_capture.py` and `CollectionsTest` already do.

- **Migration**: `tests/test_cloud_migrations.py` covers the migrations
  directory; the new file must satisfy it (idempotent, registers itself).
- **Grants**: assert `collections` is *not* granted to `anon`, and that every
  writable column appears in the update grant. Trap 2 deserves its own test.
- **Merge**: last-write-wins both directions; delete beats an older rename;
  a newer rename beats an older delete.
- **Identity**: a rename on device A does not alter the snapshot on entries
  already captured under the old name.
- **Local mode**: create signed-out, sign in, collection appears in the cloud
  exactly once (not duplicated on a second sign-in).
- **Wire**: `scan_collection_id` reaches `extra`; a payload without it still
  imports (older phones).
- **Fallback OCR**: extend the existing guard so the new key is also excluded
  from `has_metadata`. This is the highest-severity regression in this area and
  it has already happened once.

## Suggested staging

Each stage is shippable alone.

1. **Wire the id.** Add `scan_collection_id` on the phone and to
   `PHONE_PROVENANCE_KEYS`. No schema, no UI. Unblocks everything else and is
   the only change that must reach users *before* the rest is useful.
2. **Desktop surface, read-only.** Columns + filter, still sourced from
   `extra`. Delivers most of the day-to-day value with no cloud work.
3. **Migration 009 + one-way push.** Phone pushes collections; desktop reads
   and lists them with counts.
4. **Two-way.** Desktop edits, conflict handling, the merge UI for duplicate
   names.

## Resolved implementation questions

- **Field representation:** catalogue columns remain derived from `extra`,
  preserving the existing manual-entry schema and immutable capture snapshot.
- **Desktop-to-phone visibility:** authenticated background synchronization
  pulls desktop-created collections without making local collection editing
  wait on the network.
- **Archive state:** not added. Soft delete and authoritative merge tombstones
  cover this design; a separate archive lifecycle can be designed later.
