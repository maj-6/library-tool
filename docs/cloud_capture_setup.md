# Cloud capture setup (Supabase)

The phone app, the desktop Library Tool, and the optional cloud image worker
meet in a Supabase project:
the phone inserts one row + photos per captured book, along with the OCR
text and fields it already extracted in the background (BookCapture 0.2.0+,
i.e. every current build);
the desktop pulls pending rows on a schedule (or the **Sync Cloud** button),
reuses the phone's text and fields — re-processing only the photos
(perspective correction → compression) — and runs the full desktop pipeline
(Mistral OCR → field extraction) only for captures the phone didn't
process, then files each as a manual entry with its photos.

The cloud is not the only route: the desktop's **Settings → LAN** accepts
captures straight from the phone over the local network (phone Settings ›
Transport → LAN or Auto) — no internet, same ingest, identical result. This
document covers the cloud route.

## 1. Create the project

1. <https://supabase.com> → New project (free tier is plenty).
2. Note the **Project URL** (`https://xxxx.supabase.co`) and its public
   **publishable** key. The official Library Tool builds already contain
   them (`tools/cloud_defaults.py`); an end user never enters them. A fork
   bakes its own pair into a custom build, or — on the desktop — overrides
   them at runtime in Settings (project URL under Integrations, the
   *Custom-project public key* under Credentials).

## 2. Create the tables

The schema ships as ordered migrations in **`docs/cloud/migrations/`**. On a
fresh project, paste each file into the SQL Editor and run it, in order
(`001_baseline.sql` first). Together they are the whole backend — `captures`,
`capture_ingest_grants` and `books` for this pipeline; `volumes` (with
`volume_texts` / `volume_pages` / `volume_notes`), `author_pages`, `releases`,
`profiles` and `events` for the website; `profile_secrets` for account-synced
API keys; plus `builds`, `ia_catalog`, `corrections` and `taxonomy` for the
working-store sync (the desktop's gitignored builds / IA-download catalog /
WHL corrections / category taxonomy merge through these; see
`tools/store_sync.py`). Migration 015 adds the owner-readable
`photo_processing_jobs` queue used by the optional image processor. Migration
017 adds `capture_book_metadata` (desktop-authored registered-book snapshots)
and `capture_reviews` (small, shared attention/review state). Migration 020
adds the nullable, revisioned `.lib/3` association acknowledgement directly to
`captures`, so status and association can be published in one transaction.
Migration 021 introduced the first exact-scope publication RPC; append-only
migration 022 replaces it with the two-party capability protocol described
below. The archive itself never leaves the importing desktop.

