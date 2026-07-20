# Releasing

The Downloads page on the website renders the `releases` table in Supabase,
newest row per platform. Publishing a release means getting a row into that
table with a URL people can download; the static site itself never changes or
redeploys for a release.

## Versioning

The project is pre-1.0: it versions on a `0.x` line while it is still taking
shape (`0.x` signals that anything may still change). The two apps version
independently — the desktop from `desktop/package.json`, Book Capture from its
gradle `versionName` — and the `v*` tag names the GitHub Release after the
desktop version. Bump the patch for fixes and the minor (`0.4` → `0.5`) for
features; save `1.0.0` for the first release meant to be stable. Do not renumber
back into `3.x` — those tags are kept only as archived history from before the
reset. One consequence of the reset: a desktop release numbered below an
installed `3.x` build will not auto-update it (electron-updater only moves
forward), so those few installs need a one-time manual reinstall. Android is
unaffected — it updates by `versionCode`, which keeps climbing regardless of the
`versionName`.

## Release standards

The project is pre-1.0 and ships **intermediate builds on purpose**. Alpha/beta
builds are expected to be produced and published for download and testing —
known TODOs, loose threads, and half-finished features (see **Known caveats**
below, and whatever is open at the time) are acceptable in them. That bar does
**not** carry to a stable release: `1.0.0`, and any later build promoted as
stable, must clear a higher one — no non-functioning or visibly incomplete
features, and the known caveats burned down. Treat everything before then as a
testing line, and keep intermediate builds flowing so there is always something
to try.

Cut an intermediate build as a semver **prerelease** so it stays testable
without reaching stable users:

- Version it with a prerelease suffix — `desktop/package.json` `0.7.0-alpha.1`
  (then `-alpha.2`, `-beta.1`, `-rc.1`, finally plain `0.7.0` for the stable
  cut). Bump the Android `versionCode` too if the APK rides along.
- Tag it `v0.7.0-alpha.1` and push. The existing `v*` pipeline builds it, and
  because the tag carries `-alpha` / `-beta` / `-rc` it:
  - flags the GitHub Release **prerelease**, so the desktop auto-updater
    never offers it to a stock stable install. `allowPrerelease`
    (desktop/main.js) turns on only for an installed alpha/beta/rc — it keeps
    following its prerelease line — or a stable install that opted in via
    Settings > Updates (`includePrereleaseUpdates`); everyone else holds at
    the last stable version.
  - registers the `releases` row on the **`alpha` / `beta` / `rc` channel**, so
    it appears in the Downloads page's **Pre-release builds** section instead
    of replacing the stable card.

A plain `vX.Y.Z` tag (no suffix) is the stable path — GitHub "Latest", the
stable auto-update channel, and the main Downloads list. Reserve it for builds
that actually meet the bar above.

`debug` is not a public update channel. Use `workflow_dispatch` for a private,
inspectable debug artifact; public prerelease tags accept only `alpha`, `beta`,
or `rc`, so an experimental suffix can never fall through to the stable list.
The public-version grammar is exact: `X.Y.Z` for stable or
`X.Y.Z-(alpha|beta|rc).N` for a prerelease, using canonical non-negative integer
components. Mixed or trailing suffixes such as `-debug-alpha.1` and
`-alpha.1-debug` fail preflight instead of being classified by substring.

Two ways a row gets there:

## The pipeline (the normal way)

`.github/workflows/release.yml` runs on the public repo when a `v*` tag is
pushed:

1. **android** compares its `versionCode` and `versionName` with the newest
   previous, non-draft GitHub Release that actually contains a non-empty,
   uploaded `BookCapture-*.apk`. Releases where Android failed are not a
   baseline, so a partial desktop release cannot make the next run skip an APK
   that never shipped. The release query is fail-closed: an API, JSON, tag, or
   Gradle-version parsing error stops preflight instead of guessing. If no APK
   has ever shipped, Android is included as a first release. If both values are
   unchanged, Android is deliberately skipped. Otherwise, `versionCode` must
   increase and `versionName` must also change; a name-only, code-only, equal-code,
   or decreasing-code change is rejected. The job then builds
   `android/BookCapture` (`assembleRelease`) and names the APK after its Gradle
   `versionName`. A tag push that includes Android **requires** the release
   keystore secret: without it the android job fails before the APK is uploaded,
   because a debug-signed build can't update an existing install. The desktop
   release is independent and still ships. The job then verifies the APK's signer (via
   `apksigner`) and requires its normalized certificate SHA-256 to match
   `android/BookCapture/release-signing-cert.sha256`. This also protects a
   release-signed `workflow_dispatch` build. A dispatch without the secret still
   builds with the debug key and is suffixed `-debug-DONOTPUBLISH.apk`; that is
   the only signer-mismatch path the workflow permits.
