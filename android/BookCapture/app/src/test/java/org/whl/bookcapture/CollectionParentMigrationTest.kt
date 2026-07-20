package org.whl.bookcapture

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CollectionParentMigrationTest {

    private val sql = File(
        "../../../docs/cloud/migrations/016_collection_parents.sql",
    ).readText().lowercase()

    @Test
    fun migrationAddsAnIndexedSelfReferenceAndCycleGuards() {
        assertTrue(sql.contains("add column if not exists parent_id uuid"))
        assertTrue(sql.contains("foreign key (parent_id) references public.collections(id)"))
        assertTrue(sql.contains("on delete set null"))
        assertTrue(sql.contains("check (parent_id is null or parent_id <> id)"))
        assertTrue(sql.contains("collections_parent_id_idx"))
        assertTrue(sql.contains("for each statement"))
        assertTrue(sql.contains("pg_advisory_xact_lock"))
        assertTrue(sql.contains("for each row"))
        assertTrue(sql.contains("collection parent cycle"))
    }

    @Test
    fun migrationKeepsTheExistingRlsContractAndUsesExplicitGrants() {
        assertTrue(sql.contains("alter table public.collections enable row level security"))
        assertTrue(sql.contains("grant insert (parent_id) on public.collections to authenticated"))
        assertTrue(sql.contains("grant update (parent_id) on public.collections to authenticated"))
        assertTrue(sql.contains("revoke all on public.collections from anon"))
        assertFalse(sql.contains("grant all on public.collections to authenticated"))
        assertTrue(sql.contains("security invoker"))
        assertTrue(sql.contains("set search_path = ''"))
        assertTrue(sql.contains("values ('016_collection_parents')"))
    }
}
