package org.whl.bookcapture

import org.json.JSONArray
import org.json.JSONObject
import org.json.JSONTokener

internal data class BookDiagnosticsContent(
    val bookJson: String?,
    val mistralSections: List<MistralDiagnosticSection>,
)

internal data class MistralDiagnosticSection(
    val kind: Entries.MistralResponseKind,
    val captureOrder: Int?,
    val humanReadableBody: String,
    val validJson: Boolean,
)

/**
 * Builds the two diagnostic documents exclusively from persisted provider
 * artifacts. In particular, legacy OCR Markdown is not presented as a raw
 * Mistral response: older captures for which the response was discarded get
 * an honest empty state in the UI.
 */
internal object BookDiagnosticsPresenter {
    fun from(entry: Entries.Entry): BookDiagnosticsContent = from(
        bookJson = entry.bookJsonText(),
        mistralResponses = entry.mistralResponses(),
    )

    internal fun from(
        bookJson: String?,
        mistralResponses: List<Entries.MistralResponse>,
    ): BookDiagnosticsContent = BookDiagnosticsContent(
        bookJson = bookJson?.let(::prettyPrintJsonObject),
        mistralSections = mistralResponses.map { response ->
            val parsed = parseJson(response.rawJson)
            MistralDiagnosticSection(
                kind = response.kind,
                captureOrder = response.captureOrder,
                humanReadableBody = parsed?.let(JsonHumanReader::render) ?: response.rawJson,
                validJson = parsed != null,
            )
        },
    )

    private fun prettyPrintJsonObject(raw: String): String = try {
        JSONObject(raw).toString(2)
    } catch (_: Exception) {
        // Keep the exact persisted bytes visible if storage is damaged. The UI
        // does not claim invalid content is a valid JSON object.
        raw
    }

    private fun parseJson(raw: String): Any? = try {
        JSONTokener(raw).nextValue().takeIf { it is JSONObject || it is JSONArray }
    } catch (_: Exception) {
        null
    }
}

/** Generic, lossless-to-values rendering for provider JSON we do not own. */
private object JsonHumanReader {
    fun render(value: Any): String = buildString { appendValue(value, 0, null) }.trimEnd()

    private fun StringBuilder.appendValue(value: Any?, depth: Int, label: String?) {
        val prefix = "  ".repeat(depth)
        when (value) {
            is JSONObject -> {
                if (label != null) append(prefix).append(humanLabel(label)).append('\n')
                val keys = value.keys().asSequence().toList()
                if (keys.isEmpty()) append(prefix).append("(empty object)").append('\n')
                keys.forEach { key -> appendValue(value.opt(key), depth + (label != null).toInt(), key) }
            }
            is JSONArray -> {
                if (label != null) {
                    append(prefix).append(humanLabel(label))
                        .append(" (").append(value.length()).append(")").append('\n')
                }
                val itemDepth = depth + (label != null).toInt()
                if (value.length() == 0) append("  ".repeat(itemDepth)).append("(empty list)").append('\n')
                for (index in 0 until value.length()) {
                    val item = value.opt(index)
                    val itemPrefix = "  ".repeat(itemDepth)
                    if (item is JSONObject || item is JSONArray) {
                        append(itemPrefix).append("Item ").append(index + 1).append('\n')
                        appendValue(item, itemDepth + 1, null)
                    } else {
                        append(itemPrefix).append(index + 1).append(": ")
                        appendScalar(item, itemDepth + 1).append('\n')
                    }
                }
            }
            else -> {
                if (label != null) append(prefix).append(humanLabel(label)).append(": ")
                appendScalar(value, depth).append('\n')
            }
        }
    }

    private fun StringBuilder.appendScalar(value: Any?, depth: Int): StringBuilder {
        if (value == null || value == JSONObject.NULL) return append("null")
        if (value !is String) return append(value)

        val embedded = value.trim().takeIf { it.startsWith("{") || it.startsWith("[") }
            ?.let { raw ->
                try {
                    JSONTokener(raw).nextValue().takeIf { it is JSONObject || it is JSONArray }
                } catch (_: Exception) {
                    null
                }
            }
        if (embedded != null) {
            append('\n')
            appendValue(embedded, depth + 1, null)
            return this
        }
        if ('\n' !in value) return append(value)
        append('\n')
        val indent = "  ".repeat(depth + 1)
        value.lineSequence().forEach { line -> append(indent).append(line).append('\n') }
        if (isNotEmpty() && this[length - 1] == '\n') deleteCharAt(length - 1)
        return this
    }

    private fun humanLabel(key: String): String = key
        .replace(Regex("([a-z0-9])([A-Z])"), "\$1 \$2")
        .replace('_', ' ')
        .replace('-', ' ')
        .trim()
        .replaceFirstChar { if (it.isLowerCase()) it.titlecase() else it.toString() }

    private fun Boolean.toInt(): Int = if (this) 1 else 0
}

internal enum class JsonSyntaxKind {
    KEY,
    STRING,
    NUMBER,
    BOOLEAN,
    NULL,
    PUNCTUATION,
}

internal data class JsonSyntaxToken(
    val start: Int,
    val end: Int,
    val kind: JsonSyntaxKind,
)

/** Small dependency-free lexer; Android spans are applied by the Activity. */
internal object JsonSyntaxTokenizer {
    fun tokenize(text: String): List<JsonSyntaxToken> {
        val tokens = mutableListOf<JsonSyntaxToken>()
        var index = 0
        while (index < text.length) {
            when (val char = text[index]) {
                '"' -> {
                    val start = index++
                    var escaped = false
                    while (index < text.length) {
                        val current = text[index++]
                        if (current == '"' && !escaped) break
                        escaped = current == '\\' && !escaped
                        if (current != '\\') escaped = false
                    }
                    var lookahead = index
                    while (lookahead < text.length && text[lookahead].isWhitespace()) lookahead++
                    tokens += JsonSyntaxToken(
                        start,
                        index,
                        if (lookahead < text.length && text[lookahead] == ':') {
                            JsonSyntaxKind.KEY
                        } else {
                            JsonSyntaxKind.STRING
                        },
                    )
                }
                '-', in '0'..'9' -> {
                    val start = index++
                    while (index < text.length && text[index] in "0123456789+-.eE") index++
                    tokens += JsonSyntaxToken(start, index, JsonSyntaxKind.NUMBER)
                }
                '{', '}', '[', ']', ':', ',' -> {
                    tokens += JsonSyntaxToken(index, index + 1, JsonSyntaxKind.PUNCTUATION)
                    index++
                }
                else -> when {
                    text.startsWith("true", index) || text.startsWith("false", index) -> {
                        val end = index + if (char == 't') 4 else 5
                        tokens += JsonSyntaxToken(index, end, JsonSyntaxKind.BOOLEAN)
                        index = end
                    }
                    text.startsWith("null", index) -> {
                        tokens += JsonSyntaxToken(index, index + 4, JsonSyntaxKind.NULL)
                        index += 4
                    }
                    else -> index++
                }
            }
        }
        return tokens
    }
}
