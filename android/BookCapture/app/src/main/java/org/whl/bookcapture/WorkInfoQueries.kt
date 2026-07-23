package org.whl.bookcapture

import androidx.work.WorkInfo
import androidx.work.WorkQuery

/**
 * Observe only work that can still affect the UI. Terminal WorkInfo rows remain
 * in WorkManager's database for a while and should not be rematerialized on
 * every screen refresh.
 */
internal fun activeUniqueWorkQuery(vararg uniqueWorkNames: String): WorkQuery =
    WorkQuery.Builder
        .fromUniqueWorkNames(uniqueWorkNames.toList())
        .addStates(listOf(
            WorkInfo.State.ENQUEUED,
            WorkInfo.State.RUNNING,
            WorkInfo.State.BLOCKED,
        ))
        .build()
