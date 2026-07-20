package org.whl.bookcapture

/** Catalog fields that may be spoken into a structured capture note. */
internal enum class StructuredNoteField(val label: String) {
    PRICE("Price"),
    PAGES("Pages"),
    CONDITION("Condition"),
    ILLUSTRATIONS("Illustrations"),
    REMARK("Remark"),
}

internal enum class StructuredNoteStatus {
    IN_PROGRESS,
    COMPLETED,
}

/** One label/value segment in transcript order. Empty values remain visible
 * while speech recognition is still filling in the field. */
internal data class StructuredNoteRow(
    val field: StructuredNoteField,
    val value: String,
)

/**
 * Pure domain representation of an evolving voice note. Each transcript update
 * is parsed from the beginning, allowing a recognizer's revised hypothesis to
 * retroactively turn free speech into structured rows without losing the text
 * before the first recognized label.
 */
internal data class StructuredNote private constructor(
    val transcript: String,
    val unclassifiedText: String,
    val rows: List<StructuredNoteRow>,
    val status: StructuredNoteStatus,
) {
    val isCompleted: Boolean get() = status == StructuredNoteStatus.COMPLETED
    val hasStructuredRows: Boolean get() = rows.isNotEmpty()

    /** Completed notes are immutable; late recognizer callbacks cannot rewrite
     * a note after its owner has accepted the final transcript. */
    fun updateTranscript(value: String): StructuredNote =
        if (isCompleted) this else parse(value, StructuredNoteStatus.IN_PROGRESS)

    /** A speech engine may supply a refined final hypothesis when it completes. */
    fun complete(finalTranscript: String = transcript): StructuredNote =
        if (isCompleted) this else parse(finalTranscript, StructuredNoteStatus.COMPLETED)

    companion object {
        fun inProgress(transcript: String = ""): StructuredNote =
            parse(transcript, StructuredNoteStatus.IN_PROGRESS)

        fun completed(transcript: String): StructuredNote =
            parse(transcript, StructuredNoteStatus.COMPLETED)

        private fun parse(value: String, status: StructuredNoteStatus): StructuredNote {
            val transcript = value.trim()
            val matches = STRUCTURED_NOTE_LABEL.findAll(transcript).toList()
            if (matches.isEmpty()) {
                return StructuredNote(
                    transcript = transcript,
                    unclassifiedText = transcript,
                    rows = emptyList(),
                    status = status,
                )
            }

            val rows = matches.mapIndexed { index, match ->
                val valueStart = match.range.last + 1
                val valueEnd = matches.getOrNull(index + 1)?.range?.first ?: transcript.length
                StructuredNoteRow(
                    field = checkNotNull(FIELD_BY_LABEL[match.value.lowercase()]),
                    value = cleanStructuredNoteValue(transcript.substring(valueStart, valueEnd)),
                )
            }
            return StructuredNote(
                transcript = transcript,
                unclassifiedText = transcript.substring(0, matches.first().range.first).trim(),
                rows = rows,
                status = status,
            )
        }
    }
}

private val FIELD_BY_LABEL = StructuredNoteField.entries.associateBy { it.label.lowercase() }

/** Letter/digit lookarounds prevent labels from firing inside words such as
 * "priceless", "webpages", "conditioner", and "remarkable". Punctuation and
 * whitespace remain valid spoken-label boundaries. */
private val STRUCTURED_NOTE_LABEL = Regex(
    StructuredNoteField.entries.joinToString(
        prefix = "(?<![\\p{L}\\p{N}_])(?:",
        postfix = ")(?![\\p{L}\\p{N}_])",
        separator = "|",
    ) { Regex.escape(it.label) },
    RegexOption.IGNORE_CASE,
)

private val LEADING_FIELD_DELIMITERS = Regex("^[\\s:;,=.\\p{Pd}]+")
private val TRAILING_FIELD_DELIMITERS = Regex("[\\s:;,.=\\p{Pd}]+$")

private fun cleanStructuredNoteValue(value: String): String = value
    .replace(LEADING_FIELD_DELIMITERS, "")
    .replace(TRAILING_FIELD_DELIMITERS, "")
    .trim()
