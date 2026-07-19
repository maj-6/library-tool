# Architecture: data ownership and trust boundaries

What owns which data, what needs the network, and who can touch what.
Per-component detail lives in each part's own README (map at the end).

## Components and their sources of truth

- **Library Engine** (`src/librarytool/`) — framework-neutral application
  services, contracts, capability manifests, and storage ports. It imports
  neither Flask nor the transitional `tools` modules, and it does not resolve
  `DATA_ROOT` or create files at package-import time. `LibraryEngine` carries
  an immutable, versioned service registry assembled from installed module
  contributions. The filesystem production graph is now composed by
  `src/librarytool/composition/filesystem.py`, which selects adapters from
  injected paths, codecs, locks, jobs, policies, and provider ports without
  importing Flask or performing import-time I/O. The Flask sidecar delegates
  to that composition root through the transport-neutral lifecycle host in
  `src/librarytool/composition/host.py`. That host owns one recoverable write
  set, persisted job manager, provenance service, sealed engine graph, startup
  recovery report, and non-blocking process-lifetime workspace lease. It uses
  explicit immutable configuration, has native strict/atomic job-history I/O,
  imports no Flask or transitional `tools` module, and can be owned directly by
  a CLI or Qt process or by a Godot sidecar. Importing the production transport
  does not claim the workspace; executable startup opens it before migrations
  or workers, while an embedded Flask host opens it on its first trusted
  request. The remaining lifecycle gap is coordinated worker/provider shutdown:
  those executors are still borrowed from `server.py`, so session close does
  not pretend to cancel or join them. Session resources are borrowed and become
  invalid when their owning session closes. The Flask transport is a
  single-process workspace owner; additional clients connect to that sidecar
  rather than starting pre-fork/multi-worker owners. A controlled embedder may
  reload only after stopping its workers and explicitly closing/unpublishing
  the transport session. Module-owned item policies
  are selected by the same capability resolution as services, so absent OCR,
  translation-generation, research, or publishing components do not leak
  commands into item views. Browser workbenches cross one semantic
  `EngineClient`.
  Installed capabilities and available/degraded/blocked workbenches are
  discoverable at `/api/v1/capabilities`. Background processors share the
  engine's `JobManager` lifecycle, cancellation, restart recovery, typed views,
  and cursor events while provider execution remains behind adapters. The
  catalogue query spine exposes immutable item, representation, artifact, and
  readiness views. Its collection/detail ETags identify complete response
  snapshots; item detail separately exposes `X-Record-Revision`, which
  versioned catalogue updates must supply through strong
  `If-Record-Match`. Catalogue-only create and update commands now run through
  a recoverable filesystem repository and require replay-safe
  `Idempotency-Key` values. Their `EngineClient` methods are ready, but the
  transitional catalogue editor still uses legacy mutation routes. Default
  DTOs replace attached paths with opaque representation identities; only the
  explicit `build-workbench` projection carries the old local build record.
  The translation aggregate now supplies
  versioned list/detail/page-replacement resources, authoritative
  current/stale/untracked/missing/orphaned status, dual document/source
  preconditions, and recoverable text-plus-provenance publication; Replica's
  preview consumes it through `EngineClient.translations`. Existing-item
  `.lib` import likewise publishes layout, text, figures, styles,
  translations, provenance, and a durable receipt in one recoverable
  multi-file unit of work. New-item `.lib` open is a separately gated service
  requiring both catalogue creation and Replica interchange: allocation,
  catalogue state, entry assets, component receipts, and the global replay
  receipt publish through one recoverable transaction. The desktop local-path
  route delegates to it, and portable clients use `/api/v1/lib-opens`.
  Representation attachment, provider-backed translation generation, and
  legacy item delete/restore are not yet migrated to these command boundaries.
- **Desktop app** — the workbench: an Electron shell (`desktop/`) that
  spawns the Flask sidecar (`tools/whl_explorer/server.py`) on a loopback
  port. Paths split into two roots (`tools/libcommon.py`): **`APP_ROOT`**,
  read-only assets shipped with the app (`ch_library.xlsx`, the reference
  CSVs, the generated catalogue JSON — the PyInstaller bundle dir when
  frozen); and **`DATA_ROOT`**, all writable per-user state — the JSON
  document store, entry folders, IA downloads,
  `output/client_state.json`. The big search databases (the OL indexes,
  the renewals CSV) resolve most-accessible-first via `find_db`: the
  `~/.library-tool` drop-in folder (where in-app downloads also land),
  then `DATA_ROOT`, then the bundle. Packaged, the shell sets
  `WHL_DATA_ROOT=%APPDATA%\Library Tool`; a dev checkout uses the repo
  root; `WHL_DATA_ROOT` overrides either. **The local `DATA_ROOT` is
  authoritative** for the working catalogue (checked books, manual
  entries, builds, corrections, entries); the cloud tables are sync
  channels and mirrors of it, not the master copy.
