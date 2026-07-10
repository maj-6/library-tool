# Releasing

The Downloads page on the website renders the `releases` table in Supabase,
newest row per platform. Publishing a release means getting a row into that
table with a URL people can download; the static site itself never changes or
redeploys for a release.

Two ways a row gets there:

## The pipeline (the normal way)

`.github/workflows/release.yml` runs on the public repo when a `v*` tag is
pushed:

1. **android** builds `android/BookCapture` (`assembleRelease`, signed with the
   keystore secret if present, the runner's debug key if not) and names the APK
   after its gradle `versionName`.
2. **desktop** freezes the Flask sidecar with PyInstaller and runs
   electron-builder on a Windows runner, producing
   `LibraryTool-<package.json version>.msi`.
3. **publish** attaches both files to a GitHub Release named after the tag,
   then registers each in the `releases` table via `tools/release_publish.py`
   (URL → the GitHub Release asset). The Downloads page shows them immediately.

So cutting a release is:

```
# bump versions first if warranted:
#   android/BookCapture/app/build.gradle.kts   versionCode + versionName
#   desktop/package.json                       version
git tag v3.1
git push <public> master:main --follow-tags    # or push the tag with the next mirror publish
```

A `workflow_dispatch` run of the same workflow is a dry run: both apps build
and the artifacts are inspectable, nothing is published.

### One-time repository setup (public repo, Settings → Secrets and variables)

| name | kind | purpose |
|---|---|---|
| `SUPABASE_URL` | variable | already set for the pages workflow |
| `SUPABASE_SERVICE_ROLE_KEY` | secret | writes to `releases` (anon is read-only by RLS). Without it the GitHub Release still happens; the register step warns and skips. |
| `ANDROID_KEYSTORE_B64` | secret | base64 of the signing keystore. Local copy: `~/.whl-release/bookcapture.jks(.b64)`, password in `bookcapture-keystore-info.txt` next to it. |
| `ANDROID_KEYSTORE_PASSWORD` | secret | its password |
| `ANDROID_KEY_ALIAS` | secret | `bookcapture` |

Keep one keystore forever: Android refuses to update an installed app whose
signing key changed — users would have to uninstall first (settings and the
capture queue survive an update, not an uninstall).

## By hand (no CI)

`tools/release_publish.py` does the registration half on its own, with the
desktop's Supabase credentials (Settings → Sync) or `SUPABASE_URL` /
`SUPABASE_KEY` in the environment:

```
# upload a local file to the public `releases` bucket and register it
python tools/release_publish.py BookCapture-1.0.apk --platform android --version 1.0

# register a file hosted elsewhere (sha256/bytes computed if the file is local)
python tools/release_publish.py LibraryTool-3.0.0.msi \
    --url https://github.com/…/LibraryTool-3.0.0.msi --platform windows --version 3.0.0
```

The bucket route is for small files — Supabase's free tier caps one object
around 50 MB, which the APK fits and the installer does not. Re-publishing the
same platform/version/channel replaces the row, so corrections are safe.

## Known caveats

- The MSI is unsigned (no code-signing cert), so SmartScreen will warn on
  first run. electron-builder's MSI target also leans on WiX 3; if the Windows
  runner ever balks, the `nsis` target is the planned v3 direction anyway.
- The 40 MB `copyright_renewals.csv` is deliberately absent from the public
  mirror; the sidecar spec skips missing data files, so the CI build simply
  ships without that dataset.
