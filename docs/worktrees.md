# Parallel sessions with git worktrees

A worktree is a second checkout of this repo on its own branch, sharing one
`.git`. Run a Claude session in each and they cannot step on one another.

```
python3 tools/worktree.py add ocr-boxes      # branch + worktree + port
python3 tools/worktree.py list
python3 tools/worktree.py rm ocr-boxes       # -d also deletes the branch
```

Then, in a second terminal:

```
cd ../whl-worktrees/ocr-boxes
claude
```

Worktrees live in `../whl-worktrees/` — outside the repo, or every `git status`
in the main checkout would report them as untracked. Override with
`WHL_WORKTREES`.

## What the script does that `git worktree add` does not

**A private DATA_ROOT.** `server.py` writes its state — `client_state.json`,
manual entries, builds, downloads — under `DATA_ROOT`, which defaults to the
repo root. Two servers writing one `output/` is how this project lost its
checked-book set once: `client_state` is server-authoritative on load, so the
emptier instance wins. Each worktree therefore runs against `.wt/data`, its own
empty state. `output/ch_library.json` still resolves through `APP_ROOT` to
that worktree's checkout, so the app works — it just starts with no books.
The databases — the 40 MB renewals CSV, the Open Library indexes, and
`whl_catalog.csv` too — resolve most-accessible-first from the shared
`~/.library-tool` drop-in folder, then `DATA_ROOT`, then the checkout (see
`find_db` in `tools/libcommon.py`), so every worktree uses the one copy;
for the WHL catalogue the checkout's own CSV is the usual fallback.

`--seed` copies the main checkout's books and settings into the new
`DATA_ROOT`. It is a copy: the main checkout is never touched. The copied
`client_state.json` keeps nonsecret preferences and work state but strips every
registered legacy credential field. Protected and retired secret-store files
are never seeded.

**A port.** `server.py` binds 5001 unless `WHL_PORT` says otherwise, and it
never reads `PORT`, so `autoPort` cannot help. Each worktree is assigned 5101,
5102, … and gets a `.claude/launch.json` pointing at `.wt/serve.py`, which sets
`WHL_PORT` and `WHL_DATA_ROOT` before starting the server. `preview_start` then
works in every session at once.

**A smaller checkout.** Mostly history now: the corpus images — `photo/` and
the `books/` scans, 273 MB no code reads — left git entirely and sync through
R2 via `tools/corpus_sync.py`, so any checkout of a current commit is ~20 MB.
The sparse-checkout exclusion survives as a safety net for worktrees based on
older commits that still track the images. Note the pattern covers all of
`books/`, so a default worktree also omits the 34 tracked
`books/*/transcript.txt` OCR transcripts; `--full` restores those on any
base — the images it can only bring back on a pre-corpus-sync base, since a
current tree no longer holds them.

Sparse-checkout has a sharp edge that cost this repo its `photo/` directory
once. `git sparse-checkout init` writes `core.sparseCheckout` to the **shared**
config unless `extensions.worktreeConfig` is enabled first. The main checkout
then honours a pattern file it does not have, decides nothing is included, and
silently deletes 273 MB from its working tree on the next index refresh. Nothing
is lost — the files are in git objects, and `git sparse-checkout disable && git
checkout -- photo books` brings them back — but it is alarming. The script
enables `extensions.worktreeConfig` first, so both the flag and the patterns
live under `.git/worktrees/<name>/`, and then it verifies: if the flag ever
shows up in the main config, or the heavy directories go missing, it removes the
new worktree, restores the main checkout, and refuses.

## Things worth knowing

- **`add` forks from `master` by default**, which may be stale or absent in
  your clone. Pass the line you actually want: `add ocr-boxes --base main`.
  `--port` likewise overrides the assigned port.
- **A branch can only be checked out once.** `git worktree add` refuses a branch
  that another worktree holds, which is what stops two sessions committing to
  the same branch.
- **`.claude/launch.json` is not tracked** — the port differs per checkout. See
  `.claude/launch.example.json`. `.wt/` is ignored too.
- **`.claude/settings.local.json` is not tracked either**, so a new worktree
  re-prompts for tool permissions. That file has held API keys verbatim; do not
  copy it around.
- **Merging back:** the worktree is an ordinary branch. `git -C . merge <name>`
  from the main checkout, then `rm -d`.
- **Some `output/*.json` are still tracked** (`manual_entries.json`,
  `ch_library.json`, the books indexes); builds and corrections left git. A
  worktree's server writes to `.wt/data` either way, so the checkout stays
  clean — but if you seed and then edit through the UI, you are editing the
  copy, not the repo.
- The five Google/DLI scans have no text layer; unrelated to worktrees, but it
  will bite anyone testing the OCR Layout view against them.
