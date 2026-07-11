# Auth setup: confirmation links + email copy (Supabase dashboard)

Creating an account sends a confirmation email. Two things about it are
dashboard configuration — the app can't set them over the API — so they live
here.

## 1. Fix the "ERR_CONNECTION_REFUSED" link

A fresh Supabase project ships with **Site URL = `http://localhost:3000`**.
The confirmation link verifies the token and then redirects there — and since
nothing is serving localhost:3000, the browser shows *ERR_CONNECTION_REFUSED*.
The desktop app has no web server of its own (it runs on a throwaway loopback
port), so the link must land on the **public website** instead.

**Authentication → URL Configuration:**

- **Site URL:** `https://maj-6.github.io/library-tool`
- **Redirect URLs** (add both, or the wildcard):
  - `https://maj-6.github.io/library-tool/confirmed.html`
  - `https://maj-6.github.io/library-tool/**`

The app already sends `redirect_to = …/confirmed.html` at signup
(`tools/cloud_defaults.py` → `WEBSITE_URL`); GoTrue honours it only when it
matches an allow-listed Redirect URL, otherwise it falls back to the Site URL.
Setting **both** means the link works whichever path GoTrue takes.

`confirmed.html` (in `website/`) just tells the user the account is ready and
to return to the app and sign in — the desktop holds its own session, so the
browser can't hand it one. It also detects an expired/invalid link and says so.

> Fork pointing at your own project + site? Set the same two fields to your
> site, and put your site's base in **Settings → Sync → Cloud site URL**
> (`cloudSiteUrl`) so signup redirects there.

## 2. Project-specific email copy

The default Supabase confirmation email is generic ("Follow this link to
confirm your user"). Replace it:

**Authentication → Email Templates → Confirm signup**

- **Subject:** `Confirm your Library Tool account`
- **Message body:** paste [`email/confirm-signup.html`](email/confirm-signup.html)

The template keeps `{{ .ConfirmationURL }}` (Supabase fills in the verify link)
and inlines every style, since email clients drop `<style>` blocks. The other
templates (magic link, password recovery, email change) aren't used by the app
today; restyle them the same way if you ever enable those flows.

## Testing it

1. Create an account in the app with a real address.
2. The email should read as Library Tool, not stock Supabase.
3. Click **Confirm this email** → you land on `…/confirmed.html` ("Email
   confirmed"), **not** a connection error.
4. Back in the app, sign in with the same email and password.
