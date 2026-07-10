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
empty state. Read-only assets (`copyright_renewals.csv`, `whl_catalog.csv`,
`output/ch_library.json`) still resolve through `APP_ROOT` to that worktree's
checkout, so the app works — it just starts with no books.

`--seed` copies the main checkout's books and settings into the new
`DATA_ROOT`. It is a copy: the main checkout is never touched. Note that
`client_state.json` carries your API keys.

**A port.** `server.py` binds 5001 unless `WHL_PORT` says otherwise, and it
never reads `PORT`, so `autoPort` cannot help. Each worktree is assigned 5101,
5102, … and gets a `.claude/launch.json` pointing at `.wt/serve.py`, which sets
`WHL_PORT` and `WHL_DATA_ROOT` before starting the server. `preview_start` then
works in every session at once.

**A smaller checkout.** The full tree is ~330 MB, of which `photo/` and
`books/` are 273 MB of capture images no code reads. Worktrees exclude them by
sparse-checkout, landing at ~57 MB (mostly the 40 MB renewals CSV, which the
copyright tag needs). `--full` keeps everything.

## Things worth knowing

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
- **`output/*.json` are tracked** (builds, manual entries, corrections). A
  worktree's server writes to `.wt/data` instead, so those stay clean — but if
  you seed and then edit through the UI, you are editing the copy, not the repo.
- The five Google/DLI scans have no text layer; unrelated to worktrees, but it
  will bite anyone testing the OCR Layout view against them.
