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

SQL Editor → run:

```sql
create table if not exists captures (
  id         uuid primary key default gen_random_uuid(),
  created_at timestamptz default now(),
  device     text default '',
  status     text default 'pending',   -- pending | imported | void
  photos     jsonb default '[]',       -- storage object paths
  note       text default ''
);

create table if not exists books (
  key        text primary key,         -- "<source>:<idx>" or "manual:<id>"
  data       jsonb not null,           -- the book record (one-way mirror)
  updated_at timestamptz default now()
);
```

## 3. Create the storage bucket

Storage → New bucket → name **captures**, private.

## 4. Wire up both ends

- **Desktop** (Settings → Sync → *Phone capture*): project URL +
  service_role key, Mistral API key, auto-sync interval (e.g. 15 min).
  **Test connection** should report both the table and the bucket reachable.
- **Phone** (Book Capture ⚙): the same URL + key, a device name,
  **Test connection**.

The `captures` bucket stays small: after an entry is imported the desktop
keeps the processed photos locally under `DATA_ROOT/captures/<id>/` and (by
default) deletes the cloud copies.

## Notes

- If both apps use the service_role key, leave RLS off on these two tables
  (single-user project). If you prefer the anon key on the phone, add RLS
  policies allowing `insert` on `captures` and `insert/update` on storage
  objects under `captures/*` for the anon role.
- The `books` table is a one-way mirror of the desktop catalog (checked +
  manual) so future tools (or the phone) can read it; the desktop never
  reads it back.
