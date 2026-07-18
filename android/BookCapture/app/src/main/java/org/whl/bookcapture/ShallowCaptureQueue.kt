package org.whl.bookcapture

/**
 * A deterministic one-active/one-pending capture queue.
 *
 * Page numbers are reserved as soon as input is accepted. If the active shot
 * fails, the pending shot is compacted into the failed page number before it
 * is submitted, so saved pages remain dense and manifest order stays valid.
 */
internal class ShallowCaptureQueue {

    data class Ticket internal constructor(
        val id: Long,
        val pageNumber: Int,
    )

    sealed interface Acceptance {
        data class Started(val ticket: Ticket) : Acceptance
        data class Queued(val ticket: Ticket) : Acceptance
        data object Rejected : Acceptance
    }

    data class Completion(
        val finished: Ticket,
        val next: Ticket?,
    )

    private var nextId = 1L

    var active: Ticket? = null
        private set
    var queued: Ticket? = null
        private set

    val busy: Boolean get() = active != null
    val full: Boolean get() = active != null && queued != null

    fun accept(nextSavedPage: Int): Acceptance {
        require(nextSavedPage > 0)
        val current = active
        if (current == null) {
            check(queued == null)
            val ticket = Ticket(nextId++, nextSavedPage)
            active = ticket
            return Acceptance.Started(ticket)
        }
        if (queued == null) {
            val ticket = Ticket(nextId++, maxOf(nextSavedPage, current.pageNumber + 1))
            queued = ticket
            return Acceptance.Queued(ticket)
        }
        return Acceptance.Rejected
    }

    fun finishActive(success: Boolean): Completion {
        val finished = checkNotNull(active) { "No active capture to finish" }
        val waiting = queued
        queued = null
        active = waiting?.let {
            if (success) it else it.copy(pageNumber = finished.pageNumber)
        }
        return Completion(finished, active)
    }

    fun cancelQueued(): Ticket? = queued.also { queued = null }

    fun cancelAll(): List<Ticket> = listOfNotNull(active, queued).also {
        active = null
        queued = null
    }
}
