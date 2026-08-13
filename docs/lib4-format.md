# `.lib4` Living Edition package specification (`lib/4`, profile `living-edition/1.0`)

Status: prototype normative specification, August 2026.

Media type: `application/vnd.world-herb-library.lib4+zip`.

Filename extension: `.lib4`.

Normative schemas:

- `schemas/lib4-manifest.schema.json`;
- `schemas/lib4-layer.schema.json`;
- `schemas/lib4-catalog.schema.json`;
- `schemas/lib4-generation-receipt.schema.json`; and
- `schemas/lib4-retrieval-record.schema.json`.

Normative terms `MUST`, `MUST NOT`, `SHOULD`, `SHOULD NOT`, and `MAY` have their
usual RFC 2119 meanings. JSON Schema checks shape; the reference validator in
`tools/living_edition/lib4.py` also enforces graph integrity, review rules,
stable locators, and archive safety. A conforming producer passes both.

This document defines the package. The complementary
`docs/lib4-llm-generation-and-retrieval.md` defines how another LLM or pipeline
can create a useful, honestly incomplete package from PDF, image, text, audio,
video, or born-digital sources.

## 1. Purpose and boundaries

`lib/4` is the Living Edition profile in Library Tool's `.lib` major-version
lineage. It is designed for:

- ancient manuscripts, papyri, codices, inscriptions, and scrolls;
- early printed books and annotated copies;
- modern monographs, periodicals, journal articles, and reference works;
- plate books, maps, ephemera, mixed-media collections, audio, video, and
  born-digital and generic digital-document material;
- multiple competing OCR, transcription, translation, layout, entity,
  commentary, and knowledge layers;
- human correction, review, region drawing, guided reprocessing, releases, and
  durable citation; and
- loss-aware ingestion into search, vector databases, graphs, and RAG systems.

A `.lib4` file is a deterministic, checksummed ZIP projection. It is not a
mutable workbench database, a credential store, or an executable plug-in. It
may contain all required evidence, only metadata and stable external resource
references, or a deliberate mixture.

The format separates five concerns that must not collapse into one record:

1. source evidence and its alternate locations;
2. logical and physical structure;
3. revisioned editorial/analytical layers;
4. catalog assertions and their discovery projection; and
5. publication and retrieval projections pinned to exact revisions.

Incomplete work is normal. A failed OCR call, proposed category, uncertain
date, missing page, or pending translation is represented explicitly. Absence
must not be disguised as success, and machine output must not acquire human
approval by implication.

## 2. Package layout

```text
edition.lib4
├── manifest.json
├── INSTRUCTIONS.md
├── checksums.sha256
├── schemas/
│   ├── lib4-manifest.schema.json
│   ├── lib4-layer.schema.json
│   ├── lib4-catalog.schema.json
│   ├── lib4-generation-receipt.schema.json
│   └── lib4-retrieval-record.schema.json
├── sources/                         # optional embedded originals
├── assets/                          # optional images/media/derivatives
├── layers/<layer-id>/<revision>.json
├── metadata/                        # optional receipts/catalog serializations
├── authority-snapshots/             # optional immutable authority exports
├── retrieval/                       # optional JSONL/chunks/index derivatives
└── extensions/                      # declared extension resources
```

Every ZIP member is a regular file. Directory entries, symbolic links,
encryption, duplicate paths, absolute paths, drive paths, backslashes, `.` and
`..` segments, control characters, and undeclared members are forbidden.

Every member except `checksums.sha256` appears exactly once in that file:

```text
<64 lowercase hex digits><two spaces><portable member path>
```

Every non-generated embedded member is also owned by exactly one resource
location. A resource may have at most one embedded location and multiple
ordered external alternatives. Two resource records must not claim the same
embedded member.

The reference sealer fixes member order, timestamps, modes, JSON formatting,
and compression choices. Identical semantic input produces identical bytes.
Semantic timestamps are supplied data and are never inferred from filesystem
modification times.

### 2.1 Required self-description

`INSTRUCTIONS.md` teaches a generic agent the preservation, identity,
provenance, review, release, retrieval, and resealing rules. The package also
contains every schema named above. These copies are descriptive artifacts for
offline tools; the `lib/4` profile and reference semantic checks remain the
authority when a schema and an invariant disagree.