2. **desktop** freezes the Flask sidecar with PyInstaller and runs
   an isolated transport smoke against that frozen executable before running
   electron-builder on a Windows runner, producing the NSIS installer
   `LibraryTool-Setup-<package.json version>.exe` **plus `latest.yml` and the
   `.blockmap`** — those two are the auto-update channel: installed apps check
   the newest GitHub Release at startup and read them to fetch the update. The
   job requires all three files and verifies that `latest.yml` names the expected
   version and installer before uploading any of them.
3. **publish** attaches the builds to a GitHub Release named after the tag,
   using `docs/releases/<tag>.md` as its release notes when that file exists,
   then registers them in the `releases` table via `tools/release_publish.py`
   (URL → the GitHub Release asset). The two apps release independently: the
   job runs when either build succeeded and registers only the artifact(s)
   present, so a broken Android build never blocks a desktop release (or vice
   versa). When only one app builds, the GitHub Release title and a prominent
   notes preamble identify the release as partial and distinguish a deliberate
   unchanged-version Android skip from a build failure. Reruns also account for
   allowed assets already attached to the same release so they do not rewrite a
   full release as partial. Only the named `desktop` and `android` workflow
   artifacts are downloaded, and an explicit installer/manifest/blockmap/APK
   allowlist is uploaded. A new GitHub Release stays draft until every
   allowlisted asset uploads successfully; a rerun repairs an interrupted draft
   before publishing it. Existing public releases receive replacement assets
   before their title, notes, and channel metadata are edited. If a rerun reuses
   an already-uploaded allowlisted asset, it downloads that exact file for the
   subsequent Downloads-page registration step as well.
   The Downloads page shows the rows immediately, and existing
   installs with auto-update on (the Settings > Updates default) pick the
   update up on their next launch.

The Downloads page fails closed on release rows: unknown or whitespace-only
channels, non-HTTP(S) or malformed URLs, and names containing `DONOTPUBLISH`
are filtered before choosing the newest build for each platform and channel.
That lets an older valid release remain visible if a newer invalid row is ever
inserted manually.

So cutting a release is:

```
# draft docs/releases/v0.4.0.md before tagging
# bump versions first if warranted:
#   android/BookCapture/app/build.gradle.kts   versionCode + versionName
#   desktop/package.json                       version
git tag v0.4.0
git push <public> main --follow-tags           # or push the tag with the next mirror publish
```

A `workflow_dispatch` run of the same workflow is a dry run: both apps build
and the artifacts are inspectable, nothing is published.

### One-time repository setup (public repo, Settings → Secrets and variables)

| name | kind | purpose |
|---|---|---|
| `SUPABASE_URL` | variable | already set for the pages workflow |
| `SUPABASE_ANON_KEY` | variable | baked into the APK at build time so first run needs no typing; blank falls back to the in-app Settings project |
| `SUPABASE_SERVICE_ROLE_KEY` | secret | writes to `releases` (anon is read-only by RLS). Without it the GitHub Release still happens; the register step warns and skips. |
| `ANDROID_KEYSTORE_B64` | secret | **required to publish Android on a tag.** base64 of the signing keystore. Local copy: `~/.whl-release/bookcapture.jks(.b64)`, password in `bookcapture-keystore-info.txt` next to it. |
| `ANDROID_KEYSTORE_PASSWORD` | secret | its password |
| `ANDROID_KEY_ALIAS` | secret | `bookcapture` |
| `WIN_CSC_LINK_B64` | secret | base64 PFX for Windows code signing, passed to electron-builder as `CSC_LINK`. Without it the installer builds unsigned. |
| `WIN_CSC_KEY_PASSWORD` | secret | its password |

