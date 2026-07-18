package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ShallowCaptureQueueTest {

    @Test
    fun acceptsOneActiveAndOnePendingThenRejectsAThird() {
        val queue = ShallowCaptureQueue()

        val first = queue.accept(4) as ShallowCaptureQueue.Acceptance.Started
        val second = queue.accept(4) as ShallowCaptureQueue.Acceptance.Queued

        assertEquals(4, first.ticket.pageNumber)
        assertEquals(5, second.ticket.pageNumber)
        assertEquals(ShallowCaptureQueue.Acceptance.Rejected, queue.accept(4))
        assertTrue(queue.full)
    }

    @Test
    fun successfulActiveCapturePromotesPendingInPageOrder() {
        val queue = ShallowCaptureQueue()
        val first = queue.accept(1) as ShallowCaptureQueue.Acceptance.Started
        val second = queue.accept(1) as ShallowCaptureQueue.Acceptance.Queued

        val completion = queue.finishActive(success = true)

        assertEquals(first.ticket, completion.finished)
        assertEquals(second.ticket, completion.next)
        assertEquals(2, queue.active?.pageNumber)
        assertFalse(queue.full)
    }

    @Test
    fun failedActiveCaptureCompactsPendingIntoMissingPage() {
        val queue = ShallowCaptureQueue()
        val first = queue.accept(7) as ShallowCaptureQueue.Acceptance.Started
        val second = queue.accept(7) as ShallowCaptureQueue.Acceptance.Queued

        val completion = queue.finishActive(success = false)

        assertEquals(first.ticket.id, completion.finished.id)
        assertEquals(second.ticket.id, completion.next?.id)
        assertEquals(7, completion.next?.pageNumber)
    }

    @Test
    fun cancellingPendingLeavesOnlyTheActiveCapture() {
        val queue = ShallowCaptureQueue()
        queue.accept(1)
        val second = queue.accept(1) as ShallowCaptureQueue.Acceptance.Queued

        assertEquals(second.ticket, queue.cancelQueued())
        assertNull(queue.queued)
        assertTrue(queue.busy)
        assertNull(queue.finishActive(success = true).next)
        assertFalse(queue.busy)
    }
}