- **Book Capture** (`android/BookCapture/`) — captures queue on the phone
  (`filesDir/queue/<entryId>/`), then leave as a `captures` row + photos
  in the private `captures` bucket, or as a direct LAN POST to a paired
  desktop. Either way the capture's destiny is a desktop entry; the phone
  keeps only a pruned recent-scans history. Every book is scanned into a
  phone-local **collection** carrying a **From** (where the batch came
  from); the pair rides inside each capture's `meta` and lands in the
  desktop entry's `extra`, so provenance needs no column of its own.
- **Website** (`website/`, GitHub Pages) — stateless plain files, no build
  step. `assets/data.js` reads the cloud over PostgREST with the anon key
  from `assets/config.js` (gitignored); without that file it reads the
  committed `fixtures/` instead, so the site works with no cloud at all.
  It owns nothing; everything it shows was published from the desktop.
- **Cloud** — Supabase (Postgres + Auth + Storage) plus a Cloudflare R2
  bucket for large objects (published PDFs, the `entries/` and `corpus/`
  mirrors). The schema ships as ordered, append-only migrations under
  `docs/cloud/migrations/` (`schema.sql` is the entry point that explains
  the flow); each is idempotent and records itself, and
  `tools/cloud_setup.py check` diffs the `schema_migrations` table against
  that directory to name what is still pending. The cloud is authoritative only for what is
  born there or published to it: accounts, the `captures` queue, the
  shared `events` feed, and the published `volumes` catalogue + its
  artifacts; the working-store tables (`builds`, `ia_catalog`,
  `corrections`, `taxonomy`, `books`) mirror desktop state.

## Network dependencies

Day-to-day cataloguing is offline. The checks run against local copies
(`tools/catalog_checks.py`): copyright renewals from
`copyright_renewals.csv` and WHL presence from `whl_catalog.csv`. The
constrained Open Library search is local too — `tools/ol_client.py` over
the SQLite indexes (`ol_search.db` / `ol_works.db`, built by
`tools/build_ol_index.py` / `build_ol_search.py`). Phone voice
recognition is offline as well (Vosk, on-device).

The network is used for: IA + HathiTrust scan search (`scan_search.py`),
IA PDF downloads, the WHL metadata scrape and live search
(`whl_scrape.py`, `whl_client.py`), CPRS copyright-*registration* lookups
(`copyright_registration.py`), AI/OCR API calls, cloud sync and publish,
the release pipeline, and the desktop's startup auto-update check against
GitHub Releases.

In between sits the **paired LAN** path: the sidecar runs a separate
capture listener (default port 8899, `server.py`; off unless Settings >
LAN enables it) and the phone's Settings > Transport pairs host + token.
A LAN capture POSTs photos straight to the desktop and feeds the same
ingest as cloud sync — no internet on that leg.

## Trust boundaries

Four tiers, enforced by RLS + explicit grants in the migrations under
`docs/cloud/migrations/`, which follow a revoke-then-grant convention so
every anon reach is deliberate. The website ships the anon key on purpose;
RLS is what protects the project.

**PUBLIC** (anon-readable — this data is intentionally a public library):

- `anon` gets `select` on exactly these: `volumes`, `volume_texts`,
  `volume_pages`, `volume_notes`, `author_pages`, the `author_index`
  view, `releases`, `index_versions` (search-index metadata — counts,
  model id, hashes, never text), and `schema_migrations`. Nothing else.
- Two functions are `execute`-able by `anon`, and they are the *only*
  path to the corpus: `search_volume(...)` (page search) and
  `search_passages(...)`. The `passages` table itself is explicitly
  revoked from `anon`/`authenticated` with no read policy, so passage
  text and embeddings are reachable through the RPC alone, never by a
  direct table read.
- The `volumes` storage bucket and the R2-hosted published PDFs are
  world-readable — that is the point. The CH catalogue data shipped with
  the app is likewise public by design.
- All of these are written only by `service_role` (the desktop publish
  path); `volumes` deliberately has no authenticated-insert policy, since
  signup is open and the table IS the public website.