A tagged release without these three fails the android job before upload; a
`workflow_dispatch` dry run may omit them and gets a debug-signed
`-debug-DONOTPUBLISH.apk` for inspection only.

### Keystore continuity, backup, and recovery

The signing keystore is the app's identity. **Keep one keystore forever:**
Android refuses to update an installed app whose signing key changed — users
would have to uninstall first (settings and the capture queue survive an update,
not an uninstall). If the keystore or its password is ever lost, every existing
BookCapture install is stranded: the only path forward is a new keystore, a new
`applicationId`, and a fresh install for everyone.

So treat it as unrecoverable-if-lost and back it up accordingly:

- **The canonical copy** lives at `~/.whl-release/bookcapture.jks`, with its
  password/alias in `bookcapture-keystore-info.txt` beside it. Both are outside
  the repo and never committed.
- **Keep at least one off-machine backup** of `bookcapture.jks` **and** its
  password, stored together (a password without the keystore, or vice versa, is
  useless). A password manager entry plus an encrypted copy in separate storage
  is enough; the file is a few KB.
- **CI holds only a copy, not the source of truth.** `ANDROID_KEYSTORE_B64` is
  base64 of the same `.jks`; regenerate it from the local file with
  `base64 -w0 ~/.whl-release/bookcapture.jks` on Linux, or this raw-base64
  PowerShell command on Windows:
  `[Convert]::ToBase64String([IO.File]::ReadAllBytes("$HOME/.whl-release/bookcapture.jks"))`.
  Do not use `certutil -encode`, which adds PEM headers that the workflow's
  decoder does not accept. Losing the GitHub secret is recoverable from the
  local keystore; losing the local keystore is not.
- **The public certificate identity is pinned in the repo.**
  `android/BookCapture/release-signing-cert.sha256` contains the SHA-256 digest
  of the certificate used for existing installs. It is public metadata, not key
  material. The workflow requires every non-debug APK, including a signed dry
  run, to match it before upload. Do not change it unless intentionally ending
  update compatibility with every existing BookCapture install.
- **Verify recoverability periodically:** `keytool -list -keystore
  ~/.whl-release/bookcapture.jks -alias bookcapture` should list the key with the
  stored password. The release pipeline also prints the published APK's signer
  DN and certificate SHA-256 digest in its run summary after enforcing the
  tracked fingerprint.

## By hand (no CI)

`tools/release_publish.py` does the registration half on its own, with the
desktop's Supabase credentials (Settings → Sync) or `SUPABASE_URL` /
`SUPABASE_KEY` in the environment:

```
# upload a local file to the public `releases` bucket and register it
python tools/release_publish.py BookCapture-0.2.0.apk --platform android --version 0.2.0

# register a file hosted elsewhere (sha256/bytes computed if the file is local)
python tools/release_publish.py LibraryTool-Setup-0.4.0.exe \
    --url https://github.com/…/LibraryTool-Setup-0.4.0.exe --platform windows --version 0.4.0
```

The bucket route is for small files — Supabase's free tier caps one object
around 50 MB, which the APK fits and the installer does not. Re-publishing the
same platform/version/channel replaces the row, so corrections are safe.

## Known caveats

- Code signing is optional: the installer step signs when the
  `WIN_CSC_LINK_B64` / `WIN_CSC_KEY_PASSWORD` secrets hold a cert and builds
  unsigned otherwise. Until a CA-issued cert fills them, public downloaders
  see the SmartScreen warning on first run (a self-managed cert clears it
  only on machines that trust its root CA).
- The 40 MB `copyright_renewals.csv` is deliberately absent from the public
  mirror; the sidecar spec skips missing data files, so the CI build simply
  ships without that dataset — the in-app setup guide offers it (and the other
  large databases) as a download on first run.
- A version only auto-updates existing installs if `latest.yml` made it onto
  the newest GitHub Release — the artifact step fails loudly if it is missing,
  but don't delete it from a release by hand. Two gotchas baked into the
  workflow: electron-builder writes `latest.yml` even for a prerelease (the
  GitHub prerelease flag, not the manifest name, is what shields stable
  updaters), and the artifact glob must stay exactly `latest.yml` — `*.yml`
  once swept in `builder-debug.yml`.
