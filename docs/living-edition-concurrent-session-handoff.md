# Living Edition Studio concurrent-session and handoff protocol

Status: normative amendment candidate; effective with `studio-adoption-v1.1.1` for work
packages in the
[production build specification](living-edition-production-build-spec.md)
Protocol version: `1.0.1`
Coordination-ledger schema version: `1.0` (independent and unchanged)
Audience: S00 gate steward, package implementers, reviewers, and I30 integrator

This protocol turns the specification's ownership table and dependency DAG into
an executable multi-session workflow. It prevents two sessions from sharing a
working tree, moving a frozen baseline, editing the same path, inventing a
sibling dependency, or handing off uncommitted state.

The immutable `studio-adoption-v1.1.0` baseline remains the historical A00
adoption record. Protocol `1.0.1` changes only session-context routing and makes
`studio-adoption-v1.1.1` the required B00 base. It does not move or recreate the
earlier tag, alter its evidence, or activate any product package by itself.

## 0. Pre-adoption authorization

Before `studio-adoption-v1.1.0` existed, only A00 could use the protocol `1.0`
candidate, and only under an explicit repository-maintainer work order. That work order
MUST pin the externally selected candidate commit, the SHA-256 of this candidate
and the production specification at that commit, the exact local prototype tag
tuple, authoritative remote, A00 owner, branch, external worktree, and typed
lease entries. It also names any separately authorized publisher or remote
administrator.
A00 records that approval and both document digests in its bootstrap ledger
record. This narrow authority permits reconciliation and adoption preparation;
it does not activate B00 or any product package. All other protocol `1.0` provisions became
effective only from the adopted document bytes in `studio-adoption-v1.1.0`.
Changing either candidate after approval invalidates the work order and requires
new digests and renewed maintainer approval.

Unless a field explicitly declares another domain, every SHA-256 for a committed
repository path in this protocol, including candidate-document pins and the
external bootstrap-ledger digest, uses `git-blob-payload-sha256/1`: SHA-256 over
the raw Git blob payload at the externally pinned commit before checkout filters,
encoding conversion, or end-of-line conversion. Validators MUST read those bytes
through Git object plumbing such as `git cat-file blob <commit>:<path>` and MUST
NOT hash a working-tree path or filtered checkout bytes. A validator may use
`HEAD` only after proving that `HEAD` equals the externally pinned commit.

The work order and assignment receipt, which are external to the bootstrap
ledger bytes, pin the ledger path, commit, digest domain, and SHA-256. The ledger
MUST NOT contain its own digest, its future commit ID, or the future adoption tag
object. A00 records only facts already knowable when its commit is created. S00
records the accepted A00 commit, adoption tag object, and final bootstrap-ledger
digest in its first post-adoption coordination receipt without moving the
adoption tag.

### 0.1 Context-packet amendment

A01 is the one-time, repository-maintainer-authorized amendment that replaces
universal full-document ingestion with closed context packets. A01 owns only
the two normative documents, branches from `studio-adoption-v1.1.0`, and returns
a clean lease-bound commit to S00. S00 separately owns the context schema,
profiles, pre-GB validator, ledger records, assembly, and immutable
`studio-adoption-v1.1.1` tag.
No B00 session may be activated or started until the new local and remote tag
objects and peeled commits match and the context-packet artifacts validate.
A01 itself is governed by adopted protocol `1.0` and its pinned two-file work
order; protocol `1.0.1` is not applied retroactively to its own amendment work.
It is not a `studio-context-packet/1` session. Its approved work order pins the
base and exact two paths; its S00 assignment/start receipts pin the protected
coordination ref and protection receipt, active lease, and start checks; its
return and review receipts pin both complete document Git blobs and final
checks. Protocol `1.0.1` context-packet requirements begin with B00.

## 1. Non-negotiable session model

One implementation session has exactly:

- one work-package ID;
- one filesystem-safe session ID;
- one Git branch;
- one external Git worktree;
- one immutable base tag and commit;
- for B00 and later, one immutable, digest-pinned context-packet revision;
- one active, non-overlapping access lease whose mode is fixed by the assignment;
- one set of reserved public IDs;
- one accountable package owner.

From B00 onward, implementation, integration, and review sessions start without inherited
conversation history. Prior discussion becomes authoritative only when captured
in pinned repository bytes, a coordination receipt, or the active context
packet. A link, directory, earlier packet, or referenced document does not add
implicit context.

The shared primary checkout's content is read-only while concurrent sessions are
active. An implementation session MUST NOT work in it. S00 alone may use it for
approved common-Git-directory administration—listing/adding/removing registered
worktrees and read-only ref inspection—without editing or committing its files.
Every reviewer has a separate active session, branch, worktree, and nonempty
review-scope lease. Its assignment sets `access_mode` to `read-only-review`; the
lease entries constrain what the reviewer may observe and grant zero repository
writes. An implementer or integrator assignment sets `access_mode` to
`write-lease`. A reviewer who must make a correction requires a distinct
write-lease assignment rather than mutation of the review session.

Handoffs contain commits, not working-tree patches. A branch name is never a
baseline: every brief and receipt records the peeled commit and required
digests.

## 2. Roles

### S00 — gate steward

S00 is the sole writer of `coordination/**` and owns:

- gate verification and annotated baseline tags;
- worktree/session naming and lease assignment;
- context-profile maintenance and implementer/reviewer packet issuance;
- progressive-disclosure decisions and packet-revision receipts;
- public ID reservations;
- the session ledger and state transitions;
- accepted-commit receipts and merge ordering;
- immutable foundation/integration baselines;
- routing blockers to the correct owning package.

S00 does not implement product behavior or repair semantic package conflicts.

A00 has the sole pre-S00 exception: it creates the coordination schema and one
bootstrap ledger record in its leased adoption branch. After S00 accepts and
tags that commit as `studio-adoption-v1.1.0`, S00 creates its own external
`studio-s00-coordination` worktree from the adoption tag, publishes the protected
`refs/heads/studio-s00-coordination` ref, and becomes the only writer of
`coordination/**`. S00 ledger commits are merged into immutable baselines by the
assembly procedure in section 12; implementer branches never edit them.

For this protocol, “protected” means a remote-enforced policy scoped to the exact
`refs/heads/studio-s00-coordination` ref that rejects force updates and deletion,
permits ordinary non-force direct updates only by the explicitly authorized S00
publisher principal or principals, and applies to repository administrators as
well as ordinary writers. A local convention or an unprotected published branch
does not qualify. Before B00 starts, S00 reads the effective provider policy back
through the provider API and records a `coordination-ref-protection` receipt with
provider, repository, exact ref/pattern, rule or ruleset ID, authorized
principals, force-update/deletion settings, observation time, and SHA-256 of the
`github-coordination-protection-projection/1` bytes described below. Creating or
changing this policy is a separate remote administration action and requires
explicit authority.

