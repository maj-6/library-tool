package org.whl.bookcapture

import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicInteger

/** Serializes destructive delivery, processing, reprocessing, and deletion
 * for one entry inside the app process. WorkManager runs in this process, and
 * process death cancels its HTTP/file work before a replacement worker starts. */
internal object EntryOperationLocks {
    private data class Holder(val mutex: Mutex = Mutex(), val users: AtomicInteger = AtomicInteger())
    private val locks = ConcurrentHashMap<String, Holder>()

    suspend fun <T> withLock(entryId: String, action: suspend () -> T): T {
        val holder = locks.compute(entryId) { _, current ->
            (current ?: Holder()).also { it.users.incrementAndGet() }
        }!!
        return try {
            holder.mutex.withLock { action() }
        } finally {
            locks.compute(entryId) { _, current ->
                if (current !== holder) current
                else if (holder.users.decrementAndGet() == 0) null else holder
            }
        }
    }
}