**CONTRIBUTOR** (any authenticated account, scoped by RLS):

- `captures`: insert only as yourself (`created_by = auth.uid()`);
  select/update your own rows, or a contributor's rows when a
  maintainer-provisioned `capture_ingest_grants` row pairs you as their
  ingester. The `captures` bucket mirrors the same policy; grants rows
  themselves are readable (own only) but writable only by `service_role`.
- `events` (the shared activity feed): read for any signed-in user;
  append-only insert, and `actor_id` must be the writer's own uid.
- `profiles`: readable by signed-in users only (contributor names are not
  for the open internet), writable only for your own row.
- The working-store sync tables (`builds`, `ia_catalog`, `corrections`,
  `taxonomy`) and the `books` mirror are **not** contributor-writable:
  RLS is on with no policy, so only `service_role` reaches them.

**PER-USER SECRET**:

- Desktop API keys and credentials (AI, OCR, Supabase service key, R2
  key/secret, the Google service-account key path) live in
  `DATA_ROOT/output/secrets.json` — local-only, gitignored, never synced,
  and served only through the Host-guarded `/api/secrets` (loopback
  origin, anti-DNS-rebinding). They were migrated *out* of
  `client_state.json` so the synced settings blob carries no secrets.
  The signed-in session token sits in `output/auth_session.json`
  (gitignored); the LAN pairing token in `DATA_ROOT/lan_token.txt`.
- Bring-your-own Mistral/DeepSeek keys shared between phone and desktop
  live in the `profile_secrets` table — a separate table, not a
  `profiles` column, readable and writable by exactly one user
  (`id = auth.uid()`). The desktop reconciles its local copy with it.

**OWNER-ONLY** (never on a client, never in the repo):

- The Supabase `service_role` key — publishing, working-store sync, and
  maintenance; bypasses RLS. Entered by hand on the owner's desktop only.
- R2 write credentials (`r2KeyId`/`r2Secret` in the secrets store).
- The Google Sheets service-account JSON key (Settings > Credentials;
  no credentials exist yet, the sync is TODO-verify).
- The release pipeline secrets on the public repo:
  `SUPABASE_SERVICE_ROLE_KEY`, the Android keystore set, and
  `WIN_CSC_LINK_B64`/`WIN_CSC_KEY_PASSWORD` (see `docs/releasing.md`).
- The Supabase dashboard / SQL editor, where the migrations are applied,
  in order.

## Account model

Supabase Auth, email/password, with a confirmation email
(`docs/cloud/auth_setup.md`). The desktop ships the public project URL +
anon key baked in (`tools/cloud_defaults.py`); Book Capture gets them
injected at build time into `BuildConfig` from the
`WHL_SUPABASE_URL`/`WHL_SUPABASE_ANON_KEY` CI variables (a from-source
APK has blank defaults and needs the project set in its Settings). No
user pastes a Supabase key. Desktop sign-in is optional — the first-run
wizard's "Work locally" uses the app without an account; signing in
attributes cloud contributions to the real user. Book Capture gates every entry point on
sign-in (`HomeActivity`/`MainActivity` bounce to login), even though the
LAN ingest leg itself needs no account — #102 tracks LAN-without-account.
Signup is currently open (the schema says so where it locks `volumes`
writes down); invite-only contributor enforcement is TODO (#98).

## Where the docs live

- `README.md` — the parts, the workflow, running from source.
- `docs/modular-engine-architecture.md` — the proposed headless engine,
  KiCad-style workbenches, capability modules, generalized cultural-heritage
  model, and migration plan.
- `tools/README.md` — the whole explorer: every tab, data layout,
  index builders, checks, standalone CLIs.
- `desktop/README.md` — the Electron shell, installer, auto-update,
  database downloads.
- `android/BookCapture/README.md` — screens, voice flow, transports,
  the capture data path.
- `website/README.md` — the site's pages, the reader, fixtures,
  the anon-key grant list.
- `docs/releasing.md` — versioning, release standards, the pipeline,
  its secrets.
- `docs/cloud/` — `migrations/` (the authority on grants + RLS; run in
  order, `schema.sql` is the entry point), `auth_setup.md`,
  `r2_cors_setup.md`; `docs/cloud_capture_setup.md`
  for the phone→desktop pipeline.
- The website's Documentation page (`website/docs.html`,
  <https://maj-6.github.io/library-tool/docs.html>) — the illustrated
  user manual.