For the authoritative user-owned GitHub repository, classic branch protection
alone is insufficient because it cannot restrict pushes to a principal
allowlist. The required policy is two active repository rulesets with the exact
ref condition `refs/heads/studio-s00-coordination`: an integrity ruleset with no
bypass actors and `deletion` plus `non_fast_forward` rules, and a writer-gate
ruleset with `creation` plus `update` rules whose only bypass actor or actors are
the authorized S00 GitHub user or installation principals. Layering is
mandatory: S00 may bypass the writer gate for ordinary direct updates but cannot
bypass the integrity rules. A different provider mechanism is acceptable only
if its readback proves equivalent constraints.

`github-coordination-protection-projection/1` is a JSON object containing only
`schema`, repository `owner` and `name`, exact `ref`, and a `rulesets` array. Each
ruleset entry contains provider integer `id`, `name`, `target`, `enforcement`,
the exact included/excluded ref arrays, relevant rule `type` and parameters, and
each bypass actor's `actor_type`, integer `actor_id`, and `bypass_mode`. Rulesets
sort by integer ID, rules by type, ref arrays lexicographically, and bypass actors
by `(actor_type, actor_id, bypass_mode)`. Volatile timestamps, URLs, node IDs,
transport metadata, and unrelated provider fields are excluded. The receipt's
`digest_domain` is `rfc8785-jcs-sha256/1`: SHA-256 over the UTF-8 bytes of the
projection serialized with RFC 8785 JSON Canonicalization Scheme. For this
GitHub policy the sole writer-gate bypass mode is `always`; the integrity
ruleset has no bypass actors. The receipt carries both projection schema and
digest domain, so API key order or transport changes cannot change its identity.

### Package implementer

The implementer writes only paths covered by its typed lease entries.

It targets every phase-applicable pinned contract/fixture, uses declared
ports/fakes when available, runs isolated acceptance commands, and returns a
clean committed branch plus evidence.

### Reviewer/validator

A reviewer verifies the diff, ownership, public surface, and receipts from a
separate closed reviewer packet and active `read-only-review` scope lease. Its
nonempty lease entries are observation bounds, not write authority. It inherits
neither the implementer's conversation nor its caches and does not make
opportunistic fixes in the implementer's branch. Findings return to the package
owner unless S00 issues a separate corrective `write-lease` session.

### I30 — integrator

I30 composes accepted commits in a clean integration worktree. It may resolve
mechanical conflicts only inside I30-owned paths. Contract, domain, fixture, or
sibling-package semantic conflicts return to their owner.

## 3. Gate and baseline order

Sessions branch only from one of these immutable points:

| Session       | Required base                                                      |
| ------------- | ------------------------------------------------------------------ |
| A00           | `living-edition-viewer-v0.1.1` (reference-reconciliation use only) |
| A01           | `studio-adoption-v1.1.0` (context-amendment use only)              |
| B00           | `studio-adoption-v1.1.1`                                           |
| C00           | `studio-bootstrap-v1.0.0`                                          |
| T01           | `studio-contracts-v1.0.0`                                          |
| E10, D20, U20 | `studio-fixtures-v1.0.0`                                           |
| E11–E21       | `studio-engine-foundation-v1.0.0`                                  |
| U21–U27       | `studio-renderer-foundation-v1.0.0`                                |
| I30           | `studio-composition-input-v1.0.0`                                  |

Frozen-input pins are phase-aware:

| Session phase              | Contract input pin                               | Fixture input pin                               |
| -------------------------- | ------------------------------------------------ | ----------------------------------------------- |
| A00                        | `not-applicable` — contract tag does not exist   | `not-applicable` — fixture tag does not exist   |
| A01                        | `not-applicable` — contract tag does not exist   | `not-applicable` — fixture tag does not exist   |
| B00                        | `not-applicable` — contract tag does not exist   | `not-applicable` — fixture tag does not exist   |
| C00                        | `not-applicable` — C00 produces the contract tag | `not-applicable` — fixture tag does not exist   |
| T01                        | required: `studio-contracts-v1.0.0`              | `not-applicable` — T01 produces the fixture tag |
| E10–E21, D20, U20–U27, I30 | required: contract tag and lock digest           | required: fixture tag and lock digest           |

In a B00-and-later context packet, every required phase pin includes matching
local/remote annotated tag object, peeled commit, tree, verification status, and
the exact lock path/blob OID/`git-blob-payload-sha256/1` digest. Contract pins use
`contracts/contracts.lock.json`; fixture pins use
`fixtures/fixtures.lock.json`. The lock's containing commit MUST equal the
peeled phase-tag commit.

The packet, ledger, start check, and return receipt MUST carry either the exact
required pin or the literal `not-applicable` plus the reason from this table.
They MUST NOT invent a placeholder tag or digest for a phase that produces that
artifact.

For `studio-context-profiles/1`, the exact B00 contract and fixture reasons are
`contract tag does not exist` and `fixture tag does not exist`; C00's exact
contract and fixture reasons are `C00 produces the contract tag` and
`fixture tag does not exist`; and T01's exact fixture reason is
`T01 produces the fixture tag`. The profile stores each phase expectation with
`not_applicable_reason`; required pins store `null`. A `producer` expectation is
represented in the packet and ledger by `status: not-applicable` plus that exact
producer reason. The validator compares these strings byte-for-byte among the
profile, packet, and ledger; paraphrases and punctuation changes fail.

A00's live-coordination input is likewise `not-applicable`: its approved
bootstrap ledger path and SHA-256 replace the protected-ref fields. B00 and every
later session MUST pin and verify the protected coordination ref.

`living-edition-viewer-v0.1.1` is a reference artifact and the one-time A00
reconciliation base; it is never a B00 or product-implementation base. Its tag
object, peeled commit, repository tree, and prototype subtree MUST match section
3 of the production specification. The tag MUST NOT be recreated or moved.

A00 has one start-verification exception for the audited current state: the
remote prototype ref may be recorded as `remote-baseline-missing` while the exact
local tag object/peeled commit/tree/subtree tuple is verified. A00 may then edit
only its leased reconciliation paths while an explicitly authorized publisher
pushes the existing tag object by refspec. No session may run `git tag` to repair
the omission. A00 cannot enter review or be accepted, and S00 cannot publish the
adoption tag, until the remote tag object and peeled ref match exactly. A remote
ref with any different object is `baseline-mismatch`, not this exception.

No session may branch from prose such as “C00 + T01 + E10.” S00 first creates a
commit containing the accepted prerequisites and annotates it with the declared
baseline tag. Replacing a baseline requires a new versioned tag and a
supersession record; frozen tags are never force-moved.

For every baseline, S00 records the authoritative remote URL, annotated tag
object, peeled commit, and tree, then verifies the remote tag object and peeled
ref exactly. Recreating an annotated tag at the same commit changes its tag
object and fails verification.

## 4. Worktree and branch creation

Use lowercase filesystem-safe names:

```text
session: U23-001
branch:  studio-u23-001-edition-canvas
```

Before creating anything, S00 inspects registered worktrees:

```powershell
git worktree list --porcelain
python tools/worktree.py list
```

These commands MUST run from the canonical primary checkout that owns the common
Git directory, never from S00's or an implementer's external worktree. For every
`studio-*` session, `--base` is mandatory and MUST name the already-verified
annotated tag from the assignment; the helper's mutable default is forbidden.
S00 compares the base tag object with the ledger before invoking the helper.