Readers MUST NOT execute code from the archive or load renderer code named by
an extension. Renderer hints are data consumed only by trusted application
registrations.

### 2.2 Safety caps

The reference profile caps a package at 4 GiB compressed, 8 GiB inflated,
50,000 members, 512 MiB per member, 64 MiB per JSON member, 128 levels of JSON
nesting, a 500:1 compression ratio, and 128 KiB per `ext` object. Implementations
may impose lower advertised limits. A writer needing larger evidence SHOULD
store it externally and provide integrity plus availability metadata.

## 3. Manifest identity

`manifest.json` begins:

```json
{
  "$schema": "https://worldherblibrary.org/schemas/lib4-manifest-1.0.json",
  "format": "lib/4",
  "profile": "living-edition",
  "profile_version": "1.0",
  "package_id": "herbal-yale-ms-18",
  "package_revision": "edition-2026-08-12-r1",
  "created_at": "2026-08-12T18:00:00Z",
  "generator": {},
  "catalog": {},
  "source_material": [],
  "structures": [],
  "canvases": [],
  "resources": [],
  "layers": [],
  "releases": [],
  "registries": {},
  "capabilities": [],
  "ext": {}
}
```

`package_id` names the continuing Living Edition. `package_revision` names
this immutable package graph. Rebuilding corrected content creates a new
package revision even if the logical edition retains its identity.

`generator` is an actor object with type, stable ID, label, optional version
and URI, and extension data. A model actor includes the exact model/service
identity and version where the provider supplies it. “AI generated” is not
sufficient provenance.

`profile_version` evolves independently inside `lib/4`. Section 13 defines
compatibility.

## 4. Catalog metadata

`manifest.catalog` is a complete inline `lib4-catalog/1.0` object. It exists
inline so an offline library browser can generate a catalog entry without
fetching another resource. An optional `catalog_resource_id` may identify a
richer or alternate serialization, but it never replaces or contradicts the
inline graph.

The catalog model distinguishes work, expression, manifestation, item,
representation, and component entities. It stores names, identifiers,
contributors, dates, languages, subjects, categories, genres, repository
data, and descriptive fields as immutable, reified statements. Every statement
has:

- subject and property IDs;
- a typed value;
- qualifiers and certainty;
- proposed, accepted, rejected, superseded, or tombstoned state;
- source/evidence locators;
- actor, method, model/service detail, time, and confidence; and
- optional revision lineage.

This graph allows an initial LLM classification and a later human or engine
correction to coexist. A correction creates a new statement and marks lineage;
it does not rewrite the historical assertion.

`entry_projection` selects accepted statements for the terse online catalog
entry. The projection reports each required discovery field as present,
unknown, disputed, not applicable, or missing. A partial draft remains valid
when its record completion and missing fields tell the truth. Dates may use an
explicit unknown typed value; a guessed year is not an acceptable substitute.

Rights and access are separate policy graphs:

- a rights policy describes copyright/license basis and allowed uses;
- an access policy describes who may discover, read, index, or embed content;
- resources reference both policies by ID; and
- the validator requires these IDs to resolve in the inline catalog.

The catalog schema and generation guide give the full fields, projection
requirements, and crosswalk advice for DCMI, MODS, Schema.org, BIBFRAME, and
IIIF Presentation. A crosswalk is an export, not the canonical assertion store.

## 5. Resources and locations

A resource is one logical byte sequence or service-delivered representation:

```json
{
  "id": "canvas-image-f001r",
  "role": "canvas-image",
  "media_type": "image/jpeg",
  "byte_length": 1839274,
  "integrity": {
    "state": "verified",
    "algorithm": "sha256",
    "digest": "<64 lowercase hex digits>",
    "reason": null
  },
  "locations": [
    {
      "type": "embedded",
      "member": "assets/pages/f001r.jpg",
      "priority": 0,
      "ext": {}
    },
    {
      "type": "iiif",
      "uri": "https://iiif.example.org/item/f001r/full/max/0/default.jpg",
      "service": "https://iiif.example.org/item/f001r",
      "priority": 1,
      "ext": {}
    }
  ],
  "availability": {
    "state": "available",
    "checked_at": "2026-08-12T18:00:00Z",
    "cache": "allow",
    "offline": "embedded",
    "ext": {}
  },
  "retrieval_policy": {
    "mode": "on-demand",
    "authentication": "none",
    "max_bytes": 20000000,
    "ext": {}
  },
  "rights_policy_id": "rights-source-images",
  "access_policy_id": "access-public",
  "provenance": {},
  "ext": {}
}
```

