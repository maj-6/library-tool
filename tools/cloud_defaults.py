"""The Library Tool cloud this app is built for — baked in so a fresh install
talks to the cloud with zero configuration.

Only PUBLIC identifiers live here: the project URL and the anon key — the
same pair the website ships to every visitor (website/assets/config.js).
Row-level security is what protects the project: the anon key can read
volumes and releases and write nothing, and signing in only unlocks what the
policies grant that user. The service_role key is NEVER shipped; owner
machines may add it for privileged publishing and working-store maintenance.
Phone capture uses the signed-in user's session and needs no user-supplied
Supabase key. Anything set in Settings overrides these, so a fork can point
the app at its own project.

tests/test_cloud_defaults.py asserts the baked key's JWT role is `anon` —
a service key can not be committed here without failing CI.
"""

SUPABASE_URL = "https://vnjcocsyvshnbbwfblsp.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InZuamNvY3N5dnNobmJid2ZibHNwIiwicm9sZSI6"
    "ImFub24iLCJpYXQiOjE3ODM2NDg3MDQsImV4cCI6MjA5OTIyNDcwNH0."
    "bQ9YOFntTfsbNngl5rcm4mB2DP7gq67zEPufdxHkzkI"
)

# The public website (GitHub Pages). The desktop has no web server of its own —
# it runs on a throwaway loopback port — so the account-confirmation email must
# land somewhere real and stable. It lands on WEBSITE_URL/confirmed.html, which
# tells the user to return to the app and sign in. Signup sends this as GoTrue's
# `redirect_to`; the Supabase project's Site URL should also point here, so the
# link works even for the default (no-redirect) flow. Settings > Sync can
# override it (cloudSiteUrl) for a fork pointed at its own project + site.
WEBSITE_URL = "https://maj-6.github.io/library-tool"
EMAIL_CONFIRM_PATH = "/confirmed.html"

# Offline-search databases (the Open Library indexes, the copyright-renewal
# CSV). Search is LOCAL-FIRST: if the file is already in the data folder (copied
# from a flash drive, or previously downloaded) it is used as-is with no network
# and no URL. These defaults only matter for the *download* fallback: a per-file
# URL in DB_URLS wins, else `DB_BASE_URL/<filename>`. Both are empty by default,
# so out of the box the app relies purely on local copies; fill in the bucket
# base once known (Settings > Sync still overrides per database). Public
# identifiers only — never a signed/credentialed URL (those belong in Settings).
DB_BASE_URL = ""      # e.g. "https://my-bucket.s3.us-east-1.amazonaws.com/whl-db"
DB_URLS = {}          # name -> full URL, overrides DB_BASE_URL for that database