S00 then creates the session from its exact base:

```powershell
python tools/worktree.py add studio-u23-001-edition-canvas --base studio-renderer-foundation-v1.0.0
```

The helper creates the worktree outside the repository and gives it private
`.wt/data` runtime state. Do not use `--seed` unless the brief explicitly
requires copied nonsecret legacy state. Never copy credentials, a live database,
`node_modules`, `.venv`, caches, or another session's `.wt` directory.

Names are never reused implicitly. Do not auto-prune or remove another session's
worktree. Cleanup occurs only after S00 records acceptance/integration and
verifies the exact target.

## 5. Start verification

The implementer performs these checks before editing. The example is for a
post-adoption session; A00 omits the protected-ref commands and uses section 0's
approved bootstrap-ledger digest:

```powershell
$StudioRemoteUrl = "https://github.com/maj-6/library-tool.git"
$StudioBaseTag = "studio-renderer-foundation-v1.0.0"
$StudioBaseCommit = git rev-parse "${StudioBaseTag}^{commit}"
$StudioLedgerRef = "refs/heads/studio-s00-coordination"
$StudioAssignmentLedgerCommit = "<commit-from-brief>"
$StudioSessionLedgerRef = "refs/studio-sessions/u23-001/coordination"
git status --porcelain
git remote get-url origin
git rev-parse "refs/tags/${StudioBaseTag}"
git rev-parse HEAD
git rev-parse "${StudioBaseTag}^{tree}"
git cat-file -t $StudioBaseTag
git ls-remote --tags $StudioRemoteUrl "refs/tags/${StudioBaseTag}" "refs/tags/${StudioBaseTag}^{}"
git ls-remote $StudioRemoteUrl $StudioLedgerRef
git fetch --no-tags $StudioRemoteUrl "${StudioLedgerRef}:${StudioSessionLedgerRef}"
git merge-base --is-ancestor $StudioAssignmentLedgerCommit $StudioSessionLedgerRef
git merge-base --is-ancestor $StudioBaseCommit HEAD
git tag --points-at HEAD
```

The assignment pins the protected coordination ref, a minimum ledger commit, and
the SHA-256 of `coordination/studio-ledger.json` at that commit. At start, the
implementer records the current remote ledger commit/digest, validates it against
`coordination/ledger.schema.json`, proves the assignment commit is its ancestor,
and verifies that the assigned lease is active and nonoverlapping under section 6.

After GB, `tools/studio/**` supplies this exact check. For A01, S00 supplies the
pinned protocol-1.0 ledger and start checks. For B00, S00 supplies the committed
`coordination/validate_context_packet.py` pre-GB validator plus the pinned ledger
checks. A00 uses its pre-adoption bootstrap record and has no concurrent
implementation lease. Each session fetches into its own
`refs/studio-sessions/<session>/coordination`
namespace so concurrent checks do not contend on one shared local ref; S00
removes that exact ref only during verified cleanup for that session.

Required outcomes:

- status is empty;
- the authoritative remote URL equals the brief;
- the local and remote tag object IDs equal the brief exactly, and the remote
  peeled ref equals the brief's base commit, except for A00's temporary
  `remote-baseline-missing` state described above;
- `HEAD` equals the brief's base commit and tree at session start;
- the base is an annotated tag object, not a recreated or lightweight tag;
- the base commit is an ancestor of every later handoff HEAD;
- for A01 and later, the protected coordination ref is at or after the
  assignment's ledger commit, its current ledger digest is recorded, and the
  lease remains active and nonoverlapping; A00 instead verifies its approved
  bootstrap ledger digest;
- for A01 and later, the brief's `coordination-ref-protection` receipt matches a
  fresh provider-policy readback for the exact coordination ref, including the
  authorized S00 principals and force-update/deletion prohibitions; the verifier
  rebuilds `github-coordination-protection-projection/1` and compares its
  `rfc8785-jcs-sha256/1` digest;
- every phase-required contract and fixture file/digest matches the assignment
  packet, and every nonapplicable pin has the declared phase reason;
- for B00 and later, before any selected semantic context is presented, the packet manifest,
  source blobs, selectors, selected digests, and UTF-8 byte budget validate;
- each typed lease entry exists or the brief explicitly authorizes creation at
  that exact path;
- no other active lease overlaps after path normalization.

If any result differs, stop and report `baseline-mismatch`; do not rebase,
silently select another branch, or copy files from another worktree.

## 6. Leases and ownership

`studio-workspace.json` is the machine-readable path-ownership source. The S00
ledger narrows that ownership to an active session lease.

It becomes authoritative only when committed in `studio-bootstrap-v1.0.0` and
validated against section 18, including phase transfers. A provisional copy in
another worktree does not grant ownership or prove GB.

Rules:

- at most one active lease covers a normalized path;
- a work package may be subdivided only into explicitly disjoint typed lease
  entries with one named package lead;
- package-local code, tests, fixtures, snapshots, styles, strings, manifests,
  and entry points stay with that package;
- T01 owns shared fixtures; modules own consumer fixtures beneath module paths;
- C00 alone owns contracts, generated clients/validators, and the contract lock;
- feature sessions never edit root manifests/locks, composition lists,
  installers, release workflows, the frozen prototype, or legacy migration
  sources;
- no session edits another worktree, branch, process, database, cache, or
  dependency directory;
- cross-package relative imports and private implementation imports are
  forbidden.

There is no partial-file lease. B00 or I30 may receive the root `.gitignore`
only as an exclusive `exact-file` lease and must preserve bytes outside the
specification's named Studio block; the same rule applies to any future shared
file with constrained edits.

Before work, S00 reserves every new schema, operation, error, event, capability,
route, migration, renderer contribution, and workflow-step ID. A session stops
on an unreserved or conflicting public ID.

Ledger lease paths are not arbitrary globs. S00 converts the ownership table and
brief into typed `exact-file` or `subtree` entries before assignment; prose
wildcards are expanded into named entries, and a new file needs a reservation
before creation. Canonical lease paths:

- are repository-relative POSIX paths made only from printable ASCII path
  segments matching `[A-Za-z0-9._@+-]+`, with `/` separators and no glob
  metacharacters;
- reject a leading separator, drive/UNC form, NUL, repeated separator, empty,
  `.` or `..` segment, and a segment ending in a Windows space or dot;
- preserve the repository's exact spelling while using ASCII lowercase as the
  Windows collision key;
- resolve inside the assigned repository root, with every existing component
  inspected before work; a symlink, junction, or other reparse point at or below
  a leased root is forbidden.

Two entries overlap when their lowercase exact-file keys match, an exact file is
inside either subtree, or one subtree is equal to or an ancestor of the other at
a segment boundary. This deterministic test runs across all active leases and
reserved-but-not-yet-created paths. Filesystem glob expansion and string-prefix
tests are not ownership proofs.

## 7. Coordination ledger

Only S00 writes `coordination/studio-ledger.json`. Implementers send structured
updates; they do not resolve ledger conflicts themselves.

Minimum session record:

