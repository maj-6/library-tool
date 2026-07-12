# Library Tool — project notes for agents

A cataloguing workbench: a Flask sidecar (`tools/whl_explorer/`) wrapped in an
Electron desktop shell (`desktop/`), an Android "Book Capture" app (`android/`),
and a static site (`website/`) published to GitHub Pages. Public code mirrors to
`github.com/maj-6/library-tool`.

## Release standards

The project is **pre-1.0 (0.x)** and ships intermediate builds deliberately.

- **Intermediate / alpha / beta builds MUST be produced and published** for
  download and testing — the Downloads page's "Other downloads" section and
  GitHub Releases. Known TODOs, loose threads, and incomplete or non-functioning
  features are **acceptable** in these builds; keep them flowing.
- Those same gaps are **NOT acceptable in a stable release** (`1.0.0`+, or any
  build promoted as stable), which must clear a higher bar — no broken or
  visibly unfinished features, and the `docs/releasing.md` "Known caveats"
  burned down.
- Cut intermediate builds as semver **prereleases** (tag `v0.7.0-alpha.1`) so
  they never auto-ship to stable users. Exact mechanics — GitHub prerelease flag
  + `alpha`/`beta`/`rc` release channel — are in `docs/releasing.md` →
  **Release standards**.
