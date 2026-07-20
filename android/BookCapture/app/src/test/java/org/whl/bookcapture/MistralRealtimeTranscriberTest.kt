package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Test

class MistralRealtimeTranscriberTest {
    @Test
    fun parsesDocumentedSessionDeltaDoneAndErrorEvents() {
        assertSame(
            MistralRealtimeEvent.SessionCreated,
            parseMistralRealtimeEvent("{\"type\":\"session.created\"}"),
        )
        assertEquals(
            MistralRealtimeEvent.TextDelta("Price twelve dollars"),
            parseMistralRealtimeEvent(
                "{\"type\":\"transcription.text.delta\",\"text\":\"Price twelve dollars\"}",
            ),
        )
        assertEquals(
            MistralRealtimeEvent.Done("Price: twelve dollars"),
            parseMistralRealtimeEvent(
                "{\"type\":\"transcription.done\",\"text\":\"Price: twelve dollars\"}",
            ),
        )
        assertEquals(
            MistralRealtimeEvent.Failure("bad audio"),
            parseMistralRealtimeEvent(
                "{\"type\":\"error\",\"error\":{\"message\":\"bad audio\"}}",
            ),
        )
    }

    @Test
    fun acceptsEarlyAliasesAndIgnoresUnknownEvents() {
        assertEquals(
            MistralRealtimeEvent.TextDelta("Pages 240"),
            parseMistralRealtimeEvent(
                "{\"type\":\"transcription.delta\",\"delta\":\"Pages 240\"}",
            ),
        )
        assertSame(
            MistralRealtimeEvent.Ignored,
            parseMistralRealtimeEvent("{\"type\":\"rate_limits.updated\"}"),
        )
    }

    @Test
    fun invalidJsonIsAnExplicitFailure() {
        assertEquals(
            MistralRealtimeEvent.Failure("Mistral returned invalid realtime data"),
            parseMistralRealtimeEvent("not-json"),
        )
    }
}