### 5.1 Embedded and external resources

Location types are:

- `embedded`: an archive member;
- `http`: a stable HTTP(S) object URL;
- `s3`: a stable `s3://bucket/key` identity; and
- `iiif`: a stable IIIF Image API result with optional service root.

Priority is unique within one resource; lower values are preferred. A reader
chooses the first usable location compatible with rights, access, retrieval,
cache, network, and media support. An embedded location is not required.

Remote URLs MUST NOT contain query strings, fragments, usernames, passwords,
presigned tokens, temporary signatures, cookies, authorization headers, or
credentials. Authentication is application-managed or user-mediated outside
the archive. S3 references identify bucket/key only. A future locator type is
namespaced and declared in an extension; it must not smuggle a private local
path into `ext`.

### 5.2 Integrity and availability

Embedded resources always have `verified` SHA-256 integrity and byte length;
the sealer calculates them. Remote resources SHOULD do so. If the publisher
cannot obtain stable bytes or a digest, integrity is `unverified` or
`unavailable`, algorithm/digest are null, and `reason` explains why. The state
must never imply verification that was not performed.

`availability.state` records the last known disposition, not a promise.
`checked_at` is optional. Cache policy is `allow`, `require`, or `forbid`;
offline behavior is `embedded`, `use-alternate`, `metadata-only`, or
`unavailable`.

`retrieval_policy` prevents a library browser, validator, or RAG ingestion job
from turning a locator into an uncontrolled fetch. `manual` and `on-demand`
require an explicit operation; `prefetch-allowed` permits policy-aware
prefetch; `embedded-only` refuses remote fallback; and `forbidden` prevents
content retrieval. `max_bytes` is a further bound. Validation and sealing never
fetch a remote location.

### 5.3 Core roles

Core roles are `source`, `canvas-image`, `layer`, `authority-snapshot`,
`catalog`, `generation-receipt`, `retrieval-records`, `embedding-index`, and
`asset`. New roles use namespaced registry IDs. Mutable SQLite databases,
credentials, work queues, UI state, caches, and local filesystem paths do not
belong in an archive. An immutable database export may be included only as a
declared, rights-governed resource with a stable schema and digest.

## 6. Material-neutral structure and canvases

`structures[]` is a revisioned ordered hierarchy. It represents physical or
logical organization without assuming pages:

- manuscript: work → volume → gathering → folio → page/side;
- early print: work → volume → gathering/signature → page → plate;
- journal: periodical → volume → issue → article → section/figure/table;
- reference work: work → volume → entry → subsection/plate;
- audiovisual: work → part → track → segment; and
- born digital: work → part → declared extension nodes.

Each node has stable ID/revision, type, label, parent, sibling order, target
URIs, open metadata, and `ext`. Parent references resolve; sibling order is
unique; cycles are invalid. Core types cover common cases. A genuinely new type
is namespaced and declared rather than encoded as another hard-coded property.

`canvases[]` names addressable spatial or timed surfaces. A canvas has stable
ID/revision, sequence, label, related structure IDs, optional image resource,
optional pixel dimensions, optional millisecond duration, and `ext`. A visual
page normally supplies image and dimensions. Audio/video may supply duration.
A text-only or metadata-only edition may have zero canvases.

A changed image or coordinate system receives a new canvas revision. Existing
selectors continue to pin the original revision; a remapping process writes a
new layer and records the transformation.

## 7. Stable targets and selectors

Internal target URIs use:

```text
lib4://package/{package_id}
lib4://package/{package_id}/structure/{structure_id}/revision/{revision}
lib4://package/{package_id}/canvas/{canvas_id}/revision/{revision}
lib4://package/{package_id}/layer/{layer_id}/revision/{revision}
lib4://package/{package_id}/layer/{layer_id}/revision/{revision}/item/{item_id}
lib4://package/{package_id}/release/{release_id}/revision/{revision}
lib4://package/{package_id}/retrieval/{chunk_set_id}/chunk/{chunk_id}
```

