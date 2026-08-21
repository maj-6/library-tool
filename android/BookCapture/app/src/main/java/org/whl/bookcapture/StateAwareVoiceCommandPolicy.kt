package org.whl.bookcapture

internal enum class VoiceCommandState {
    IDLE,
    NOTE_ACTIVE,
}

internal enum class VoiceRecognitionStability {
    UNSTABLE_PARTIAL,
    STABLE_PARTIAL,
    FINAL,
}

internal enum class PolicyVoiceCommand(val wireValue: String) {
    START("start"),
    PHOTO("photo"),
    CHECK("check"),
    SCAN("scan"),
    DONE("done"),
    CANCEL("cancel"),
    RESTART("restart"),
    UNDO("undo"),
    NOTES("notes"),
    END_NOTES("end_notes"),

    // Role-declaring captures. Each shoots exactly like PHOTO and additionally
    // stamps the resulting asset with a manual role, which is the only way a
    // role other than the classifier's guess ever gets recorded: the automatic
    // spine test compares whole-frame dimensions against a 3:1 ratio that a 4:3
    // camera cannot produce, so it has never once fired.
    SPINE("spine"),
    COVER("cover"),
    TITLE("title"),
    ;

    /** The photo role this command declares, or null when it is not a capture. */
    val declaredRole: PhotoRole?
        get() = when (this) {
            SPINE -> PhotoRole.SPINE
            COVER -> PhotoRole.COVER
            TITLE -> PhotoRole.TITLE_PAGE
            else -> null
        }
}

/**
 * Exact source positions consumed by a decision. Value equality makes this a
 * useful debounce key within one recognizer generation: the stable-partial and
 * final callbacks for the same command normally produce the same value.
 */
internal data class VoiceCommandConsumption(
    val command: PolicyVoiceCommand,
    val commandStart: Int,
    val commandEndExclusive: Int,
    val consumedThroughExclusive: Int,
) {
    val commandRange: IntRange get() = commandStart until commandEndExclusive
}

internal data class VoiceCommandPolicyResult(
    val command: PolicyVoiceCommand,
    /** Note text before a trailing stream command, with command separators removed. */
    val transcriptBeforeCommand: String,
    val consumption: VoiceCommandConsumption,
)

/** Pure recognition policy; microphone lifetime and action dispatch stay with
 * the eventual VoiceController/MainActivity integration. */
internal object StateAwareVoiceCommandPolicy {
    private val idleCommands = setOf(
        PolicyVoiceCommand.START,
        PolicyVoiceCommand.PHOTO,
        PolicyVoiceCommand.CHECK,
        PolicyVoiceCommand.SCAN,
        PolicyVoiceCommand.DONE,
        PolicyVoiceCommand.CANCEL,
        PolicyVoiceCommand.RESTART,
        PolicyVoiceCommand.UNDO,
        PolicyVoiceCommand.NOTES,
        PolicyVoiceCommand.SPINE,
        PolicyVoiceCommand.COVER,
        PolicyVoiceCommand.TITLE,
    )
    private val noteCommands = setOf(
        PolicyVoiceCommand.END_NOTES,
        PolicyVoiceCommand.RESTART,
        PolicyVoiceCommand.UNDO,
    )
    // Shutter commands fire on a stable partial rather than waiting for the
    // final result: the user is already holding the book still, and the extra
    // recogniser latency is what makes hands-free capture feel broken.
    private val stablePartialCommands = setOf(
        PolicyVoiceCommand.START,
        PolicyVoiceCommand.PHOTO,
        PolicyVoiceCommand.SPINE,
        PolicyVoiceCommand.COVER,
        PolicyVoiceCommand.TITLE,
    )

