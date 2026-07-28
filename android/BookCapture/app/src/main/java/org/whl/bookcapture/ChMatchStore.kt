package org.whl.bookcapture

import org.json.JSONObject
import java.io.File

/**
 * The phone-owned CH verdict for a capture, persisted beside the entry.
 *
 * The approve/reject decision is made on the phone and is the user's, so it is
 * stored locally and immediately — before any sync, and independent of whether
 * the desktop or the cloud ever sees it. That also means a rejection survives a
 * re-scan of the same shelf: the next search can see that this candidate was
 * already refused.
 *
 * Written through [Entries.atomicWrite] so a crash mid-write cannot leave a
 * half-file that reads as "no decision" and re-prompts.
 */
internal object ChMatchStore {

    const val FILE_NAME = "ch_match.json"
    private const val SCHEMA = "org.whl.bookcapture.ch-match"
    private const val VERSION = 1

    fun read(dir: File): ChMatchState = parse(
        File(dir, FILE_NAME).takeIf { it.isFile }?.readText(),
    )

    /** Visible for tests: parsing is where the tolerance for bad data lives. */
    internal fun parse(text: String?): ChMatchState {
        if (text.isNullOrBlank()) return ChMatchState.NOT_SEARCHED
        return try {
            val json = JSONObject(text)
            // An unknown schema or a future version is not ours to interpret;
            // treating it as "not searched" re-runs the search rather than
            // acting on a record we may be misreading.
            if (json.optString("schema") != SCHEMA || json.optInt("version") != VERSION) {
                return ChMatchState.NOT_SEARCHED
            }
            val candidate = json.optJSONObject("candidate")?.let { obj ->
                val key = obj.optString("key").trim()
                if (key.isEmpty()) null
                else ChCandidate(
                    key = key,
                    title = obj.optString("title"),
                    author = obj.optString("author"),
                    year = obj.optString("year"),
                    score = obj.optDouble("score", 0.0).takeIf { it.isFinite() } ?: 0.0,
                    fields = obj.optJSONObject("fields")?.let { fields ->
                        buildMap {
                            fields.keys().forEach { key ->
                                val value = fields.optString(key).trim()
                                if (value.isNotEmpty()) put(key, value)
                            }
                        }
                    }.orEmpty(),
                )
            }
            ChMatchState(
                searched = json.optBoolean("searched", candidate != null),
                candidate = candidate,
                decision = when (json.optString("decision")) {
                    "approved" -> ChDecision.APPROVED
                    "rejected" -> ChDecision.REJECTED
                    else -> ChDecision.UNREVIEWED
                },
            )
        } catch (_: Exception) {
            ChMatchState.NOT_SEARCHED
        }
    }

    internal fun encode(state: ChMatchState): String = JSONObject()
        .put("schema", SCHEMA)
        .put("version", VERSION)
        .put("searched", state.searched)
        .put(
            "decision",
            when (state.decision) {
                ChDecision.APPROVED -> "approved"
                ChDecision.REJECTED -> "rejected"
                ChDecision.UNREVIEWED -> "unreviewed"
            },
        )
        .apply {
            state.candidate?.let { candidate ->
                put(
                    "candidate",
                    JSONObject()
                        .put("key", candidate.key)
                        .put("title", candidate.title)
                        .put("author", candidate.author)
                        .put("year", candidate.year)
                        .put("score", candidate.score)
                        .put(
                            "fields",
                            JSONObject().apply {
                                candidate.fields.forEach { (key, value) -> put(key, value) }
                            },
                        ),
                )
            }
        }
        .toString()

    fun write(dir: File, state: ChMatchState) {
        Entries.atomicWrite(File(dir, FILE_NAME), encode(state))
    }

    fun decide(dir: File, decision: ChDecision): ChMatchState {
        val next = read(dir).copy(decision = decision)
        write(dir, next)
        return next
    }
}