IDs are opaque portable identifiers; clients percent-encode URI segments.
Array indexes and filenames are never identity. A citation pins package/release
and every layer or canvas revision needed to reconstruct its evidence.

Core selectors are:

- `box`: normalized `x`, `y`, `width`, `height` in 0..1;
- `polygon`: 3–256 normalized points with non-zero area;
- `time-range`: ordered integer `start_ms` and `end_ms`; and
- `text-quote`: pinned layer/revision/item, character offsets, exact text,
  prefix/suffix context, and current/stale/ambiguous state.

Spatial selectors pin a canvas revision. Time selectors also pin the timed
canvas revision. An entity mention or knowledge assertion SHOULD combine
independent anchors where possible: region/spatial target, pinned text quote,
and exact source/citation URI. If anchors disagree, the status becomes stale or
ambiguous; software must not silently reattach by approximate text.

A custom selector has a namespaced declared type, coordinate space, revision,
data, and a core box/polygon/time fallback. This keeps unknown geometry visible
and navigable in generic readers.

## 8. Revisioned layers

`manifest.layers[]` inventories every historical revision. Stable layer ID
names an editorial strand; `revision` names one immutable state. At most one
revision per ID is current. `variant` distinguishes purposes such as diplomatic
OCR, normalized reading text, literal translation, public summary, or one
engine's layout analysis.

Core layer kinds are `region`, `transcription`, `translation`, `entity`,
`knowledge`, `commentary`, `notes`, `reprocessing`, `classification`, and
`retrieval`. Namespaced declared kinds remain valid and inspectable even when a
client lacks a specialized renderer.

The descriptor separates processing lifecycle from editorial status:

| Concern | Values |
|---|---|
| lifecycle | `planned`, `running`, `partial`, `failed`, `complete` |
| editorial status | `machine-draft`, `human-draft`, `under-review`, `approved`, `frozen`, `superseded` |

A planned or failed layer may have no content resource. Partial and complete
layers have content. Lifecycle includes numeric completeness, update time,
message, retryability, and `ext`. The full layer includes coverage counts,
target selection, omissions, and structured errors. Thus an initial service
failure is a useful, valid scheduling record rather than a malformed edition.

### 8.1 Common layer envelope

Every content layer conforms to `lib4-layer-1.0` and contains:

- stable ID, revision, kind, label, and optional language;
- lifecycle and editorial status matching its manifest descriptor;
- coverage scope, completed/total counts, targets, and omissions;
- provenance actor, exact generation time, method, run ID, parameters, and
  pinned sources;
- dependencies on exact layer revisions and named relations;
- chronological history events;
- review decisions applying to this exact revision;
- structured processing errors;
- kind-specific `data`; and
- bounded extension data.

A retry or better engine writes a new revision with new provenance and a
dependency/supersession link. It does not mutate the failed record or overwrite
an approved human reading.

### 8.2 Regions and reading order

A region layer stores stable regions, selectors, classifications, parentage,
named reading flows, and typed relations. Region classification is multi-facet:
the same polygon may be `layout:marginalia`, `hand:scribe-b`, and
`function:gloss`. User-defined type hierarchies belong in a declared controlled
vocabulary layer or extension resource. Marginalia, body, headers, decoration,
multiple hands, damage, erased text, columns, music, captions, advertisements,
and figures are not mutually exclusive global enums.

Named reading flows let body text, marginal notes, captions, parallel columns,
and interlinear material retain separate order. A renderer chooses or combines
flows intentionally. ZIP/array order is not reading order.

### 8.3 Text, translation, entities, notes, and knowledge

Transcription preserves source language and stated normalization policy.
Modernized text is a separate transcription/normalization variant. Translation
is another layer and pins its input. Multiple engines and human readings can
coexist.

Entity layers store mentions, not silent replacements in text. A mention links
stable targets to external or bundled authority assertions, records candidate
versus accepted state, and retains uncertainty. The mutable plant-entity POC
remains outside the package; an immutable snapshot may be a declared resource.

Notes, commentary, knowledge, classifications, and reprocessing directions use
stable target URIs so the same model can address book, structure, page, region,
text item, entity, or retrieval chunk scope. A reprocessing instruction records
requested outputs and constraints; execution produces a new machine revision
and a diff/provenance link.

## 9. Provenance and review

