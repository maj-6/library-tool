package org.whl.bookcapture

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

class CatalogCheckStoreTest {

    private val bibliography = CatalogCheckBibliography(
        title = "A Modern Herbal",
        author = "Mrs. M. Grieve",
        year = "1931",
    )
    private val ch = CatalogCheckChResult(
        searched = true,
        candidate = CatalogCheckChCandidateSummary(
            key = "ch-a-modern-herbal",
            title = "A Modern Herbal",
            author = "Grieve, M.",
            year = "1931",
            score = 0.96,
        ),
    )
    private val whl = CatalogCheckWhlResult(
        status = CatalogCheckWhlStatus.YES,
        candidate = CatalogCheckWhlCandidateSummary(
            title = "A Modern Herbal",
            author = "Maud Grieve",
            year = "1931",
            permalink = "https://worldherblibrary.org/catalog/a-modern-herbal/",
            score = 0.98,
        ),
    )

    @Test
    fun requestPersistsOneFreshPendingRecordAtomically() = withEntryDir { dir ->
        val first = CatalogCheckStore.request(dir, page = 2)
        val second = CatalogCheckStore.request(
            dir,
            page = 3,
            targetAssetId = "0a3f4512-7f9a-4e89-ae35-2fc247cc9554",
        )

        assertNotEquals(first.requestId, second.requestId)
        assertEquals(
            CatalogCheckRecord(
                requestId = second.requestId,
                targetPage = 3,
                state = CatalogCheckState.PENDING,
                targetAssetId = "0a3f4512-7f9a-4e89-ae35-2fc247cc9554",
            ),
            CatalogCheckStore.read(dir),
        )
        assertTrue(File(dir, CATALOG_CHECK_FILE).isFile)
        assertTrue(dir.listFiles().orEmpty().none { it.name.endsWith(".tmp") })
    }

    @Test
    fun completeRoundTripsBibliographyAndBothListResults() = withEntryDir { dir ->
        CatalogCheckStore.request(
            dir,
            4,
            "request-complete",
            "0a3f4512-7f9a-4e89-ae35-2fc247cc9554",
        )
        val completed = CatalogCheckStore.complete(
            dir = dir,
            requestId = "request-complete",
            bibliography = bibliography,
            ch = ch,
            whl = whl,
        )

        assertEquals(CatalogCheckState.SUCCEEDED, completed?.state)
        assertEquals(completed, CatalogCheckStore.read(dir))
        assertEquals(
            "0a3f4512-7f9a-4e89-ae35-2fc247cc9554",
            completed?.targetAssetId,
        )
        assertEquals("ch-a-modern-herbal", completed?.ch?.candidate?.key)
        assertEquals(CatalogCheckWhlStatus.YES, completed?.whl?.status)
        assertEquals(
            "https://worldherblibrary.org/catalog/a-modern-herbal/",
            completed?.whl?.candidate?.permalink,
        )
        val json = JSONObject(File(dir, CATALOG_CHECK_FILE).readText())
        assertEquals("org.whl.bookcapture.catalog-check", json.getString("schema"))
        assertEquals("succeeded", json.getString("state"))
    }

    @Test
    fun bindOrRetargetOnlyMovesTheMatchingPendingRequest() = withEntryDir { dir ->
        CatalogCheckStore.request(dir, 2, "old-request")
        CatalogCheckStore.request(dir, 3, "current-request")

        assertNull(CatalogCheckStore.bindOrRetarget(
            dir,
            requestId = "old-request",
            targetAssetId = "old-asset",
            page = 8,
        ))

        val retargeted = CatalogCheckStore.bindOrRetarget(
            dir,
            requestId = "current-request",
            page = 4,
        )
        assertNull(retargeted?.targetAssetId)
        assertEquals(4, retargeted?.targetPage)

        val bound = CatalogCheckStore.bindOrRetarget(
            dir,
            requestId = "current-request",
            targetAssetId = "current-asset",
        )
        assertEquals("current-asset", bound?.targetAssetId)
        assertEquals(4, bound?.targetPage)
        assertEquals(bound, CatalogCheckStore.read(dir))

        CatalogCheckStore.complete(dir, "current-request", bibliography, ch, whl)
        assertNull(CatalogCheckStore.bindOrRetarget(
            dir,
            requestId = "current-request",
            targetAssetId = "different-asset",
            page = 5,
        ))
        assertEquals(
            CatalogCheckStore.read(dir),
            CatalogCheckStore.bindOrRetarget(
                dir,
                requestId = "current-request",
                targetAssetId = "current-asset",
                page = 4,
            ),
        )
    }

    @Test
    fun searchedNoMatchAndEveryWhlVerdictRemainDistinct() {
        for (status in CatalogCheckWhlStatus.values()) {
            val record = CatalogCheckRecord(
                requestId = "request-${status.wireValue}",
                targetPage = 1,
                state = CatalogCheckState.SUCCEEDED,
                bibliography = bibliography,
                ch = CatalogCheckChResult(searched = true, candidate = null),
                whl = CatalogCheckWhlResult(status),
            )
            val parsed = CatalogCheckStore.parse(CatalogCheckStore.encode(record))

            assertTrue(parsed?.ch?.searched == true)
            assertNull(parsed?.ch?.candidate)
            assertEquals(status, parsed?.whl?.status)
            assertNull(parsed?.whl?.candidate)
        }
    }