```json
{
  "session_id": "U23-001",
  "work_package": "U23",
  "accountable_owner": "<person-or-agent-id>",
  "lease_id": "lease-U23-001-01",
  "branch": "studio-u23-001-edition-canvas",
  "worktree": "../whl-worktrees/studio-u23-001-edition-canvas",
  "coordination_ref": "refs/heads/studio-s00-coordination",
  "authoritative_remote_url": "https://github.com/maj-6/library-tool.git",
  "base_tag": "studio-renderer-foundation-v1.0.0",
  "base_tag_object": "<40-hex-tag-object>",
  "remote_base_tag_object": "<same-40-hex-tag-object>",
  "baseline_verification_status": "verified",
  "base_commit": "<40-hex-commit>",
  "base_tree": "<40-hex-tree>",
  "contract_pin": {
    "status": "required",
    "tag": "studio-contracts-v1.0.0",
    "tag_object": "<40-hex-tag-object>",
    "lock_sha256": "<64-hex-digest>"
  },
  "fixture_pin": {
    "status": "required",
    "tag": "studio-fixtures-v1.0.0",
    "tag_object": "<40-hex-tag-object>",
    "lock_sha256": "<64-hex-digest>"
  },
  "lease_entries": [
    {
      "kind": "subtree",
      "path": "renderer/features/edition-canvas"
    }
  ],
  "lease_state": "active",
  "reserved_ids": ["feature.edition-canvas"],
  "status": "active",
  "head_commit": "<40-hex-commit>",
  "validation": [],
  "blocker": null,
  "updated_at": "<UTC RFC3339>"
}
```

For B00 and later, `session.validation` contains exactly one context access-mode
entry with this closed semantic shape:

```json
{
  "schema": "studio-context-access-mode/1",
  "session_id": "U23-001",
  "work_package": "U23",
  "assignment_receipt_id": "<assignment-receipt-id>",
  "lease_id": "lease-U23-001-01",
  "access_mode": "write-lease"
}
```

The five identity values MUST equal the containing session and resolved packet
assignment. Exactly one matching entry is allowed. A reviewer uses
`read-only-review` and still has `status: active`, `lease_state: active`, a
nonnull `lease_id`, and nonempty observation-scope `lease_entries`; this ledger
1.0 validation extension grants no write authority and does not change the
ledger schema.

The coordination-ledger schema remains version `1.0`; protocol `1.0.1` does not
silently change that published schema ID or the historical bootstrap subtree.
For B00 and later, S00 first appends a `context-packet-activation` receipt that
pins `studio-context-packet/1`, packet ID/revision/path/commit/blob OID, phase
profile, `rfc8785-jcs-sha256/1` manifest digest, budget, and activated expansion
IDs before selected context is presented. It omits a handoff HEAD that does not
yet exist. The later `context-packet-handoff` receipt repeats those pins and
binds the final implementer packet revision to the unchanged handoff HEAD. It
also carries `activation_ledger`, a complete `gitBlobPin` whose path is exactly
`coordination/studio-ledger.json` and whose commit identifies the latest
cumulative pre-handoff ledger blob. That blob MUST contain exactly one named
outer `context-packet-activation` receipt and exactly one named outer
`context-packet-return` receipt for the session.
Receipt payloads are the ledger 1.0 extension seam. The packet itself cannot
contain its own commit or digest.

The activation receipt's containing coordination commit and raw ledger-blob
digest are an external historical activation pin because neither can appear in
the packet or activation receipt without self-reference. Emission validation
MUST treat that historical pin and current authority as separate checks. It
first samples the authoritative remote's protected coordination-ref HEAD, fetches
that exact commit into the session-local ref, reads and validates its complete
ledger 1.0 blob, and resolves exactly one session matching the packet's session,
work package, assignment receipt, lease, and access-mode validation entry. Both
the live session status and live access-lease state MUST be `active`. A
`write-lease` authorizes only its nonoverlapping write entries; a
`read-only-review` lease authorizes only bounded observation and zero writes.

The validator separately reads the externally pinned historical activation
ledger blob, proves its commit is an ancestor of the sampled live HEAD, finds
exactly one named activation receipt there, and validates its closed payload and
packet revision chain. After all packet, selector, digest, budget, and live-state
checks but before emitting any selected bytes, it samples the protected ref
again. If the second HEAD differs from the first, validation fails with
`coordination-ref-changed`; it MUST NOT emit from the stale sample. A
caller-supplied packet or activation digest alone never authorizes semantic
presentation. Candidate validation before the activation commit may compute the
digest while the pinned assignment state is `ready` with an `issued` lease, but
it MUST NOT emit context.

The closed receipt payload schemas are the
`contextPacketActivationReceipt`, `contextPacketReturnReceipt`,
`contextPacketHandoffReceipt`, and `contextPacketReviewReceipt` definitions in
`coordination/context-packet.schema.json`; ledger extension payloads MUST
validate against the corresponding definition with no additional properties.
Their exact required fields are:

- activation: `schema`, `session_id`, `work_package`, `lease_id`,
  `assignment_receipt_id`, `packet`, `profile`, `budget`,
  `activated_expansion_ids`, ordered `packet_revision_chain`, and
  `handoff_head`, which is `null`;
- return: `schema`, `session_id`, `work_package`, `lease_id`,
  `assignment_receipt_id`, `access_mode`, `packet`, `base_commit`,
  `handoff_head`, `lease_entries`, `changed_paths`, `acceptance_commands`, closed
  `checks`, `clean_worktree`, ordered `ordered_commits`, and empty
  `merge_commits`; `access_mode` is exactly `write-lease` and
  `clean_worktree` is exactly `true`;
- handoff: `schema`, `session_id`, `work_package`, `lease_id`,
  `assignment_receipt_id`, `activation_receipt_id`, `activation_ledger`,
  `packet`, `profile`, `budget`, `activated_expansion_ids`, ordered
  `packet_revision_chain`, non-null `handoff_head`, and `return_receipt_id`;
- review: `schema`, `reviewer_session_id`, `implementation_session_id`,
  `work_package`, `lease_id`, `reviewer_packet`, ordered
  `reviewer_revision_chain`, `implementation_packet`, ordered
  `implementation_revision_chain`, the unchanged `handoff_head`,
  `return_receipt_id`, `handoff_receipt_id`, closed `checks`, and `outcome`.

The review receipt's `lease_id` is the reviewer's active non-write scope lease;
the implementation lease is the separately bound
`review_of.implementation_lease_id`.

The enclosing ledger receipt kind is `context-packet-activation`,
`context-packet-return`, `context-packet-handoff`, or `context-packet-review` and
MUST match the payload schema marker. A handoff resolves exactly one
`context-packet-return` receipt by `return_receipt_id` and exactly one activation
receipt by `activation_receipt_id` from its pinned `activation_ledger`. A
reviewer packet's later assignment-ledger pin independently resolves exactly one
named `context-packet-handoff` receipt plus that same named
`context-packet-return` receipt; reviewer `review_of` and the review receipt bind
both closed payloads.

Each `checks` item has exactly `check_id`, `command`, integer `exit_code`, and
`result` (`passed` or `failed`). `passed` is valid only for exit code zero;
`failed` is valid only for a nonzero exit code. Review `outcome` is `accepted`,
`changes-requested`, or `rejected`; `accepted` requires every check to pass, and
either other outcome requires at least one failed check.

