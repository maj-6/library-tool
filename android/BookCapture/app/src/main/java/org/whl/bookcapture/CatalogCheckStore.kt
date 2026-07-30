package org.whl.bookcapture

import org.json.JSONObject
import java.io.File
import java.util.UUID

internal const val CATALOG_CHECK_FILE = "catalog_check.json"
internal const val CATALOG_CHECK_ERROR_MAX_CHARS = 1_000

private const val CATALOG_CHECK_SCHEMA = "org.whl.bookcapture.catalog-check"
private const val CATALOG_CHECK_VERSION = 1
private const val CATALOG_CHECK_MAX_BYTES = 64 * 1024
private const val CATALOG_CHECK_REQUEST_ID_MAX_CHARS = 160
private const val CATALOG_CHECK_ASSET_ID_MAX_CHARS = 200
private const val CATALOG_CHECK_FIELD_MAX_CHARS = 500
private const val CATALOG_CHECK_YEAR_MAX_CHARS = 40
private const val CATALOG_CHECK_KEY_MAX_CHARS = 200
private const val CATALOG_CHECK_PERMALINK_MAX_CHARS = 2_048
private const val CATALOG_CHECK_MAX_PAGE = 100_000

private val CATALOG_CHECK_REQUEST_ID =
    Regex("[A-Za-z0-9](?:[A-Za-z0-9._-]{0,159})")
private val CATALOG_CHECK_ASSET_ID =
    Regex("[A-Za-z0-9](?:[A-Za-z0-9._-]{0,199})")

internal enum class CatalogCheckState(val wireValue: String) {
    PENDING("pending"),
    SUCCEEDED("succeeded"),
    FAILED("failed"),
}

internal enum class CatalogCheckWhlStatus(val wireValue: String) {
    YES("yes"),
    DRAFT("draft"),
    NO("no"),
    UNAVAILABLE("unavailable"),
}

internal data class CatalogCheckBibliography(
    val title: String = "",
    val author: String = "",
    val year: String = "",
) {
    companion object {
        val EMPTY = CatalogCheckBibliography()
    }
}

/**
 * The bounded part of a CH proposal needed to present one check result.
 *
 * This deliberately does not retain [ChCandidate.fields]. A check result is
 * evidence about list membership, not a second metadata-merge payload.
 */
internal data class CatalogCheckChCandidateSummary(
    val key: String,
    val title: String,
    val author: String = "",
    val year: String = "",
    val score: Double = 0.0,
) {
    companion object {
        fun from(candidate: ChCandidate): CatalogCheckChCandidateSummary =
            CatalogCheckChCandidateSummary(
                key = candidate.key,
                title = candidate.title,
                author = candidate.author,
                year = candidate.year,
                score = candidate.score,
            )
    }
}

internal data class CatalogCheckChResult(
    /** False means the bundled CH index could not be consulted. */
    val searched: Boolean,
    /** Null with searched=true is the durable "no CH match" answer. */
    val candidate: CatalogCheckChCandidateSummary? = null,
)

/**
 * A neutral WHL summary kept independent of the runtime index representation.
 *
 * The WHL index can evolve without making previously persisted check results
 * unreadable. The permalink is retained with the exact candidate shown.
 */
internal data class CatalogCheckWhlCandidateSummary(
    val title: String,
    val author: String = "",
    val year: String = "",
    val permalink: String = "",
    val score: Double = 0.0,
)

internal data class CatalogCheckWhlResult(
    val status: CatalogCheckWhlStatus,
    val candidate: CatalogCheckWhlCandidateSummary? = null,
)

/**
 * One entry's latest catalogue-check request or terminal result.
 *
 * A failed result may still carry bibliography or one completed lookup. That
 * makes a partial failure explainable without turning it into success.
 */
internal data class CatalogCheckRecord(
    val requestId: String,
    val targetPage: Int,
    val state: CatalogCheckState,
    /** Stable photo-contract identity; page remains a human-readable fallback. */
    val targetAssetId: String? = null,
    val bibliography: CatalogCheckBibliography = CatalogCheckBibliography.EMPTY,
    val ch: CatalogCheckChResult? = null,
    val whl: CatalogCheckWhlResult? = null,
    val error: String = "",
)