Every derived resource and layer names who or what created it, when, how, from
which pinned inputs, and with which parameters. Service/model provenance SHOULD
include provider, model, version/snapshot, endpoint class, prompt/template
digest, deterministic parameters where available, and run/receipt ID. Secrets
and request credentials remain external.

Provenance is not approval. Machines/services/importers may propose, classify,
compare, flag, and abstain. Only a named human actor may approve or reject
scholarly content. Approval applies to one exact revision. An approved/frozen
layer must be complete and have a human approval for that revision.

A human edit creates a human-draft revision. A machine may suggest a patch to
it but must not write a result that appears to retain the earlier human
approval. Review history and rejected alternatives remain auditable.

## 10. Releases and publication

A release pins exact catalog, layer, and resource revisions. States are draft,
candidate, published, and withdrawn. It has its own stable ID/revision,
creation actor/time, predecessor, citation, and publication policy.

A draft may include incomplete machine layers for preview. A published release:

- pins only complete approved/frozen scholarly layers;
- sets `approved_layers_only: true`;
- sets `include_machine_drafts: false`;
- pins the inline catalog revision and explicit resource IDs;
- carries a citation that does not depend on “latest”; and
- respects effective rights/access policies.

Withdrawal is state, not deletion. The release and its historical citations
remain addressable with an explanatory policy/status surface.

## 11. Retrieval and knowledge-engine profile

RAG compatibility is a first-class projection, not an invitation to flatten
the edition into untraceable prose. `manifest.retrieval` names a stable document
ID, citation URI template, and one or more chunk sets. Each chunk set pins:

- an exact retrieval layer revision;
- a declared `retrieval-records` resource containing JSONL/JSON records;
- `lib4-retrieval-record-1.0` as the record schema;
- the canonical content digest;
- an optional `embedding-index` resource; and
- open backend-neutral index hints.

Every retrieval record has stable chunk ID/revision, upsert/delete operation,
current/stale/tombstoned state, package/release identity, source targets and
selectors, exact layer pins, content by modality/language/trust class, entities,
categories, citation, provenance, rights, access, optional embeddings, and
staleness/tombstone information. The schema supports text, visual, and timed
material without pretending each modality is plain text.

Chunk boundaries SHOULD follow meaningful structures and reading flows, with
bounded overlap stored as explicit relationships. Each chunk keeps enough
anchors to open the source evidence. A retrieval system filters effective
access before search, carries rights constraints through results, distinguishes
machine draft from reviewed text, and cites the pinned record. It does not
return a vector as evidence.

Embedding model, version, dimensions, input digest, and storage reference are
metadata on a replaceable derivative. Re-embedding does not change the source
chunk identity when its canonical content is unchanged. Changed content creates
a chunk revision and a stale/delete/upsert event; withdrawn access produces a
tombstone/delete projection.

The detailed chunking, multimodal, multilingual, ACL, reindexing, and
retrieval-time safety protocol is in the LLM generation guide.

## 12. Open registries and extensions

The manifest registries cover layer kinds, resource roles, structure types,
material types, selector types, relation predicates, and capabilities. Core IDs
need not be redeclared. An extension ID starts `x-` or contains a namespace
colon and supplies label, description, optional schema URI, renderer hint, and
`ext`.

Unknown declared extensions are preserved. A generic client can verify their
bytes, list them, expose raw JSON/metadata, follow core fallback selectors, and
reseal without understanding specialized semantics. It must not discard them
or invent a renderer.

An `ext` object is bounded JSON. It may not contain credential-shaped keys,
private/local paths, API tokens, non-finite values, or executable code. An
extension needing a large payload declares a resource rather than filling
`ext` with an unbounded blob.

`capabilities[]` declares features actually used by this package, not every
feature a producer understands. A reader may decline an unsupported required
experience while still verifying and inventorying the package.

## 13. Versioning, compatibility, and migration

The marker is `format: "lib/4"`; the living-edition contract is
`profile/profile_version`. The file extension `.lib4` makes the incompatible
major version explicit and avoids claiming that older `.lib` readers can edit
it safely.

- A backward-compatible profile minor may add optional fields or core registry
  terms and retains the same major schema family.
- A profile change that alters required meaning, identity, selectors, integrity,
  review, release, or preservation rules requires a new supported profile
  version.