Each revision chain begins at revision 1, increases contiguously for the same
packet ID and canonical path, and ends at the separately named packet pin. An
activation or handoff chain equals all revisions activated for that session; a
review receipt repeats both its own reviewer chain and the final implementation
chain. Packet/profile/budget/expansion facts MUST equal the validated packet,
and review implementation session, work package, implementation lease, base,
handoff receipt, return receipt, changed paths, and commands MUST equal its
`review_of` facts, the resolved final implementation packet, the base-to-handoff
Git diff, the closed `context-packet-return` payload, and the pinned ledger
receipts. The handoff verifier resolves both its `return_receipt_id` and
`activation_receipt_id` in the latest cumulative pre-handoff
`activation_ledger`, never in the packet's earlier assignment-ledger pin. A
differing HEAD, identity, path set, or omitted revision fails the receipt. These
equality and chain rules are semantic validation in addition to the closed JSON
shape.

For a nonapplicable phase input, the corresponding pin object is exactly
`{"status":"not-applicable","reason":"<exact profile reason>"}` and omits tag
and digest fields. The exact reason is frozen above and in the selected profile.
The ledger schema rejects empty strings, synthetic hashes, or a `required` pin
with any missing field; the context validator additionally rejects a reason that
does not exactly match the selected profile.

Only A00 may temporarily set `baseline_verification_status` to
`remote-baseline-missing` and `remote_base_tag_object` to `null`. Before A00
enters review, A00 observes the now-matching remote object, changes the status to
`verified`, and records the verification command/time. S00 independently repeats
that verification while reviewing the unchanged A00 HEAD. No other state permits
a missing remote baseline, and S00 performs no pre-adoption ledger edit.

Allowed transitions:

```text
queued   -> ready
ready    -> active | abandoned
active   -> blocked | review | abandoned | superseded
blocked  -> active | abandoned | superseded
review   -> active | accepted | abandoned | superseded
accepted -> integrated | superseded

lease: issued -> active -> released | revoked
```

S00 updates the ledger at assignment, start verification, each material blocker
or gate, handoff, acceptance, integration, abandonment, and supersession.

A lease is not reusable merely because a session reports `blocked` or stops
responding. Before release/revocation and reassignment, S00 explicitly terminates
the prior session, inspects its worktree and running processes, records or
rejects uncommitted state, freezes the branch HEAD, and records the disposition.
The replacement receives a new lease and session ID.

If a task was mistakenly routed to a fresh branch before any work or new commit
existed, S00 records a `ready -> abandoned` no-work closure. That branch is not a
handoff, snapshot, migration input, or accepted source HEAD; it contributes
nothing to recover or merge. S00 may remove its branch, session-local ledger ref,
and worktree only after the same explicit inspection and cleanup record required
above.

## 8. Assignment and context packets

For B00 and every later package, S00 first commits the ordinary assignment in
`ready` state. It then commits a `studio-context-packet/1` manifest that refers
back to that assignment, validates the manifest against
`coordination/context-packet.schema.json` and the pinned phase profile in
`coordination/context-profiles.json`, and commits a
`context-packet-activation` receipt. Only then may the session transition to
`active` or receive selected semantic context. A packet is a closed context
set, not a starting point for recursive discovery. A referenced link,
directory, prior packet, conversation, or unlisted file adds no implicit
context.

Every packet assignment has required `access_mode`. A reviewer packet requires
`read-only-review`; implementer and integrator packets require `write-lease`.
Both modes retain a nonnull lease ID and nonempty typed lease entries. For
`read-only-review`, those entries are review-scope observation bounds only and
MUST NOT be interpreted as repository write authority.

The canonical packet path is
`coordination/context-packets/<packet_id>.json`. Revisions reuse that path at
new commits; every activation receipt pins the exact commit and blob OID, so an
older revision remains addressable and MUST NOT be rewritten or deleted from
history.

The packet contains a universal capsule and a package capsule. The universal
capsule pins authority, assignment, coordination protection, immutable base and
phase inputs, lease, reserved IDs, imports, stop routes, acceptance commands,
and return destination. The package capsule contains only the assigned section
18 row, applicable operations/ports/schemas/fixtures, requirement IDs, budgets,
non-goals, and composition instructions. Whole-ledger, lock, and source-file
integrity is checked mechanically; unrelated contents are not presented as
semantic session context.

`studio-context-profiles/1` is a template catalog, not implicit context. The
initial ordered core of `required_context` is exactly the catalog's
`universal_templates`, then `audience_templates[packet.audience]`, then the
selected profile's `templates`, then the one `package_templates` entry whose
`work_package` exactly matches the assignment. A verifier rejects missing,
duplicate, extra, or reordered core entries, unknown sources, a package outside
the profile, or an expansion outside `allowed_expansion_ids`. Only activated
expansion entries follow that core order.

Each profile's ordered `required_initial_expansion_routes` binds an expansion ID
to either the contract or fixture phase pin and the exact pointer template
`/context_routes/{work_package}`. At issue time, `{work_package}` is replaced by
the assignment's exact work-package ID (all frozen IDs contain neither `~` nor
`/`), and the resulting RFC 6901 pointer is resolved in the verified phase lock.
Revision 1 MUST materialize every binding in listed order and concatenate the
route arrays in that order. Within a route, entries appear in their lock order,
set `expansion_id` to the binding's ID, and otherwise match the routed
`context_id`, source path, media type, selector, and purpose exactly. A missing,
empty, duplicate, nonmember, extra, or reordered route or entry fails before
presentation. Revision 1 may activate no undeclared ID. Later progressive
disclosure appends only profile-allowed expansion entries.

```json
"required_initial_expansion_routes": [
  {
    "expansion_id": "contract-detail",
    "phase_pin": "contract",
    "route_pointer_template": "/context_routes/{work_package}"
  }
]
```

`contracts/contracts.lock.json` and `fixtures/fixtures.lock.json` each freeze a
top-level `context_routes` object before their respective G0 or GF promotion. A
`context_routes` value MUST validate against the `contextRoutes` definition in
`coordination/context-packet.schema.json`. A work-package member is a nonempty
ordered array whose entries have exactly
`context_id`, `source_path`, `media_type`, `selector`, and `purpose`. Every source
path MUST be an entry in that same lock, and every selector uses the closed
grammar below. The lock route is control metadata: the validator reads it
mechanically, but only its materialized selected values enter semantic context
and the context byte budget. G0/GF review proves each required route complete,
relevant, and minimal before downstream activation.

Every ordered `required_context` entry pins the repository URL, source commit,
path, Git blob OID, `git-blob-payload-sha256/1` whole-blob digest, purpose, one
closed selector, the selected digest domain/digest, and selected UTF-8 byte
length. Core entries set `expansion_id` to `null`; every expansion entry names
one activated, profile-allowed expansion ID. The selector grammar is exactly:

- `whole-git-blob/1`;
- `markdown-heading-section/1`, with ATX `heading_level`, `heading_text`, and
  1-based `occurrence`;
- `markdown-table-row/1`, with `containing_heading`, exact `table_columns`,
  1-based `table_occurrence`, `key_column`, and `key_value`;
