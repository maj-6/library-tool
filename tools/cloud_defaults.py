"""The Library Tool cloud this app is built for — baked in so a fresh install
talks to the cloud with zero configuration.

Only PUBLIC identifiers live here: the project URL and the anon key — the
same pair the website ships to every visitor (website/assets/config.js).
Row-level security is what protects the project: the anon key can read
volumes and releases and write nothing, and signing in only unlocks what the
policies grant that user. The service_role key is NEVER shipped; owner
machines paste it in Settings > Sync for the capture / publish / store-sync
pipelines. Anything set in Settings overrides these, so a fork can point the
app at its own project.

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
