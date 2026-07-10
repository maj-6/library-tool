# The Library Tool website

Three static pages: an introduction, a downloads page, and a library browser
over the volumes published to Supabase. No build step, no framework, no CDN —
`fetch` against PostgREST is the whole data layer, and it speaks the same HTTP
as `tools/supabase_sync.py`.

```
python3 -m http.server 8080 --directory website
```

Without `assets/config.js` the browser reads `fixtures/volumes.json`, so the
site works before the cloud holds anything. Regenerate the fixture from the
local builds with:

```
python3 tools/cloud_setup.py fixture
```

## Pointing it at the cloud

```
python3 tools/cloud_setup.py anon-key      # prints the snippet
```

Write it to `assets/config.js` — gitignored, because the project reference is
yours. The **anon** key belongs here, never the service_role key. Row-level
security is what protects the project: `docs/cloud/schema.sql` grants anon
exactly two reads, `volumes` and `releases`, and nothing else.

## Publishing

Any static host. The site is plain files; `browse.html?q=…&year=…` keeps its
query in the URL, so deep links and the back button work without a router.

Two things to decide before uploading volumes:

- **Storage.** The `volumes` bucket is public — that is the point of a public
  library. Supabase's free tier gives 1 GB, and the local collection is 62 PDFs
  of which one is 129 MB. Volumes will need Supabase Pro, or an R2/B2 bucket.
  The schema anticipates this: a volume carries `pdf_path` (the Supabase bucket)
  *or* `pdf_url` (anywhere), and readers prefer `pdf_url`. Moving storage later
  is a column update, not a migration.
- **Copyright.** Only publish what is public domain. The desktop's copyright tag
  exists to answer that question, and its Info panel shows the registration and
  renewal records behind the verdict.