- `rfc6901-json-pointer/1`, with a document-root `pointer` and optional
  `expected_identity` pointers relative to the selected value. The empty string
  is valid for either form and selects the corresponding root value.

Line numbers are display hints only. Markdown source MUST decode as strict UTF-8.
ATX recognition allows zero to three leading ASCII spaces, one to six `#`
characters, and either end of line or ASCII space/tab before content. Heading
text is the remaining content after trimming ASCII space/tab and an optional
whitespace-preceded closing `#` sequence; comparison is exact Unicode-scalar
comparison with no normalization or case folding. Headings inside a fenced code
block are ignored. A fence is opened by zero to three spaces plus at least three
backticks or tildes and closed only by zero to three spaces plus at least the
same count of the same character followed only by ASCII space/tab before the
line ending. The heading
selection begins at the first raw byte of the matched heading line, includes
that line terminator, and ends immediately before the next outside-fence ATX
heading of equal or higher level, or at EOF.

The table selector operates only inside that selected containing-heading span.
Its closed table grammar requires each header, delimiter, and data row to begin
and end with `|`; embedded or escaped `|` is unsupported and makes selection of
that candidate fail. Cells are split on `|` and trimmed of ASCII space/tab.
Every delimiter cell matches `:?-{3,}:?` and has the header's cell count.
`table_occurrence` counts, in source order, only valid adjacent header/delimiter
pairs whose trimmed header cells equal `table_columns` exactly. After a selected
pair, its data block is the maximal contiguous sequence of following physical
lines that begin and end with `|`; the first other line or EOF terminates the
block. Every pipe-delimited line in that block MUST have exactly the header's
cell count, so a malformed or wrong-count pipe row invalidates the selector
rather than terminating the table. `table_columns`, `key_column`, and
`key_value` compare exactly with no normalization or case folding, and exactly
one data row MUST match. The emitted selection is the raw header line, delimiter
line, and matched row in that order, including each source line terminator when
present.

An RFC 6901 selector resolves `pointer` from the parsed document root. Each
`expected_identity` key resolves relative to the selected value and its scalar
value compares by equality of RFC 8785 JCS bytes. The emitted selection is the
selected JSON value serialized as RFC 8785 JCS UTF-8. `whole-git-blob/1` emits
the complete raw blob, which MUST also be valid UTF-8. Digest domains are:

| Identity                      | Digest domain                      |
| ----------------------------- | ---------------------------------- |
| whole committed source        | `git-blob-payload-sha256/1`        |
| whole/Markdown selected bytes | `selected-git-blob-bytes-sha256/1` |
| selected JSON value           | `rfc8785-jcs-sha256/1`             |
| context-packet manifest       | `rfc8785-jcs-sha256/1`             |

Before presenting selected semantic context, the validator MUST read the blob
through Git object plumbing, verify its whole digest, resolve exactly one
selector, verify the selected digest and byte length, enforce the packet budget,
and verify the packet-manifest digest. Any mismatch is
`context-packet-invalid` and stops the session. Mechanically scanning a whole
blob to validate or resolve a selector does not authorize semantic
full-document ingestion.

Context budgets use deterministic UTF-8 bytes; token estimates are advisory:

| Profile | Default bytes | Hard maximum bytes |
| ------- | ------------: | -----------------: |
| B00     |        49,152 |             73,728 |
| C00     |       135,168 |            163,840 |
| T01     |        98,304 |            131,072 |
| E10     |       180,224 |            229,376 |
| D20     |        98,304 |            131,072 |
| U20     |        65,536 |             98,304 |
| E11–E21 |       131,072 |            163,840 |
| U21–U27 |        65,536 |             98,304 |
| I30     |       114,688 |            163,840 |

A profile activation is invalid if any package's initial implementer packet,
including required initial expansions, exceeds its default; if a package appears
zero or multiple times; or if its declared base/contract/fixture expectations
differ from section 3.

A named profile limit applies to the complete ordered packet—universal,
audience, package, and progressive-disclosure entries together—and is not
additive. Charged bytes are the sum of `selected_utf8_bytes`, counting every
entry once even when byte spans overlap. A cumulative successor revision is
metered once and does not re-add superseded revisions. Mechanical whole-blob
scans and manifest metadata are excluded from selected-context bytes, but the
packet's full RFC 8785 JCS form MUST be no more than 65,536 bytes and records its
own measured JCS byte length.

Required content is never truncated. `selected_utf8_bytes` MUST equal the sum of
the ordered entries and be no greater than `authorized_utf8_bytes`, which MUST be
no greater than the profile hard maximum. `above_default_reason` is non-null if
and only if `authorized_utf8_bytes` exceeds the profile default, and is `null`
otherwise. The packet's default and hard maximum MUST equal the selected profile.
Exceeding a hard maximum
requires a separately reviewed and versioned profile amendment; a successor
packet cannot raise it ad hoc. `whole-git-blob/1` for either normative document
is allowed only by an explicit profile rule or progressive-disclosure reason.

Progressive disclosure is append-only. The session reports the missing
question/source, reason, and expected byte cost. Within the pinned profile's hard
maximum, S00 issues packet revision `n+1` containing the complete cumulative
closed list and records its external manifest digest; revision `n` remains
immutable. Revision 1 sets `previous_revision` to `null`; every successor pins
the immediately prior packet's ID/revision/path/commit/blob/JCS digest. The
validator requires the same packet/assignment/profile, the prior ordered context
as an exact prefix, cumulative expansion IDs, and only an authorized budget
increase within the same hard maximum. Unlisted context cannot become
authoritative before that revision. The packet cannot pin its own containing
commit or digest, so the later activation receipt pins its path, commit, blob
OID, digest domain, and digest.

Reviewers receive a distinct `audience: reviewer` packet with
`assignment.access_mode: read-only-review`, a separate active reviewer session,
and a separate active non-write review-scope lease. `review_of` pins the final
implementation packet plus `implementation_session_id`, `work_package`,
`implementation_lease_id`, base and unchanged handoff HEAD, the implementation
lease-bound path inventory, exact changed paths, `return_receipt_id`,
`handoff_receipt_id`, and acceptance commands. The validator resolves the final
implementation packet and both named receipts, reconstructs
`git diff --name-only <base_commit>...<handoff_head>`, and requires every one of
those values to match mechanically. The reviewer's own assignment lease is
distinct from `review_of.implementation_lease_id` and `review_of.lease`. The
packet repeats needed selectors rather than importing the implementer's
conversation or an implicit union of its context.

Every assignment brief contains the phase-applicable fields below. For B00 and
later, the context packet repeats the complete closed brief; A00 and A01 use the
explicit earlier exceptions and omit packet-only fields.

- session/work-package ID, owner, branch, worktree, required `access_mode`, lease
  ID, and canonical `lease_entries` array using the section 6 representation;
- protected coordination ref, minimum assignment-ledger commit/digest, and the
  lease ID represented there, or A00's explicit bootstrap-ledger
  `not-applicable` substitution;
- for B00 and later, the `coordination-ref-protection` receipt ID, canonical
  provider-policy projection schema, digest domain/digest, rule/ruleset IDs, and
  authorized S00 principals;