    fun evaluate(
        transcript: String,
        state: VoiceCommandState,
        stability: VoiceRecognitionStability,
    ): VoiceCommandPolicyResult? {
        if (transcript.isBlank() || stability == VoiceRecognitionStability.UNSTABLE_PARTIAL) {
            return null
        }

        // Resolve overlapping phrases before state filtering. Otherwise the
        // `notes` suffix of `end notes` could incorrectly start a note while idle.
        val candidates = COMMAND_PHRASES.flatMap { phrase ->
            phrase.regex.findAll(transcript).map { match -> Candidate(phrase, match) }.toList()
        }.filterNot { candidate ->
            COMMAND_PHRASES.asSequence()
                .filter { it.command != candidate.phrase.command }
                .flatMap { phrase -> phrase.regex.findAll(transcript).map { Candidate(phrase, it) } }
                .any { other ->
                    other.match.range.last == candidate.match.range.last &&
                        other.match.range.first < candidate.match.range.first
                }
        }

        val allowedByState = if (state == VoiceCommandState.IDLE) idleCommands else noteCommands
        val eligible = candidates.asSequence()
            .filter { it.phrase.command in allowedByState }
            .filter { candidate ->
                stability == VoiceRecognitionStability.FINAL ||
                    candidate.phrase.command in stablePartialCommands
            }
            .filter { candidate ->
                state != VoiceCommandState.NOTE_ACTIVE ||
                    transcript.substring(candidate.match.range.last + 1)
                        .matches(TRAILING_COMMAND_DECORATION)
            }
            .toList()
        val selected = eligible.maxWithOrNull(
            compareBy<Candidate> { it.match.range.last }
                .thenBy { it.phrase.wordCount }
                .thenBy { it.match.value.length },
        ) ?: return null

        val start = selected.match.range.first
        val endExclusive = selected.match.range.last + 1
        val consumedThrough = if (state == VoiceCommandState.NOTE_ACTIVE) {
            transcript.length
        } else {
            endExclusive
        }
        return VoiceCommandPolicyResult(
            command = selected.phrase.command,
            transcriptBeforeCommand = cleanTranscriptBeforeCommand(
                transcript.substring(0, start),
            ),
            consumption = VoiceCommandConsumption(
                command = selected.phrase.command,
                commandStart = start,
                commandEndExclusive = endExclusive,
                consumedThroughExclusive = consumedThrough,
            ),
        )
    }
}

private data class CommandPhrase(
    val command: PolicyVoiceCommand,
    val spokenPhrase: String,
) {
    val wordCount: Int = spokenPhrase.split(' ').size
    val regex: Regex = Regex(
        spokenPhrase.split(' ').joinToString(
            prefix = "(?<![\\p{L}\\p{N}_])(?:",
            postfix = ")(?![\\p{L}\\p{N}_])",
            separator = "[\\s\\p{P}]+",
        ) { Regex.escape(it) },
        RegexOption.IGNORE_CASE,
    )
}

private data class Candidate(val phrase: CommandPhrase, val match: MatchResult)

/** Longest phrases come first for readability; overlap resolution also uses
 * their actual source spans rather than relying on declaration order. */
private val COMMAND_PHRASES = listOf(
    CommandPhrase(PolicyVoiceCommand.END_NOTES, "end notes"),
    // "title page" must be listed as its own phrase, not left to the bare
    // "title": both map to TITLE, and matching the longer span keeps the word
    // "page" from being left behind for another phrase to pick up.
    CommandPhrase(PolicyVoiceCommand.TITLE, "title page"),
    CommandPhrase(PolicyVoiceCommand.RESTART, "restart"),
    CommandPhrase(PolicyVoiceCommand.CANCEL, "cancel"),
    CommandPhrase(PolicyVoiceCommand.START, "start"),
    CommandPhrase(PolicyVoiceCommand.PHOTO, "photo"),
    CommandPhrase(PolicyVoiceCommand.CHECK, "check"),
    CommandPhrase(PolicyVoiceCommand.SCAN, "scan"),
    CommandPhrase(PolicyVoiceCommand.DONE, "done"),
    CommandPhrase(PolicyVoiceCommand.UNDO, "undo"),
    CommandPhrase(PolicyVoiceCommand.NOTES, "notes"),
    CommandPhrase(PolicyVoiceCommand.SPINE, "spine"),
    CommandPhrase(PolicyVoiceCommand.COVER, "cover"),
    CommandPhrase(PolicyVoiceCommand.TITLE, "title"),
)

private val TRAILING_COMMAND_DECORATION = Regex("^[\\s\\p{P}]*$")
private val COMMAND_SEPARATOR_SUFFIX = Regex("[\\s:;,=\\p{Pd}]+$")

private fun cleanTranscriptBeforeCommand(value: String): String = value
    .replace(COMMAND_SEPARATOR_SUFFIX, "")
    .trimEnd()
