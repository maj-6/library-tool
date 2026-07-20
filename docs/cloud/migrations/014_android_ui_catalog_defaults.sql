-- 014_android_ui_catalog_defaults — publish the packaged UI's initial overlay.
--
-- This intentionally contains only text. Maintainers can add hashed PNG icons
-- with tools/android_ui_catalog.py; the app and publisher enforce the same
-- payload limits independently.

update public.android_ui_catalog
set revision = 2,
    catalog = '{
      "schema": 1,
      "strings": {
        "app_name": "Library Tool Capture",
        "home_new_scan": "New scan",
        "home_tab_scans": "Scans",
        "home_tab_collections": "Collections"
      },
      "icons": {}
    }'::jsonb,
    updated_at = now(),
    updated_by = null
where id = 'current' and revision < 2;

insert into schema_migrations (id) values ('014_android_ui_catalog_defaults') on conflict do nothing;