- authoritative remote URL; exact base tag; matching local/remote annotated tag
  object; peeled commit; tree; and baseline-verification status, subject only to
  A00's temporary missing-remote exception;
- phase-required contract tag and lock digest, or `not-applicable` plus reason;
- phase-required fixture tag and lock digest, or `not-applicable` plus reason;
- package-specific schemas, generated package, fakes, and port contracts when
  those inputs exist for the phase;
- reserved public IDs, including provided/required port binding IDs;
- required public outputs and acceptance behaviors;
- exact format, lint, typecheck, test, and build commands;
- performance/security/accessibility budgets that apply;
- known upstream limitations and explicit non-goals;
- for B00 and later, the exact protocol adoption tag object/commit/tree, context
  schema and profile artifact path/commit/blob/digests, context packet/profile
  IDs, revision, manifest digest, closed selector list, UTF-8 byte budget, and
  any activated disclosure revisions;
- the return destination and S00 contact/session.

Copy-paste B00-and-later brief:

```text
You are implementing <work-package> in session <session-id>.

Fresh-session rule: do not inherit or rely on prior conversations.

Context packet: studio-context-packet/1 / <packet-id> / revision <n>
Manifest digest: rfc8785-jcs-sha256/1 / <SHA-256>
Profile and budget: <profile> / <default> / <hard> / <authorized UTF-8 bytes>
Activation pin: <receipt ID / containing coordination commit / raw ledger SHA-256>
Access mode: write-lease

Required context is only the closed ordered selector list in that validated
packet. Validate each whole blob before resolving or presenting its selection.
No link, directory, referenced file, prior packet, or transcript adds implicit
context. Request a new packet revision for progressive disclosure.

Authoritative remote: <URL>
Coordination pin: <ref / minimum ledger commit / ledger SHA-256 | A00 not-applicable + bootstrap ledger SHA-256>
Lease ID/entries: <lease-id> / <canonical JSON exact-file/subtree array>
Base tag/object/commit/tree: <tag> / <tag-object> / <commit> / <tree>
Baseline verification: <verified | A00 remote-baseline-missing>
Contract pin: <tag / tag-object / lock SHA-256 | not-applicable + phase reason>
Fixture pin: <tag / tag-object / lock SHA-256 | not-applicable + phase reason>
Reserved public IDs: <ids>

You may write only:
- <exact-file: repository/relative/file>
- <subtree: repository/relative/directory>

You may import only:
- generated contract package (when the contract pin is required)
- package-local public code
- <explicit foundation SDK/ports>

Required outputs:
- <the exact outputs for this work package from specification section 18>
- <phase-specific reports, locks, manifests, tests, and public entry point>

Acceptance commands:
- <exact commands>

Stop on a contract, fixture, root-dependency, ownership, public-ID, or sibling-
implementation need. Do not patch around it. Return committed paths, digests,
test receipts, limitations, and composition instructions.
```

## 9. During implementation

- Work only inside the assigned worktree and lease.
- Target the pinned generated client and fakes; do not assume a sibling branch
  exists.
- Commit coherent checkpoints. Do not leave the only copy of useful work
  uncommitted.
- Do not rebase onto a moving branch. If a prerequisite baseline is superseded,
  S00 creates a new session/branch or explicitly records a controlled replay.
- Do not regenerate the root lockfile unless the active lease is B00 before GB
  or I30 after the composition-input tag. B00 establishes the frozen production
  dependency graph; I30 may add only composition/packaging dependencies. A
  package-local lock may change only when its package owns it and the brief
  permits it.
- Do not weaken tests, validators, security boundaries, payload caps, or frozen
  semantics to make implementation pass.
- Use ports for conceptual dependencies. If the required port is absent, stop.
- Send updates at start, on every blocker, before public-surface changes, and at
  handoff. Updates are reports to S00, not edits to the shared ledger.
- Include the active context packet ID/revision/digest in every update. Context
  additions use progressive disclosure; pasted conversation is not a packet.

Short update format:

```text
Session: <id>
Status: active | blocked | review
HEAD: <commit>
Changed lease entries/paths: <typed entries and summary>
Checks: <command=result>
Reserved IDs used: <ids>
Blocker/decision: <none or exact issue and owner>
Next: <one concrete step>
```

## 10. Stop and escalation rules

Stop before editing outside the lease when:

- a schema, operation, generated binding, canonicalization rule, URI grammar,
  or other frozen contract must change — route to C00;
- a shared fixture/vector is missing or inconsistent — route to T01;
- a dependency/lock/workspace change is needed — route a missing feature/runtime
  dependency to a versioned B00 baseline supersession and replay; route only a
  composition/packaging dependency to I30 after the composition-input tag;
- paths overlap or a public ID conflicts — route to S00;
- a sibling implementation seems necessary — request a port/contract decision;
- a semantic integration conflict appears — return it to the owning package;
- the base/tag/digest differs — route to S00 as `baseline-mismatch`;
- authoritative context is missing, a selector is ambiguous, or a byte budget
  would be exceeded — stop for an S00 packet revision;
- credentials, restricted content, or an external side effect would exceed the
  assignment's authority — stop for explicit authorization.

Temporary ownership overlap, local compatibility shims, silent lock
regeneration, moved tags, copied untracked inputs, and “fix it in integration”
are prohibited resolutions.

## 11. Handoff acceptance

Before requesting review, the implementer provides:

- authoritative remote, matching local/remote base tag object, peeled base
  commit/tree, and head commit;
- for B00 and later, protected coordination ref plus the observed current ledger
  commit/digest, with the lease still active and nonoverlapping; for A00, the
  approved bootstrap-ledger digest;
- for B00 and later, the complete latest cumulative pre-handoff
  `activation_ledger` Git-blob pin whose ledger contains the named
  context-packet activation and return receipts;
- final context packet/profile IDs, packet revision, manifest digest, selected
  byte total versus budget, and every activated disclosure revision;
- each phase-required contract and fixture tag object/lock digest, or the exact
  `not-applicable` phase reason;
- clean `git status --porcelain`;
- successful `git merge-base --is-ancestor <base-commit> HEAD`;
- ordered `git rev-list --reverse <base-commit>..HEAD` commit inventory;
- empty `git rev-list --merges <base-commit>..HEAD`; implementation branches
  MUST NOT contain merge commits;
- `git diff --name-only <base-commit>...HEAD`, with every path inside the lease;
- format/lint/typecheck/test/build/acceptance commands and exit codes;
- validation against the unchanged final handoff HEAD, including OS and exact
  Node/npm/Python/tool versions;
- confirmation that phase-frozen inputs and every protected path outside the
  active lease did not change;
- paths and SHA-256/lock/tag/report digests for every authorized owner output
  created or changed by this phase;
- module manifest, public entry point, and composition registrations when
  section 18 requires them for this work package;
- migration/data-impact statement;
- known limitations, deferred work, and required I30 actions.

