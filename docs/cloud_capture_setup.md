# Cloud capture setup (Supabase)

The phone app and the desktop Library Tool meet in a free Supabase project:
the phone inserts one row + photos per captured book; the desktop pulls
pending rows on a schedule (or the **Sync Cloud** button), runs the photo
pipeline (perspective correction → compression → Mistral OCR → field
extraction), and files each capture as a manual entry with its photos.

## 1. Create the project

1. <https://supabase.com> → New project (free tier is plenty).
2. For a custom build, note the **Project URL** (`https://xxxx.supabase.co`)
   and its public **publishable** key. These are application build settings,
   not values an end user should ever have to enter. The official Library Tool
   builds already contain them.

## 2. Create the tables

Paste **`docs/cloud/schema.sql`** into the SQL Editor and run it. That one script
is the whole backend — `captures` and `books` for this pipeline, `volumes`,
`releases`, `profiles` and `events` for the website, plus `builds`,
`ia_catalog` and `corrections` for the working-store sync (the desktop's
gitignored builds / IA-download catalog / WHL corrections merge through
these; see `tools/store_sync.py`). It is idempotent, so re-run it whenever
the schema changes.

## 3. Create the storage buckets

```
python3 tools/cloud_setup.py buckets --apply
```

`captures` (private) holds phone photos; `volumes` (public) holds published
PDFs. Bucket creation is an owner setup task. `tools/cloud_setup.py` reads a
service credential from `SUPABASE_KEY` for this one-time administration step;
that credential is never distributed to phone or desktop users.

Then check the whole thing:

```
python3 tools/cloud_setup.py check
```

## 4. Wire up both ends

- **Desktop**: sign in to a Library Tool account, then choose the auto-sync
  interval under Settings → Sync → *Phone capture*. No Supabase key is needed.
- **Phone**: sign in and select Cloud transport. The same account works by
  default; a project maintainer can also link a separate contributor account
  to the curator's desktop in `capture_ingest_grants`. No Supabase key is
  needed. **Test connection** verifies that the signed-in capture path is
  reachable.

The public project URL/key are compiled into official builds. A fork points
both apps at its own project as part of its build/configuration; that remains
the fork maintainer's responsibility, not the user's.

The `captures` bucket stays small: after an entry is imported the desktop
keeps the processed photos locally under `DATA_ROOT/captures/<id>/` and (by
default) deletes the cloud copies.

## 5. Accounts: confirmation links + email copy

If people will sign in from the app, set the auth **Site URL / Redirect URLs**
(a fresh project's default makes confirmation links refuse the connection) and
the project-specific confirmation email — both in **[docs/cloud/auth_setup.md](cloud/auth_setup.md)**.

## Notes

- Keep RLS enabled. `schema.sql` lets an authenticated account insert its own
  captures, and lets a desktop process only its own or explicitly assigned
  contributors' captures. Storage follows the same rule; upload remains
  available to signed-in phones.
- A service credential is still appropriate for explicitly privileged owner
  tasks such as publishing public volumes and maintaining project-wide working
  stores. It is optional and is not part of phone sync.
- The `books` table is a one-way mirror of the desktop catalog (checked +
  manual) so future tools (or the phone) can read it; the desktop never
  reads it back.
