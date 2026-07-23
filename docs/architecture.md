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
  `Idempotency-Key` values. Browser creation, normal metadata Save, attention,
  category assignment, and release-bundle selection use that durable command,
  preserve one operation identity across ambiguous responses, and chain undo
  and redo from committed receipt revisions. Loading the catalogue no longer
  writes inferred volume grouping. Verification/OCR workflow state remains a
  deliberately narrow compatibility mutation until its owning aggregate is
  extracted.
  Item commands accept an optional validation-only product profile after
  durable replay and before allocation or staging. Production installs the
  Flask-free WHL book profile with an injected category vocabulary, so direct
  CLI or future desktop clients receive the same domain rules as HTTP clients.
  The legacy WHL row revision, decode/encode, managed-state preservation, and
  restore codec likewise live in a reusable filesystem adapter; Flask retains
  only transport parsing and injected compatibility callbacks.
  Interactive PDF attachment, replacement, and detachment now use a separate
  representation command service with item-level and representation-level
  compare-and-swap, durable replay receipts, and atomic catalogue publication.
  The browser reaches it through `EngineClient.items`; command and query
  responses contain opaque representation identities, checksums, sizes, and
  `unchanged`/`drifted`/`missing`/`untracked` content state, never the adapter's
  local source token or private replay fingerprint. Attachment structurally
  validates and hashes one stable PDF handle, then records its filesystem
  identity; later stat/identity drift makes a referenced source unavailable
  until explicit replacement revalidates it. Legacy create, update, and direct
  undo-restore routes cannot write source fields, and compatibility projections
  containing local paths are `no-store`. Folder and page-rewrite compatibility
  workflows refresh source integrity through the same command service. The
  production adapter currently references local PDFs and retains their paths
  only in the transitional raw build record. The neutral command contract also
  models copied acquisition, but an owned-asset copier and transactional asset
  staging are not installed yet. Only the explicit `build-workbench` projection
  carries the old local build record.
  The engine also defines opaque, ordered canvas query contracts. A strict
  filesystem reader can project an explicitly persisted, representation-
  revision-bound item index while withholding private source positions and
  paths. It performs no preparation or ID generation during a read. The engine
  preparation command now requires exact replay and a monotonic private ledger
  that reserves retired IDs. Its recoverable filesystem adapter atomically
  publishes the query index, private identity ledger, and durable receipt. The
  reusable first-party graph composes query and preparation only as one
  optional `library.canvases` vertical. A closed private page-materialization
  record now binds exact asset bytes and page evidence to repository-minted
  random correlations, and publishes atomically with the ledger, index, and
  receipt. Identical assets reuse those correlations even when their
  representation revision changes; changed assets fail closed without minting
  or retiring IDs. Page hashes, object numbers, paths, and ordinals remain
  evidence or locators, never identity. An exact attached-PDF inspector now
  verifies an authority-supplied revision, size, and SHA-256, copies the source
  once, and parses only that immutable path-private snapshot. Its pypdf-6
  snapshot-geometry evidence is versioned and explicitly not a page-content
  reconciliation fingerprint. The optional attached-PDF composition factory
  remains off in the production WHL host until the exact asset authority,
  hostile-parser worker isolation, transport/client surface, and explicit
  changed-asset reconciliation command are installed.
  The transitional PDF-page trash path now records the exact post-delete
  SHA-256 and verifies it before restore. A different or rewritten PDF with
  the same page count is therefore refused, and older rows without lineage
  evidence are download-only. This closes the immediate silent-splice hazard;
  page delete/restore still needs to become an engine-owned source/canvas
  transaction before it can maintain stable canvas identities.
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
  Provider-backed OCR/region and translation generation are not yet fully
  migrated to these command boundaries. A separate immutable provider registry
  can now describe future layout, OCR, translation, image, embedding, and
  answer generators without importing a UI or provider runtime. Its public
  projection contains stable IDs/versions, exact capability refs, portable
  execution/media/language/limit traits, secret-presence status IDs, cached
  sanitized health, and explicit user/default selection. Command availability
  fails closed, validates health reasons in context, and never silently replaces
  an unhealthy user selection with a default. Engine assembly derives an
  immutable discovery service against exact active module capabilities, so a
  selected healthy provider still reports `command-not-installed` until a
  concrete module binds that command. The optional `library.providers` module
  and versioned `/api/v1/providers` resource are composed only when a host
  injects that complete registry and its side-effect-free cached probes;
  process-lifetime host bindings carry the same optional seam. Production
  injects none, so legacy generation is neither registered nor advertised as
  an engine capability. Item delete/restore now has a neutral
  dual-CAS, tombstone, receipt, replay, and coherent preflight contract plus a
  recoverable no-copy managed-tree move primitive. Its filesystem repository
  persists private raw-record envelopes, moves owned trees before publishing
  tombstone/receipt files, and publishes the catalogue last. The service is an
  optional capability-composed module; installing it disables the older
  catalogue-only delete authority. The production host installs that module
  through the reusable first-party composition package. Versioned preflight,
  delete, tombstone-list/detail, and restore resources are consumed through
  `EngineClient`; browser delete/undo/redo retains exact operation identity and
  compare-and-swap revisions across ambiguous failures. Item-scoped job starts,
  in-process cloud catalogue/entry sync, engine repositories, compatibility
  delete/restore routes, and remaining synchronous entry writers now join the
  same workspace isolation. A delete therefore either waits for a complete
  write and removes it, or wins and prevents that writer from recreating an
  orphan entry tree. Active lifecycle tombstones reserve their item identities,
  including case aliases, during direct creation and new-item `.lib` open. The
  narrow reservation reader remains active even if lifecycle commands are
  disabled, preserving optional-module state for a later reinstall. Historical
  catalogue-only Trash rows remain downloadable but are intentionally not
  restorable as aggregate items.
  This guarantee is currently local to the one authoritative sidecar and its
  `DATA_ROOT`. Lifecycle tombstones are not yet a replicated cloud event type,
  so a second independent host must not treat the ordinary last-write-wins
  build mirror as lifecycle authority.
  A separate revisioned text-layer aggregate contract now supplies bounded,
  deterministic documents with opaque ordered selectors, exact source pins,
  document/unit revisions, source freshness, provenance, conditional single
  and batch edits, and durable replay. Public mutation receipts contain no
  command fingerprint; a distinct immutable storage envelope owns replay
  evidence. A strict native filesystem repository now persists closed/versioned
  documents and global hashed replay envelopes through one recoverable
  transaction, re-derives stored revisions, rechecks source/item state at
  commit, and performs no lazy read writes. An optional first-party
  `library.text-layers` module now composes that repository as a distinct
  aggregate service and advertises read/edit capabilities only when explicitly
  bound. A separately imported Flask adapter and `EngineClient.textLayers` now
  provide versioned list, detail, pinned fixed-range unit reads, and
  conditional single-unit replacement resources whenever that service is
  present. Page requests require strong document/source pins and explicit
  one-based page/limit ranges over canonical order, so a pinned traversal
  cannot skip or duplicate units. Pages contain at most 256 complete units and
  8 MiB of canonical unit data, carry a page-specific strong ETag, and fail
  rather than truncate when one unit or the exact response envelope exceeds
  its bound.
  Reads perform no lazy writes; commands require exact idempotency, unit,
  source, and complete-provenance inputs. The transport caps mutation bodies at
  1 MiB and coherent detail projections at 16 MiB. The service remains absent
  from the production host, so no native file is created and no legacy OCR
  path changes. A deliberate migration from page-marked `ocr/*.txt` still
  remains before Replica, translation, or RAG can depend on it.