/**
 * Durable hand-off between CameraX, background OCR/extraction, and the capture UI.
 *
 * Only the latest request is retained. Terminal writes compare the request id
 * under the same process-local lock as replacement, so a slow result from an
 * older photo cannot overwrite a newer check. Every physical write uses
 * [Entries.atomicWrite].
 */
internal object CatalogCheckStore {
    private val lock = Any()

    fun read(dir: File): CatalogCheckRecord? = synchronized(lock) {
        readFile(File(dir, CATALOG_CHECK_FILE))
    }

    /**
     * Replace any older request/result with a fresh pending request.
     *
     * [requestId] is injectable for deterministic callers/tests; production
     * callers normally use the UUID default.
     */
    fun request(
        dir: File,
        page: Int,
        requestId: String = UUID.randomUUID().toString(),
        targetAssetId: String? = null,
    ): CatalogCheckRecord = synchronized(lock) {
        require(page in 1..CATALOG_CHECK_MAX_PAGE) { "invalid check target page" }
        require(validRequestId(requestId)) { "invalid check request id" }
        require(targetAssetId == null || validAssetId(targetAssetId)) {
            "invalid check target asset id"
        }
        val pending = CatalogCheckRecord(
            requestId = requestId,
            targetPage = page,
            state = CatalogCheckState.PENDING,
            targetAssetId = targetAssetId,
        )
        write(dir, pending)
        pending
    }

    /**
     * Bind a request created before photo registration, or retarget its page
     * hint after photo compaction. Only the matching pending request can move.
     *
     * A terminal request is returned only when the requested binding is
     * already identical, making retries idempotent without changing evidence.
     */
    fun bindOrRetarget(
        dir: File,
        requestId: String,
        targetAssetId: String? = null,
        page: Int? = null,
    ): CatalogCheckRecord? = synchronized(lock) {
        require(targetAssetId == null || validAssetId(targetAssetId)) {
            "invalid check target asset id"
        }
        require(page == null || page in 1..CATALOG_CHECK_MAX_PAGE) {
            "invalid check target page"
        }
        if (!validRequestId(requestId)) return@synchronized null
        val current = readFile(File(dir, CATALOG_CHECK_FILE))
            ?: return@synchronized null
        if (current.requestId != requestId) return@synchronized null
        val nextPage = page ?: current.targetPage
        val nextAssetId = targetAssetId ?: current.targetAssetId
        if (current.state != CatalogCheckState.PENDING) {
            return@synchronized current.takeIf {
                it.targetAssetId == nextAssetId && it.targetPage == nextPage
            }
        }
        val bound = current.copy(
            targetPage = nextPage,
            targetAssetId = nextAssetId,
        )
        if (bound == current) return@synchronized current
        write(dir, bound)
        bound
    }

    /**
     * Commit a successful result only if [requestId] still names the latest
     * pending request. A repeated terminal callback is idempotent.
     */
    fun complete(
        dir: File,
        requestId: String,
        bibliography: CatalogCheckBibliography,
        ch: CatalogCheckChResult,
        whl: CatalogCheckWhlResult,
    ): CatalogCheckRecord? = finish(dir, requestId) { current ->
        current.copy(
            state = CatalogCheckState.SUCCEEDED,
            bibliography = bibliography,
            ch = ch,
            whl = whl,
            error = "",
        )
    }

    /**
     * Commit a bounded failure only if [requestId] is still current.
     *
     * Optional partial values let a caller retain successful extraction or one
     * completed list lookup when the later stage failed.
     */
    fun fail(
        dir: File,
        requestId: String,
        error: String,
        bibliography: CatalogCheckBibliography = CatalogCheckBibliography.EMPTY,
        ch: CatalogCheckChResult? = null,
        whl: CatalogCheckWhlResult? = null,
    ): CatalogCheckRecord? = finish(dir, requestId) { current ->
        current.copy(
            state = CatalogCheckState.FAILED,
            bibliography = bibliography,
            ch = ch,
            whl = whl,
            error = error,
        )
    }