On an existing project, don't re-paste everything: `python3
tools/cloud_setup.py check` diffs the `schema_migrations` table against the
directory and names the files still pending — paste those, in order. Every
migration is idempotent and records itself, so re-running one is harmless.
Rollback follows the same rule: migrations are append-only, so never edit an
applied file — ship a new migration that reverses the change.
## 3. Create the storage buckets

```
python3 tools/cloud_setup.py buckets --apply
```

`captures` (private) holds phone originals; `capture-derivatives` (private)
holds corrected display/OCR/thumbnail artifacts; `volumes` (public) holds
published PDFs. Bucket creation is an owner setup task. `tools/cloud_setup.py`
reads `SUPABASE_KEY` from the process environment for this one-time
administration step. Use a backend-only `sb_secret_...` key or legacy
service-role credential; it is never distributed to phones or ordinary users.

Then check the whole thing:

```
python3 tools/cloud_setup.py check
```

It verifies every expected table, view and column against the migrations,
names pending migrations, checks bucket visibility, and smoke-tests the anon
role (public reads work; profiles/events/captures refuse). Non-zero exit on
any failure.

## 4. Wire up both ends

- **Desktop**: sign in to a Library Tool account, then choose the auto-sync
  interval under Settings → Integrations → *Phone capture (Supabase)*.
  Downloading and locally importing a capture uses the signed-in account.
  Confirming its trusted `.lib/3` association, returning registered-book fields
  to phones, and consuming phone review changes are trusted curator operations;
  the library owner must add the project service key under Settings →
  Credentials → *Owner service key*. It is held only in the desktop engine's
  protected secret store. Both that key and a signed-in user session are
  required, and they must name the same Supabase project: the user session
  first prepares a five-minute, exact-scope capability bound by `auth.uid()` to
  the capture, association, observed revision, and requested status change.
  The service credential can consume only that unguessable capability; it
  cannot supply or impersonate an actor. The consuming transaction locks the
  capture and capability, rechecks current ownership or the locked ingest
  grant, and compare-and-sets the association plus imported status. Only the
  capability's SHA-256 is stored, and an exact consumed receipt remains
  replayable for seven days so a lost HTTP response cannot advance the
  revision twice. Direct service-role table updates cannot write the trusted
  association columns, and a service key by itself never enables phone
  round-trip publication. A private, bounded cleanup runs hourly through
  Supabase Cron, in a transaction separate from publication locks, and keeps
  30 days of this job's run history. Without
  the protected owner
  credential, a locally sealed capture must remain remotely `pending` until a
  later retry can atomically publish `status=imported` plus the association.
  A Mistral API key (Settings → Credentials) is
  needed only for captures the phone didn't pre-OCR; it syncs through
  `profile_secrets`, so a key entered on either device follows the
  signed-in account to the other. **Test connection** in the same panel
  checks the desktop's capture path.
- **Phone**: sign in and select the Cloud (or Auto) transport. The same
  account works by default; a project maintainer can also link a separate
  contributor account to the curator's desktop in `capture_ingest_grants`.
  No Supabase key is needed. **Test connection** verifies that the
  signed-in capture path is reachable.

The public project URL/key are compiled into official builds (the desktop
can also override them in Settings — see step 1). A fork points both apps
at its own project as part of its build/configuration; that remains the
fork maintainer's responsibility, not the user's.

The `captures` bucket stays small: after an entry is imported the desktop
keeps the processed photos locally under `DATA_ROOT/captures/<id>/` and (by
default) deletes the cloud copies.

When a captured entry is registered as a desktop book, an owner sync publishes
only that capture's bounded phone projection: copyright status and any located
registration/renewal records, WHL and Internet Archive availability, scan
status, and remarks. Phones can read snapshots only for
their own captures; the full service-only `books` mirror is not exposed. The
phone may pull this state without uploading anything. Phone attention/review
edits stay in an offline sidecar until the user presses **Sync captures**, then use a
server-revision compare-and-set through `capture_reviews`. Capture upload and
review publication are never started by capture completion, app resume, or
background OCR; **Sync captures** freezes the eligible batch and its selected
destination. A failed or interrupted batch resumes that same batch rather than
silently including later captures or switching between LAN and cloud.

Only IDs that still exist in the cloud `captures` table are published to
`capture_book_metadata` or `capture_reviews`; direct-LAN captures therefore
cannot violate those tables' capture foreign keys. A paired desktop provides
the equivalent projection and review exchange at authenticated
`POST /lan/metadata`, using a stable desktop identity and content revisions,
without requiring a Supabase row. The LAN identity is stored atomically and is
rotated if its compact revision ledger is lost; newer timestamps allow safe
recovery from a restored/pruned ledger while older delayed responses stay
stale. Metadata requests, multipart captures, photo count, and each photo are
bounded before ingest. Removed desktop registrations are published
as higher-revision tombstones (`book_id` empty) rather than deleting/recreating
the projection row. Review rows are likewise updated in place; neither phone
nor service roles receive direct table DELETE permission.

Copyright display uses the curated desktop rights value while retaining the
automated copyright result and its registration/renewal evidence. A draft WHL
match remains **unknown**, not available. The phone rejects oversized or
malformed projection rows independently, preserves a corrupt local review
sidecar for recovery, and combines simultaneous nonempty desktop/phone reasons
(or reports a length conflict) instead of silently discarding either note.
Desktop projection publication uses per-row revision compare-and-set plus a
small source vector (build, manual source, and tombstone timestamps), so a
second desktop without the source-only fields cannot erase richer phone data
or resurrect a newer tombstone.

### Capture archive confirmation

The portable association is the exact `org.whl.capture-lib-association`
version-1 document produced by capture import. Its state is `current` or
`stale`; only `current` produces the confirmed marker. Transport ordering is
kept outside that frozen document as a server-owned revision and timestamp.
The phone persists the resulting confirmation in `lib_association.json`, so
the marker survives offline use and process restarts. Null legacy rows, stale
documents, malformed documents, and out-of-order revisions remain unconfirmed.
No local archive path, token, or owner credential is part of either document.

Migration 020 deliberately replaces the historical table-wide authenticated
`INSERT` grant on `captures` with a column list. PostgreSQL table privileges
otherwise include columns added later, which would let a phone forge the new
association during its initial insert. Authenticated users may read the
association only through the existing owner-or-assigned-ingester capture RLS;
association writes are service-only and also guarded by a trigger. These
explicit grants are required on Supabase projects that no longer add Data API
privileges for new schema objects automatically.

Migration 022 has the desktop pass its exact verified association to
`publish_capture_lib_association` with separate owner-service and signed-in-user
configs. An authenticated RPC binds `auth.uid()` to an exact capture,
association, expected revision, and status intent under a freshly generated
256-bit capability. A service-role-only RPC receives only that capability,
then locks the capture and hashed capability, rechecks the bound actor against
the current owner or locked ingest grant, and applies the association/status
CAS in the same transaction. A grant, ownership, revision, status, or document
change therefore conflicts. Expired capabilities return 410; concurrency
conflicts return 409. Direct service-role association column updates are not
granted. Remote photos are deleted only after the RPC returns the exact
accepted association. If a catalogue edit makes the local association stale
after durable ingest but before that first receipt, the same exact stale
document can still mark the capture imported; it remains unconfirmed on the
phone and its remote photos are retained until a current archive is resealed.

LAN `/lan/capture` receipts and `/lan/metadata` projections expose
`org.whl.capture-lib-confirmation` envelopes. Their independent durable stream
ledger keeps exact replays stable across restarts, advances revision and
timestamp when the current/stale association changes, and rotates the stream
before any safe revision reset after ledger loss or corruption. The raw
`lib_association` receipt field remains for older paired clients; older
cloud/LAN responses without a confirmation also remain valid but unconfirmed.

When migration 015 finds a valid processing request in an uploaded capture,
it atomically changes that row from `pending` to `processing`. The desktop
therefore waits while the worker uses the immutable original. Once every job
for that capture is completed or terminally failed, the worker returns the
capture to `pending` and normal desktop import resumes. Corrected files do not
go into `captures.photos`; that array remains the exact original-photo
transport contract.

The deployable worker, its local test commands, and a complete Cloud Run setup
are in [`services/image_processor/README.md`](../services/image_processor/README.md).

## 5. Accounts: confirmation links + email copy

If people will sign in from the app, set the auth **Site URL / Redirect URLs**
(a fresh project's default makes confirmation links refuse the connection) and
the project-specific confirmation email — both in **[docs/cloud/auth_setup.md](cloud/auth_setup.md)**.

## Notes

- Keep RLS enabled. The schema lets an authenticated account insert its own
  captures, and lets a desktop process only its own or explicitly assigned
  contributors' captures. Storage follows the same rule; upload remains
  available to signed-in phones.
- A backend secret is still appropriate for explicitly privileged owner
  tasks such as publishing public volumes and maintaining project-wide working
  stores, and it is required by the image worker. It is never part of phone or
  desktop account sync.
- The `books` table is a one-way mirror of the desktop catalog (checked +
  manual) so future tools (or the phone) can read it; the desktop never
  reads it back. Each row has a database-generated UUID identity; its unique
  source `key` remains only the conflict target for mirror upserts.