- **Desktop app** — the workbench: an Electron shell (`desktop/`) that
  spawns the Flask sidecar (`tools/whl_explorer/server.py`) on a loopback
  port. Each desktop launch now creates a 256-bit capability that is passed
  only through the child environment, consumed before the Flask application
  imports, and retained by the sidecar only as a digest. Exact Host, supplied
  Origin, request provenance, redirect tainting, API `no-store`, sandbox,
  navigation, permission, and resource-window policies prevent arbitrary
  renderer or remote-content access to authenticated APIs. The former
  same-origin remote HTML proxy is retired. Large PDF previews use bounded
  streaming with explicit authenticated resource-window fallback instead of
  unbounded renderer blobs. Paths split into two roots (`tools/libcommon.py`):
  **`APP_ROOT`**,
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
  per-capture `capture_reviews` revision stream,
  shared `events` feed, and the published `volumes` catalogue + its
  artifacts; the working-store tables (`builds`, `ia_catalog`,
  `corrections`, `taxonomy`, `books`) and the bounded
  `capture_book_metadata` phone projection mirror desktop state.

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
ingest as cloud sync — no internet on that leg. The same authenticated
listener exposes a bounded `POST /lan/metadata` exchange for desktop book
projections and additive review state, so LAN-only capture IDs never need a
Supabase `captures` row. The phone records each delivered entry's transport;
later metadata sync follows that entry marker instead of the current global
setting. LAN capture requests have total, photo-count, and per-photo limits.
The desktop stream identity is written atomically; its compact, capped
fingerprint ledger rotates the identity after ledger loss, and timestamp-aware
phone merges recover from an older restored revision without accepting delayed
stale responses.

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
- `capture_book_metadata`: owner-only read of desktop-authored status for a
  retained capture; authenticated clients cannot write it. `capture_reviews`
  is owner-readable and permits authenticated writes only to the attention,
  reason, and needs-review columns. A trigger derives ownership from the
  capture and advances the server revision/timestamp on every write. Both
  tables revoke PUBLIC access explicitly, bound JSON/reason sizes by their
  uncompressed representation, and use in-place revisions/tombstones rather
  than exposing DELETE to clients or the desktop service path. Desktop
  round-trip code requires both a signed-in capture credential and a service
  credential for the same project: it scopes IDs with the former before any
  exact-ID service read/write. Metadata rows use revision compare-and-set and a
  build/manual/tombstone source vector to prevent stale desktops from erasing
  richer projections.