    private fun finish(
        dir: File,
        requestId: String,
        terminal: (CatalogCheckRecord) -> CatalogCheckRecord,
    ): CatalogCheckRecord? = synchronized(lock) {
        if (!validRequestId(requestId)) return@synchronized null
        val current = readFile(File(dir, CATALOG_CHECK_FILE))
            ?: return@synchronized null
        if (current.requestId != requestId) return@synchronized null
        // WorkManager may retry after the atomic write landed but before the
        // worker returned. Never rewrite or reverse an existing terminal result.
        if (current.state != CatalogCheckState.PENDING) return@synchronized current
        val result = normalized(terminal(current))
        write(dir, result)
        result
    }

    private fun write(dir: File, record: CatalogCheckRecord) {
        Entries.atomicWrite(File(dir, CATALOG_CHECK_FILE), encode(record))
    }

    private fun readFile(file: File): CatalogCheckRecord? = try {
        if (!file.isFile || file.length() !in 1..CATALOG_CHECK_MAX_BYTES.toLong()) null
        else parse(file.readText())
    } catch (_: Exception) {
        null
    }

    /** Pure tolerant decoder used by JVM tests and future migrations. */
    internal fun parse(text: String?): CatalogCheckRecord? {
        if (text.isNullOrBlank() ||
            text.toByteArray(Charsets.UTF_8).size > CATALOG_CHECK_MAX_BYTES
        ) return null
        return try {
            val root = JSONObject(text)
            if (root.text("schema", 80) != CATALOG_CHECK_SCHEMA ||
                root.wholeInt("version") != CATALOG_CHECK_VERSION
            ) return null
            val requestId = root.exactText(
                "request_id",
                CATALOG_CHECK_REQUEST_ID_MAX_CHARS,
            )
                ?.takeIf(::validRequestId) ?: return null
            val targetPage = root.wholeInt("target_page")
                ?.takeIf { it in 1..CATALOG_CHECK_MAX_PAGE } ?: return null
            val targetAssetId = root.exactText(
                "target_asset_id",
                CATALOG_CHECK_ASSET_ID_MAX_CHARS,
            )?.takeIf(::validAssetId)
            val state = when (root.text("state", 24)) {
                CatalogCheckState.PENDING.wireValue -> CatalogCheckState.PENDING
                CatalogCheckState.SUCCEEDED.wireValue -> CatalogCheckState.SUCCEEDED
                CatalogCheckState.FAILED.wireValue -> CatalogCheckState.FAILED
                else -> return null
            }
            normalized(
                CatalogCheckRecord(
                    requestId = requestId,
                    targetPage = targetPage,
                    state = state,
                    targetAssetId = targetAssetId,
                    bibliography = parseBibliography(root.optJSONObject("bibliography")),
                    ch = parseCh(root.optJSONObject("ch")),
                    whl = parseWhl(root.optJSONObject("whl")),
                    error = root.text("error", CATALOG_CHECK_ERROR_MAX_CHARS),
                ),
            )
        } catch (_: Exception) {
            null
        }
    }

    /** Pure encoder; values are bounded before they reach disk. */
    internal fun encode(record: CatalogCheckRecord): String {
        require(validRequestId(record.requestId)) { "invalid check request id" }
        require(record.targetPage in 1..CATALOG_CHECK_MAX_PAGE) {
            "invalid check target page"
        }
        require(record.targetAssetId == null || validAssetId(record.targetAssetId)) {
            "invalid check target asset id"
        }
        val clean = normalized(record)
        return JSONObject()
            .put("schema", CATALOG_CHECK_SCHEMA)
            .put("version", CATALOG_CHECK_VERSION)
            .put("request_id", clean.requestId)
            .put("target_page", clean.targetPage)
            .put("target_asset_id", clean.targetAssetId ?: JSONObject.NULL)
            .put("state", clean.state.wireValue)
            .put("bibliography", bibliographyJson(clean.bibliography))
            .put("ch", clean.ch?.let(::chJson) ?: JSONObject.NULL)
            .put("whl", clean.whl?.let(::whlJson) ?: JSONObject.NULL)
            .put("error", clean.error)
            .toString()
    }

