// Copy to assets/config.js (gitignored) and fill in.
//
//   python3 tools/cloud_setup.py anon-key
//
// The ANON key, never the service_role key. The anon key is designed to be
// public: row-level security is what protects the project, and it allows anon
// exactly two things — read `volumes` and read `releases`.
//
// Without this file the site falls back to fixtures/volumes.json, which is how
// it is developed before the cloud has any rows.

window.WHL_CONFIG = {
  supabaseUrl: "https://<project-ref>.supabase.co",
  supabaseAnonKey: "<anon public key>",
};