    @Test
    fun staleOrRepeatedTerminalCallbacksCannotReplaceTheLatestResult() =
        withEntryDir { dir ->
            CatalogCheckStore.request(dir, 1, "old-request")
            CatalogCheckStore.request(dir, 2, "new-request")

            assertNull(CatalogCheckStore.complete(
                dir,
                "old-request",
                bibliography,
                ch,
                whl,
            ))
            assertEquals("new-request", CatalogCheckStore.read(dir)?.requestId)
            assertEquals(CatalogCheckState.PENDING, CatalogCheckStore.read(dir)?.state)

            val accepted = CatalogCheckStore.complete(
                dir,
                "new-request",
                bibliography,
                ch,
                whl,
            )
            val repeatedFailure = CatalogCheckStore.fail(
                dir,
                "new-request",
                "late failure",
            )
            assertEquals(accepted, repeatedFailure)
            assertEquals(CatalogCheckState.SUCCEEDED, CatalogCheckStore.read(dir)?.state)
        }

    @Test
    fun failBoundsTheErrorAndCanRetainPartialWork() = withEntryDir { dir ->
        CatalogCheckStore.request(dir, 7, "request-failed")
        val failed = CatalogCheckStore.fail(
            dir = dir,
            requestId = "request-failed",
            error = "  " + "x".repeat(CATALOG_CHECK_ERROR_MAX_CHARS + 50) + "  ",
            bibliography = bibliography,
            ch = CatalogCheckChResult(searched = true, candidate = null),
        )

        assertEquals(CatalogCheckState.FAILED, failed?.state)
        assertEquals(CATALOG_CHECK_ERROR_MAX_CHARS, failed?.error?.length)
        assertEquals(bibliography, failed?.bibliography)
        assertTrue(failed?.ch?.searched == true)
        assertNull(failed?.whl)
        assertEquals(failed, CatalogCheckStore.read(dir))
    }

    @Test
    fun decoderToleratesDamageButNeverInventsRequestIdentity() {
        assertNull(CatalogCheckStore.parse(null))
        assertNull(CatalogCheckStore.parse("{not json"))
        assertNull(CatalogCheckStore.parse("[]"))

        val valid = CatalogCheckStore.encode(
            CatalogCheckRecord(
                requestId = "request-parse",
                targetPage = 8,
                state = CatalogCheckState.SUCCEEDED,
                bibliography = bibliography,
                ch = ch,
                whl = whl,
            ),
        )
        assertNull(CatalogCheckStore.parse(
            valid.replace(
                "org.whl.bookcapture.catalog-check",
                "foreign.catalog-check",
            ),
        ))
        assertNull(CatalogCheckStore.parse(valid.replace("\"version\":1", "\"version\":2")))
        assertNull(CatalogCheckStore.parse(valid.replace("\"target_page\":8", "\"target_page\":0")))
        assertNull(CatalogCheckStore.parse(
            valid.replace("\"request_id\":\"request-parse\"", "\"request_id\":\"bad/id\""),
        ))
        assertNull(CatalogCheckStore.parse(
            valid.replace(
                "\"request_id\":\"request-parse\"",
                "\"request_id\":\"${"r".repeat(200)}\"",
            ),
        ))

        // Unknown optional result material is dropped while the durable request
        // envelope remains readable.
        val optionalDamage = valid
            .replace("\"key\":\"ch-a-modern-herbal\"", "\"key\":\"\"")
            .replace("\"status\":\"yes\"", "\"status\":\"future\"")
            .replace("\"score\":0.96", "\"score\":\"high\"")
        val parsed = CatalogCheckStore.parse(optionalDamage)
        assertEquals("request-parse", parsed?.requestId)
        assertNull(parsed?.ch?.candidate)
        assertFalse(parsed?.ch?.searched == false)
        assertNull(parsed?.whl)

        val invalidAsset = valid.replace(
            "\"target_asset_id\":null",
            "\"target_asset_id\":\"bad/asset\"",
        )
        val assetParsed = CatalogCheckStore.parse(invalidAsset)
        assertEquals("request-parse", assetParsed?.requestId)
        assertNull(assetParsed?.targetAssetId)

        val oversizedAsset = valid.replace(
            "\"target_asset_id\":null",
            "\"target_asset_id\":\"${"a".repeat(201)}\"",
        )
        assertNull(CatalogCheckStore.parse(oversizedAsset)?.targetAssetId)
    }

    @Test
    fun pureCodecBoundsCandidateFieldsAndScores() {
        val oversized = CatalogCheckRecord(
            requestId = "request-bounds",
            targetPage = 1,
            state = CatalogCheckState.SUCCEEDED,
            bibliography = CatalogCheckBibliography(" t ".repeat(400), " a ".repeat(400), "1931"),
            ch = CatalogCheckChResult(
                searched = false,
                candidate = CatalogCheckChCandidateSummary(
                    key = "key",
                    title = "title",
                    score = Double.POSITIVE_INFINITY,
                ),
            ),
            whl = CatalogCheckWhlResult(
                CatalogCheckWhlStatus.DRAFT,
                CatalogCheckWhlCandidateSummary(
                    title = "draft",
                    permalink = "https://example.test/" + "p".repeat(3_000),
                    score = -2.0,
                ),
            ),
        )

        val parsed = CatalogCheckStore.parse(CatalogCheckStore.encode(oversized))!!
        assertTrue(parsed.bibliography.title.length <= 500)
        assertEquals(parsed.bibliography.title.trim(), parsed.bibliography.title)
        assertEquals(0.0, parsed.ch?.candidate?.score ?: -1.0, 0.0)
        assertTrue(parsed.ch?.searched == true)
        assertEquals(2_048, parsed.whl?.candidate?.permalink?.length)
        assertEquals(0.0, parsed.whl?.candidate?.score ?: -1.0, 0.0)
    }

    private fun withEntryDir(block: (File) -> Unit) {
        val root = Files.createTempDirectory("catalog-check-").toFile()
        val dir = File(root, "capture-id").apply { mkdirs() }
        try {
            block(dir)
        } finally {
            root.deleteRecursively()
        }
    }
}