    private fun normalized(record: CatalogCheckRecord): CatalogCheckRecord {
        val bibliography = CatalogCheckBibliography(
            title = bounded(record.bibliography.title, CATALOG_CHECK_FIELD_MAX_CHARS),
            author = bounded(record.bibliography.author, CATALOG_CHECK_FIELD_MAX_CHARS),
            year = bounded(record.bibliography.year, CATALOG_CHECK_YEAR_MAX_CHARS),
        )
        val ch = record.ch?.let { result ->
            val candidate = result.candidate?.let {
                CatalogCheckChCandidateSummary(
                    key = bounded(it.key, CATALOG_CHECK_KEY_MAX_CHARS),
                    title = bounded(it.title, CATALOG_CHECK_FIELD_MAX_CHARS),
                    author = bounded(it.author, CATALOG_CHECK_FIELD_MAX_CHARS),
                    year = bounded(it.year, CATALOG_CHECK_YEAR_MAX_CHARS),
                    score = score(it.score),
                )
            }?.takeIf { it.key.isNotEmpty() }
            CatalogCheckChResult(
                searched = result.searched || candidate != null,
                candidate = candidate,
            )
        }
        val whl = record.whl?.let { result ->
            CatalogCheckWhlResult(
                status = result.status,
                candidate = result.candidate?.let {
                    CatalogCheckWhlCandidateSummary(
                        title = bounded(it.title, CATALOG_CHECK_FIELD_MAX_CHARS),
                        author = bounded(it.author, CATALOG_CHECK_FIELD_MAX_CHARS),
                        year = bounded(it.year, CATALOG_CHECK_YEAR_MAX_CHARS),
                        permalink = bounded(it.permalink, CATALOG_CHECK_PERMALINK_MAX_CHARS),
                        score = score(it.score),
                    )
                }?.takeIf { it.title.isNotEmpty() || it.permalink.isNotEmpty() },
            )
        }
        return when (record.state) {
            CatalogCheckState.PENDING -> record.copy(
                bibliography = CatalogCheckBibliography.EMPTY,
                ch = null,
                whl = null,
                error = "",
            )
            CatalogCheckState.SUCCEEDED -> record.copy(
                bibliography = bibliography,
                ch = ch,
                whl = whl,
                error = "",
            )
            CatalogCheckState.FAILED -> record.copy(
                bibliography = bibliography,
                ch = ch,
                whl = whl,
                error = bounded(record.error, CATALOG_CHECK_ERROR_MAX_CHARS),
            )
        }
    }

    private fun parseBibliography(value: JSONObject?): CatalogCheckBibliography =
        CatalogCheckBibliography(
            title = value?.text("title", CATALOG_CHECK_FIELD_MAX_CHARS).orEmpty(),
            author = value?.text("author", CATALOG_CHECK_FIELD_MAX_CHARS).orEmpty(),
            year = value?.text("year", CATALOG_CHECK_YEAR_MAX_CHARS).orEmpty(),
        )

    private fun parseCh(value: JSONObject?): CatalogCheckChResult? {
        value ?: return null
        val candidate = value.optJSONObject("candidate")?.let { row ->
            CatalogCheckChCandidateSummary(
                key = row.text("key", CATALOG_CHECK_KEY_MAX_CHARS),
                title = row.text("title", CATALOG_CHECK_FIELD_MAX_CHARS),
                author = row.text("author", CATALOG_CHECK_FIELD_MAX_CHARS),
                year = row.text("year", CATALOG_CHECK_YEAR_MAX_CHARS),
                score = row.finiteDouble("score"),
            )
        }?.takeIf { it.key.isNotEmpty() }
        return CatalogCheckChResult(
            searched = value.boolean("searched") ?: (candidate != null),
            candidate = candidate,
        )
    }