- An incompatible package/container change requires `lib/5` and `.lib5`.
- Readers validate exactly the versions they advertise. They may inspect an
  unknown additive profile read-only but must not rewrite or reseal it as a
  known version.

### 13.1 `.whled` compatibility

`.whled` (`whled/0.1`) was the exploratory Living Edition package. It remains
readable by `tools/living_edition/whled.py`. It is not silently treated as
`lib/4`. Migration is explicit and creates a receipt:

1. preserve package, catalog/edition, canvas, layer, region, and entity IDs;
2. map each embedded member to a `lib/4` resource and location;
3. map `whl://` targets to `lib4://package/...` and record both in an identity
   mapping extension/resource;
4. split processing lifecycle from editorial status;
5. turn authority snapshots and assets into policy-governed resources;
6. generate inline catalog assertions/projection without manufacturing missing
   facts;
7. pin releases and retrieval projections explicitly; and
8. validate and seal a new `.lib4`, retaining the source archive digest.

Migration is not lossless if the old package lacks rights/access policy,
catalog evidence, service/model detail, or stable identities. The receipt lists
every inferred, defaulted, omitted, or unresolved field.

### 13.2 `.lib` 1–3 compatibility

Existing `.lib` 1–3 importers remain the authority for their formats. A major
import creates a new `lib/4` graph; it does not edit an old archive in place.
Legacy page regions may map to normalized selectors; representations/artifacts
map to resources and layers; original provenance and digests remain pinned.
Fields that cannot be represented truthfully become generation-receipt issues
and review/reprocessing targets.

## 14. Validation and sealing

Build an authoring directory whose relative embedded files match the manifest:

```powershell
python tools/living_edition/build_lib4.py build `
  work/manifest.json work/edition.lib4 --resource-root work
```

Validate or inspect without fetching external resources:

```powershell
python tools/living_edition/build_lib4.py validate work/edition.lib4 --json
python tools/living_edition/build_lib4.py inspect work/edition.lib4
```

Build reads only embedded locations declared in the manifest. It fails if an
embedded payload is missing or an undeclared payload is offered. It computes
embedded byte lengths/digests, generates instructions and schema copies,
performs semantic checks, writes a deterministic temporary ZIP, then atomically
replaces the requested destination. Existing output requires `--force`.

Validation checks at least:

1. ZIP paths, links, encryption, duplicates, bombs, sizes, and required files;
2. every archive checksum and embedded resource digest;
3. strict UTF-8 JSON with unique keys, finite numbers, and bounded nesting;
4. exact format/profile/schema markers;
5. unique IDs/revisions and resolvable structures, resources, policies,
   canvases, layers, releases, and retrieval records;
6. stable, credential-free remote locators and explicit retrieval behavior;
7. normalized geometry, time bounds, and revision pins;
8. layer lifecycle/completeness/content consistency;
9. human-only exact approvals and published release gates;
10. declared extensions and bounded safe `ext`; and
11. no undeclared archive members.

Availability checks, remote-byte verification, virus scanning, image decoding,
OCR quality scoring, catalog authority reconciliation, and scholarly review are
separate policy-controlled operations. A structurally valid package is not a
claim of scholarly correctness.

## 15. Prototype acceptance gates

The profile is ready to progress beyond prototype only when fixtures and tests
demonstrate:

1. one fully embedded edition, one remote-only edition, and one mixed edition;
2. manuscript, journal article, illustrated work, and timed-media structures;
3. box, polygon, text, and time selectors with stable round trips;
4. planned, partial, failed, machine draft, human draft, approved, and frozen
   layer histories;
5. multiple OCR/transcription/translation variants without overwriting;
6. catalog assertions, uncertainty, corrections, rights, access, and a usable
   discovery projection;
7. published release refusal for incomplete or unapproved layers;
8. retrieval JSONL that opens every result at its exact evidence and respects
   rights/access filters;
9. deterministic sealing and preservation of unknown declared extensions;
10. traversal, link, credential, signed-URL, undeclared-member, digest, and ZIP
    bomb rejection; and
11. explicit migration receipts from representative `.whled` and `.lib/3`
    packages.

The current code and schemas are a proof of concept. They define the intended
contract sufficiently for independent generators and reviewers, while leaving
room for tested profile evolution before claiming a stable archival standard.
