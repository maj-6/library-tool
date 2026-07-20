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
`photo_processing_jobs` queue used by the optional image processor.

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
service-role credential; it is never saved in desktop settings or distributed
to phone/desktop users.

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
  interval under Settings → Integrations → *Phone capture (Supabase)*. No
  Supabase key is needed. A Mistral API key (Settings → Credentials) is
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
