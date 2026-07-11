# Cloud capture setup (Supabase)

The phone app and the desktop Library Tool meet in a free Supabase project:
the phone inserts one row + photos per captured book; the desktop pulls
pending rows on a schedule (or the **Sync Cloud** button), runs the photo
pipeline (perspective correction → compression → Mistral OCR → field
extraction), and files each capture as a manual entry with its photos.

## 1. Create the project

1. <https://supabase.com> → New project (free tier is plenty).
2. Note the **Project URL** (`https://xxxx.supabase.co`) and, from
   *Settings → API*, the **service_role key** (used by the desktop; treat it
   like a password — it bypasses row security).

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

`captures` (private) for phone photos, `volumes` (public) for published PDFs.
The Storage API accepts the service_role key, so this needs no SQL.

Then check the whole thing:

```
python3 tools/cloud_setup.py check
```

## 4. Wire up both ends

- **Desktop** (Settings → Sync → *Phone capture*): the service_role key,
  Mistral API key, auto-sync interval (e.g. 15 min). The project URL and the
  anon key are built into the app (`tools/cloud_defaults.py`) — set them only
  when pointing at your own project. **Test connection** should report both
  the table and the bucket reachable.
- **Phone** (Book Capture ⚙): the same URL + key, a device name,
  **Test connection**.

The `captures` bucket stays small: after an entry is imported the desktop
keeps the processed photos locally under `DATA_ROOT/captures/<id>/` and (by
default) deletes the cloud copies.

## Notes

- If both apps use the service_role key, leave RLS off on these two tables
  (single-user project). If you prefer the anon key on the phone, it needs
  RLS policies for `select` + `insert` on `captures` (the connection test
  reads one row; retried inserts use ignore-duplicates) and `insert` +
  `update` on storage objects under `captures/*` (uploads are upsert).
- The `books` table is a one-way mirror of the desktop catalog (checked +
  manual) so future tools (or the phone) can read it; the desktop never
  reads it back.