    private fun parseWhl(value: JSONObject?): CatalogCheckWhlResult? {
        value ?: return null
        val status = when (value.text("status", 24)) {
            CatalogCheckWhlStatus.YES.wireValue -> CatalogCheckWhlStatus.YES
            CatalogCheckWhlStatus.DRAFT.wireValue -> CatalogCheckWhlStatus.DRAFT
            CatalogCheckWhlStatus.NO.wireValue -> CatalogCheckWhlStatus.NO
            CatalogCheckWhlStatus.UNAVAILABLE.wireValue -> CatalogCheckWhlStatus.UNAVAILABLE
            else -> return null
        }
        val candidate = value.optJSONObject("candidate")?.let { row ->
            CatalogCheckWhlCandidateSummary(
                title = row.text("title", CATALOG_CHECK_FIELD_MAX_CHARS),
                author = row.text("author", CATALOG_CHECK_FIELD_MAX_CHARS),
                year = row.text("year", CATALOG_CHECK_YEAR_MAX_CHARS),
                permalink = row.text("permalink", CATALOG_CHECK_PERMALINK_MAX_CHARS),
                score = row.finiteDouble("score"),
            )
        }?.takeIf { it.title.isNotEmpty() || it.permalink.isNotEmpty() }
        return CatalogCheckWhlResult(status, candidate)
    }

    private fun bibliographyJson(value: CatalogCheckBibliography): JSONObject =
        JSONObject()
            .put("title", value.title)
            .put("author", value.author)
            .put("year", value.year)

    private fun chJson(value: CatalogCheckChResult): JSONObject =
        JSONObject()
            .put("searched", value.searched)
            .put(
                "candidate",
                value.candidate?.let {
                    JSONObject()
                        .put("key", it.key)
                        .put("title", it.title)
                        .put("author", it.author)
                        .put("year", it.year)
                        .put("score", it.score)
                } ?: JSONObject.NULL,
            )

    private fun whlJson(value: CatalogCheckWhlResult): JSONObject =
        JSONObject()
            .put("status", value.status.wireValue)
            .put(
                "candidate",
                value.candidate?.let {
                    JSONObject()
                        .put("title", it.title)
                        .put("author", it.author)
                        .put("year", it.year)
                        .put("permalink", it.permalink)
                        .put("score", it.score)
                } ?: JSONObject.NULL,
            )

    private fun validRequestId(value: String): Boolean =
        value.length <= CATALOG_CHECK_REQUEST_ID_MAX_CHARS &&
            CATALOG_CHECK_REQUEST_ID.matches(value)

    private fun validAssetId(value: String): Boolean =
        value.length <= CATALOG_CHECK_ASSET_ID_MAX_CHARS &&
            value != "." && value != ".." &&
            CATALOG_CHECK_ASSET_ID.matches(value)

    private fun bounded(value: String, maximum: Int): String =
        value.trim().take(maximum)

    private fun score(value: Double): Double =
        value.takeIf { it.isFinite() }?.coerceIn(0.0, 1.0) ?: 0.0

    private fun JSONObject.text(name: String, maximum: Int): String =
        (opt(name) as? String)?.trim()?.take(maximum).orEmpty()

    /** Identity tokens are rejected, never truncated into a different token. */
    private fun JSONObject.exactText(name: String, maximum: Int): String? =
        (opt(name) as? String)?.trim()?.takeIf { it.length <= maximum }

    private fun JSONObject.wholeInt(name: String): Int? = when (val value = opt(name)) {
        is Byte, is Short, is Int -> (value as Number).toInt()
        is Long -> value.takeIf {
            it in Int.MIN_VALUE.toLong()..Int.MAX_VALUE.toLong()
        }?.toInt()
        else -> null
    }

    private fun JSONObject.boolean(name: String): Boolean? = opt(name) as? Boolean

    private fun JSONObject.finiteDouble(name: String): Double =
        (opt(name) as? Number)?.toDouble()?.let(::score) ?: 0.0
}