S00 records the mechanically bindable subset as one outer ledger receipt of kind
`context-packet-return` whose payload validates as
`studio-context-packet-return/1`. `ordered_commits` is the exact ordered output
of `git rev-list --reverse <base_commit>..<handoff_head>`; `merge_commits` is the
exact output of `git rev-list --merges <base_commit>..<handoff_head>` and MUST be
empty. `changed_paths` is the ordered repository-path sequence decoded from the
NUL-safe output of
`git diff --name-only -z <base_commit>...<handoff_head> --`. Its packet, session,
work package, assignment, write lease, commands, checks, clean-worktree fact,
base, and head MUST equal the validated packet and Git evidence.

Copy-paste return receipt:

```text
Session/work package/lease: <id> / <package> / <lease-id>
Lease entries: <canonical JSON exact-file/subtree array>
Authoritative remote: <URL>
Observed coordination pin: <ref / ledger commit / ledger SHA-256 | A00 bootstrap ledger SHA-256>
Access mode: write-lease
Activation ledger: coordination/studio-ledger.json / <commit> / <blob OID> / git-blob-payload-sha256/1 / <SHA-256>
Context packet/profile/revision: <packet-id> / <profile-id> / <n>
Context manifest digest: rfc8785-jcs-sha256/1 / <SHA-256>
Context use: <selected UTF-8 bytes> / <authorized bytes>; expansions: <ids or none>
Base tag/object/commit/tree: <tag> / <tag-object> / <commit> / <tree>
Baseline verification: verified
Head commit: <commit>
Ordered commits: <base-exclusive commit IDs in order>
Contract pin: <tag-object / lock SHA-256 | not-applicable + phase reason>
Fixture pin: <tag-object / lock SHA-256 | not-applicable + phase reason>

Changed paths:
- <paths grouped by purpose>

Public surface:
- operations/capabilities/ports/contributions/migrations: <ids or not-applicable>
- entry point: <path/export or not-applicable>

Authorized owner outputs:
- <path> -> <SHA-256, lock digest, tag object, or report digest>

Validation:
- <command> -> <exit code/result>
- validation HEAD: <same head commit>
- environment: <OS, Node, npm, Python, package-tool versions>

Ownership proof:
- every changed path is covered by a typed lease entry: yes/no
- working tree clean: yes/no
- implementation-branch merge commits absent: yes/no
- phase-frozen inputs and protected paths outside lease unchanged: yes/no

Known limitations:
- <items or none>

Composition instructions:
- <registrations, order, environment, migrations>

Required follow-up owner:
- <S00/C00/T01/package/I30 or none>
```

S00 rejects an uncommitted, dirty, out-of-lease, unpinned, or unverifiable
handoff without integrating it. The implementer return and
`context-packet-handoff` receipt MUST pin the final implementer packet and
unchanged handoff HEAD. The independent review receipt MUST pin both its own
distinct reviewer packet and that final implementer packet plus the unchanged
handoff HEAD. Any HEAD change invalidates the review and requires a new reviewer
packet and receipt.

## 12. Review, merge, and supersession

1. S00 verifies the receipt and lease-bound diff.
2. A reviewer reruns package checks in a clean worktree without trusting source
   caches and only after its distinct reviewer packet, active reviewer session,
   active non-write review-scope lease, `read-only-review` validation entry, and
   all `review_of` bindings validate. The review lease grants zero writes.
3. The package owner fixes semantic findings on the same session branch.
4. S00 marks the unchanged handoff HEAD accepted and records its ordered commit
   list and validation environment.
5. Before every post-adoption immutable baseline assembly, S00 commits and
   pushes one pre-assembly coordination record naming the immutable base,
   accepted source HEADs, merge order, validation receipts, and assembly
   commands. It cannot name its future baseline tag object. The original
   `studio-adoption-v1.1.0` tag is the sole direct-tag exception: S00 points it
   directly at the accepted A00 commit that contains the bootstrap coordination
   record, then establishes the protected coordination ref.
   The separately authorized A01 context amendment is the sole pre-GB use of
   this assembly procedure: it merges the pinned S00 pre-assembly coordination
   commit and accepted A01 document HEAD without authoring either source.
6. In a clean assembly worktree at the declared base, S00 merges that exact
   coordination commit first, then accepted source HEADs using conflict-free
   `--no-ff` merges in the declared order. Any conflict aborts and returns to the
   owning package; S00 never resolves it or edits product paths.
7. For A01 only, S00's pre-assembly record, under the maintainer-authorized
   assembly work order, pins the pre-GB Git executable, version, executable
   digest, merge configuration, and exact `git merge-tree` verification
   commands. B00's GB verifier MUST replay and confirm that A01 assembly
   evidence. Beginning with B00 assembly, S00 uses the GB-pinned Git
   implementation and verifier. For every merge commit, S00 proves that its tree
   equals the clean automatic merge tree for its two parents and that every
   introduced product blob or deletion comes from the accepted source diff. A
   dirty assembly worktree or a blob with no accepted-source provenance fails
   assembly.
8. S00 uses this process for the accepted B00, C00, and T01 package baselines;
   for the E10, D20, and U20 foundation baselines; then for E11–E21 in ascending
   ID order for
   `studio-headless-integration-v1.0.0`; then U21–U27 in ascending ID order for
   `studio-renderer-integration-v1.0.0`.
9. S00 creates `studio-composition-input-v1.0.0` by merging the current
   coordination commit, headless baseline, renderer baseline, and
   `studio-desktop-foundation-v1.0.0` in that order under the same verification.
10. After annotating and publishing a baseline, S00 commits a post-assembly
    receipt to the protected coordination ref with tag object, peeled commit,
    tree, source-to-merge mapping, and verifier receipts. That receipt enters the
    next baseline; the completed tag is never moved to include a receipt about
    itself.
11. I30 alone branches from the composition-input tag and edits I30-owned
    composition paths. It reruns collision, port binding, contract, migration,
    and applicable gate checks.

S00 assembly is a narrowly defined non-authoring exception to the normal
lease-bound-diff rule: merge commits necessarily introduce accepted product
paths, but S00 owns no product lease and may create no product blob. Only
coordination commits remain subject to S00's ordinary `coordination/**` lease.

If an accepted commit is replaced, the prior receipt remains. The new commit,
review, and baseline get new identifiers; history and tags are not rewritten.

## 13. Concurrency waves

Safe maximum parallelism follows the dependency graph:

1. A00 alone; S00 accepts and tags `studio-adoption-v1.1.0`.
2. A01 alone; S00 accepts, assembles, and tags `studio-adoption-v1.1.1`.
3. B00 alone while S00 coordinates from its separate worktree.
4. C00 alone after GB.
5. T01 alone after G0.
6. E10, D20, and U20 concurrently after GF.
7. E11–E21 concurrently from the engine foundation; U21–U27 concurrently from
   the renderer foundation. S00 MAY reduce concurrency when leases, machine
   resources, or integration risk require it.
8. S00 assembles headless and renderer baselines; I30 composes only accepted
   baselines.

Two sessions may communicate through frozen contracts, fixtures, reserved IDs,
and S00 updates. They MUST NOT coordinate by reading another session's
uncommitted files, inheriting conversations or unrecorded summaries, dumping
unrelated ledger history, or creating an undeclared private dependency. A
needed decision is promoted to a pinned contract, fixture, receipt, or context
packet revision.
