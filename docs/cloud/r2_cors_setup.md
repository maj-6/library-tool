# R2 CORS setup — make the website reader load scans

## The problem

The website archival reader (`website/read.html` + `website/assets/read.js`,
pdf.js) streams each volume's PDF with **cross-origin HTTP Range requests**.
Cloudflare's *managed* public URLs — `https://pub-<hash>.r2.dev/...` — send **no
`Access-Control-Allow-Origin` header and answer the CORS preflight with `403`**.
So every scan is blocked browser-side and the reader shows
*"Cannot open … blocked by cross-origin rules."*

This is inherent to r2.dev URLs: **bucket CORS rules do not apply to them.** They
apply only to a **custom domain** (and to the S3 API). The fix is to serve the
same objects from a custom domain that carries the CORS policy, then repoint the
stored `volumes.pdf_url` values. The object keys are identical across hosts
(`volumes/<slug>.pdf`), so only the scheme+host changes.

Verified failing example (as of 2026-07-11): 1 published volume,
`libellus-de-materia-medicae-1727`, at
`pub-36b4d09e456a460484b0a4c99a7c9abe.r2.dev`. A cross-origin GET returns `206`
with a valid `Content-Range` but **no** `Access-Control-Allow-Origin`; the
`OPTIONS` preflight returns `403`.

## Fix (Option 1, recommended — R2 custom domain)

All steps except #4 are one-time.

1. **Connect a custom domain to the bucket.** Cloudflare dashboard → R2 → your
   bucket → **Settings → Public access → Custom Domains → Connect Domain**. Use a
   subdomain on a zone Cloudflare already manages, e.g. `files.<yourdomain>`.
   Cloudflare provisions the cert and a CORS-capable public host. (You can leave
   the r2.dev managed URL enabled or disabled — the app uses whatever base you
   configure in step 3.)

2. **Apply the CORS policy.** Same bucket → **Settings → CORS Policy → Add/Edit**
   and paste [`r2-cors.json`](./r2-cors.json):

   ```json
   [
     {
       "AllowedOrigins": ["https://maj-6.github.io"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedHeaders": ["range", "if-range", "content-type"],
       "ExposeHeaders": ["Content-Range", "Content-Length", "Accept-Ranges", "ETag"],
       "MaxAgeSeconds": 86400
     }
   ]
   ```

   `range` in `AllowedHeaders` lets the preflight pass; the `ExposeHeaders` list
   is what lets pdf.js read the range/length headers it needs. Add the site's own
   custom domain to `AllowedOrigins` too if you ever move off `maj-6.github.io`.

3. **Point new publishes at the custom domain.** In the desktop app: **Settings →
   Sync → R2 public base** (`r2PublicBase`) → `https://files.<yourdomain>`. Every
   future published volume's `pdf_url` is then built on the CORS-capable host.

4. **Repoint the volumes already published** (currently 1 row):

   ```bash
   # dry run first — read-only, the anon key is enough
   python3 tools/fix_pdf_url_host.py --to https://files.<yourdomain>
   # then write (needs the service_role key: Settings > Sync, or SUPABASE_KEY env)
   python3 tools/fix_pdf_url_host.py --to https://files.<yourdomain> --apply
   ```

5. **Verify.** The CORS header should now be present, and the reader should load:

   ```bash
   curl -s -D - -o /dev/null -H "Origin: https://maj-6.github.io" -r 0-1023 \
     https://files.<yourdomain>/volumes/libellus-de-materia-medicae-1727.pdf \
     | grep -i 'access-control-allow-origin'
   # expect: access-control-allow-origin: https://maj-6.github.io
   ```

   Then open `https://maj-6.github.io/library-tool/read.html?slug=libellus-de-materia-medicae-1727`.

## Option 2 (fallback — serve from Supabase storage)

The reader already falls back to the Supabase public `volumes` bucket when a row
has `pdf_path` set and no `pdf_url` (`website/assets/data.js`). Supabase storage
URLs send CORS. To use this instead, upload the PDFs to the Supabase `volumes`
bucket and clear `pdf_url` (leaving `pdf_path`). Downside: PDFs are large
(~80 MB each) and may exceed the Supabase free-tier storage cap, which is why R2
+ a custom domain is preferred.
