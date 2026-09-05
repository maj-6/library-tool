from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "docs"
    / "cloud"
    / "migrations"
    / "035_collection_inventory_live_metadata.sql"
).read_text(encoding="utf-8")
FLAT = " ".join(MIGRATION.split())


def test_inventory_resolves_current_metadata_by_durable_uuid():
    view = FLAT.split(
        "create or replace view public.capture_collection_inventory", 1
    )[1].split("revoke all on public.capture_collection_inventory", 1)[0]

    assert (
        "left join public.collections as original_collection on "
        "original_collection.id::text = nullif(btrim(capture.meta ->> "
        "'scan_collection_id'), '')"
    ) in view
    assert (
        "left join public.collections as effective_collection on "
        "effective_collection.id = coalesce( membership.collection_id, "
        "original_collection.id )"
    ) in view
    assert (
        "coalesce( effective_collection.name, capture.meta ->> "
        "'scan_collection', '' ) as collection_name"
    ) in view
    assert "effective_collection.tag_id" not in view
    assert "effective_collection.name =" not in view


def test_inventory_keeps_provenance_shape_security_and_grants():
    assert "with (security_invoker = true) as" in FLAT
    assert (
        "nullif(btrim(capture.meta ->> 'scan_collection_id'), '') as "
        "original_collection_id"
    ) in FLAT
    assert (
        "coalesce( membership.collection_id::text, nullif(btrim(capture.meta "
        "->> 'scan_collection_id'), '') ) as collection_id"
    ) in FLAT
    assert (
        "revoke all on public.capture_collection_inventory from public, anon, "
        "authenticated, service_role;"
    ) in FLAT
    assert (
        "grant select on public.capture_collection_inventory to authenticated, "
        "service_role;"
    ) in FLAT
    assert "notify pgrst, 'reload schema';" in FLAT
    assert FLAT.endswith(
        "insert into schema_migrations (id) values "
        "('035_collection_inventory_live_metadata') on conflict do nothing;"
    )
