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
    DONE("done"),
    CANCEL("cancel"),
    RESTART("restart"),
    UNDO("undo"),
    NOTES("notes"),
    END_NOTES("end_notes"),
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
        PolicyVoiceCommand.DONE,
        PolicyVoiceCommand.CANCEL,
        PolicyVoiceCommand.RESTART,
        PolicyVoiceCommand.UNDO,
        PolicyVoiceCommand.NOTES,
    )
    private val noteCommands = setOf(
        PolicyVoiceCommand.END_NOTES,
        PolicyVoiceCommand.RESTART,
        PolicyVoiceCommand.UNDO,
    )
    private val stablePartialCommands = setOf(
        PolicyVoiceCommand.START,
        PolicyVoiceCommand.PHOTO,
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
    CommandPhrase(PolicyVoiceCommand.RESTART, "restart"),
    CommandPhrase(PolicyVoiceCommand.CANCEL, "cancel"),
    CommandPhrase(PolicyVoiceCommand.START, "start"),
    CommandPhrase(PolicyVoiceCommand.PHOTO, "photo"),
    CommandPhrase(PolicyVoiceCommand.DONE, "done"),
    CommandPhrase(PolicyVoiceCommand.UNDO, "undo"),
    CommandPhrase(PolicyVoiceCommand.NOTES, "notes"),
)

private val TRAILING_COMMAND_DECORATION = Regex("^[\\s\\p{P}]*$")
private val COMMAND_SEPARATOR_SUFFIX = Regex("[\\s:;,=\\p{Pd}]+$")

private fun cleanTranscriptBeforeCommand(value: String): String = value
    .replace(COMMAND_SEPARATOR_SUFFIX, "")
    .trimEnd()
