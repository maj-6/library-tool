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
  - flags the GitHub Release **prerelease**, so ordinary stable installs never
    receive it. Installed prereleases continue following the prerelease line;
    stable installs can opt in from Settings.
  - registers the `releases` row on the **`alpha` / `beta` / `rc` channel**, so
    it appears in the Downloads page's **Other downloads** section instead of
    replacing the stable card.

A plain `vX.Y.Z` tag (no suffix) is the stable path — GitHub "Latest", the
stable auto-update channel, and the main Downloads list. Reserve it for builds
that actually meet the bar above.

Two ways a row gets there:

## The pipeline (the normal way)

`.github/workflows/release.yml` runs on the public repo when a `v*` tag is
pushed:

1. **android** builds `android/BookCapture` (`assembleRelease`) and names the APK
   after its gradle `versionName`. A tag push **requires** the release keystore
   secret: without it the android job fails before the APK is uploaded, because a
   debug-signed build can't update an existing install. The desktop release is
   independent and still ships. The job then verifies the APK's signer (via
   `apksigner`) and refuses a debug-signed tagged build. A `workflow_dispatch`
   dry run without the secret still builds, debug-signed and suffixed
   `-debug-DONOTPUBLISH.apk` so it can't be mistaken for a release.
2. **desktop** freezes the Flask sidecar with PyInstaller and runs
   electron-builder on a Windows runner, producing the NSIS installer
   `LibraryTool-Setup-<package.json version>.exe` **plus `latest.yml` and the
   `.blockmap`** — those two are the auto-update channel: installed apps check
   the newest GitHub Release at startup and read them to fetch the update.
3. **publish** attaches everything to a GitHub Release named after the tag,
   then registers the APK and the installer in the `releases` table via
   `tools/release_publish.py` (URL → the GitHub Release asset). The Downloads
   page shows them immediately, and existing installs pick the update up on
   their next launch.

So cutting a release is:

```
# bump versions first if warranted:
#   android/BookCapture/app/build.gradle.kts   versionCode + versionName
#   desktop/package.json                       version
git tag v0.4.0
git push <public> master:main --follow-tags    # or push the tag with the next mirror publish
```

A `workflow_dispatch` run of the same workflow is a dry run: both apps build
and the artifacts are inspectable, nothing is published.

### One-time repository setup (public repo, Settings → Secrets and variables)

| name | kind | purpose |
|---|---|---|
| `SUPABASE_URL` | variable | already set for the pages workflow |
| `SUPABASE_SERVICE_ROLE_KEY` | secret | writes to `releases` (anon is read-only by RLS). Without it the GitHub Release still happens; the register step warns and skips. |
| `ANDROID_KEYSTORE_B64` | secret | **required to publish Android on a tag.** base64 of the signing keystore. Local copy: `~/.whl-release/bookcapture.jks(.b64)`, password in `bookcapture-keystore-info.txt` next to it. |
| `ANDROID_KEYSTORE_PASSWORD` | secret | its password |
| `ANDROID_KEY_ALIAS` | secret | `bookcapture` |

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
- **Verify recoverability periodically:** `keytool -list -keystore
  ~/.whl-release/bookcapture.jks -alias bookcapture` should list the key with the
  stored password. The release pipeline also prints the published APK's signer
  DN and certificate SHA-256 digest in its run summary, so the exact signing
  certificate can be compared across releases.

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

- The installer is unsigned (no code-signing cert), so SmartScreen will warn
  on first run.
- The 40 MB `copyright_renewals.csv` is deliberately absent from the public
  mirror; the sidecar spec skips missing data files, so the CI build simply
  ships without that dataset — the in-app setup guide offers it (and the other
  large databases) as a download on first run.
- A version only auto-updates existing installs if `latest.yml` made it onto
  the newest GitHub Release — the artifact step fails loudly if it is missing,
  but don't delete it from a release by hand.