- `events` (the shared activity feed): read for any signed-in user;
  append-only insert, and `actor_id` must be the writer's own uid.
- `profiles`: readable by signed-in users only (contributor names are not
  for the open internet), writable only for your own row.
- The working-store sync tables (`builds`, `ia_catalog`, `corrections`,
  `taxonomy`) and the `books` mirror are **not** contributor-writable:
  RLS is on with no policy, so only `service_role` reaches them.

**PER-USER SECRET**:

- Desktop provider credentials (AI/OCR keys, Supabase owner/custom-project
  keys, R2 credentials, and the Google service-account path) are production-
  bound to the Windows current-user DPAPI repository in
  `DATA_ROOT/output/secrets.dpapi`. Its one closed, versioned atomic envelope
  contains ciphertext, random status revisions, receipts, and authenticated
  replay evidence; corruption, the wrong Windows user, or an unsupported
  platform reports explicit degraded health and never selects plaintext
  fallback.
- Startup completes the legacy cutover before the engine session, listeners,
  or workers become visible. Values from `client_state.json` and the retired
  `secrets.json` are committed, the repository is reconstructed, and every
  value is exactly leased and verified before either plaintext source is
  sanitized. A failed commit/reopen/verification preserves the legacy sources
  for an idempotent retry; no plaintext backup is created. The renderer also
  isolates credentials left in a pre-cutover `localStorage` cache before any
  settings synchronization, imports each through the protected CAS API, and
  scrubs it only after a confirmed protected write. A failed or ambiguous
  import remains locally retryable and never enters `state.settings` or client
  state.
- `library.secrets` publishes only fixed masked status and idempotent CAS
  replace/clear. The renderer uses `/api/v1/secrets` through `EngineClient` and
  retains status, masks, and revisions only; the former plaintext
  `/api/secrets` resource returns `410`. The repository and credential lease
  remain sidecar-private. OCR, AI/image generation, embeddings, Google Sheets,
  Supabase auth/owner sync, R2, profile sync, and capture paths acquire only
  the required credential inside their provider execution and scrub temporary
  configs afterward.
- The signed-in session/refresh tokens in `output/auth_session.json` and the
  LAN pairing token in `DATA_ROOT/lan_token.txt` remain separate future
  security boundaries, not provider API keys. The LAN token is not written to
  the application log.
- Bring-your-own Mistral/DeepSeek keys shared between phone and desktop
  live in the `profile_secrets` table — a separate table, not a
  `profiles` column, readable and writable by exactly one user
  (`id = auth.uid()`). The desktop's Mistral cache carries explicit account
  ownership and a redacted write-ahead sync record next to the DPAPI vault.
  Vault revision recovery makes local mutation plus pending-cloud intent
  crash-safe; exact replays cannot transfer ownership. Logout and account
  changes hide and deny another account's cache, and a prior owner's unsynced
  change blocks takeover until it is reconciled. A key entered while signed
  out (or migrated without trustworthy ownership) remains an unowned
  local-only key: usable only while signed out and never uploaded
  automatically.

**OWNER-ONLY** (never on a client, never in the repo):

- The Supabase `service_role` key — publishing, working-store sync, and
  maintenance; bypasses RLS. The desktop keeps it in the protected store;
  standalone owner tools accept it only through `SUPABASE_KEY` for that
  process and never recover it from UI state.
- R2 write credentials (`r2KeyId`/`r2Secret` in the secrets store).
- The Google Sheets service-account JSON key (Settings > Credentials;
  no credentials exist yet, the sync is TODO-verify).
- The release pipeline secrets on the public repo:
  `SUPABASE_SERVICE_ROLE_KEY`, the Android keystore set, and
  `WIN_CSC_LINK_B64`/`WIN_CSC_KEY_PASSWORD` (see `docs/releasing.md`).
- The Supabase dashboard / SQL editor, where the migrations are applied,
  in order.

Standalone maintenance scripts use the environment-only matrix in
`tools/README.md`; they do not import the desktop vault or inspect
`client_state.json`/`secrets.json`. `tools/worktree.py --seed` copies useful
nonsecret work state but removes every registered legacy credential field and
never copies either secret-store file.

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
