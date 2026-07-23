package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Test

class CopyrightStatusDrawableTest {
    @Test
    fun `desktop copyright wording maps to the matching semantic tone`() {
        assertEquals(
            CopyrightStatusTone.PUBLIC_DOMAIN,
            copyrightStatusTone("Public domain (published before 1929)"),
        )
        assertEquals(
            CopyrightStatusTone.INCONCLUSIVE,
            copyrightStatusTone("Public domain (no renewal found)"),
        )
        assertEquals(
            CopyrightStatusTone.PUBLIC_DOMAIN,
            resolvedCopyrightStatusTone(
                "Public domain (no renewal found)",
                hasRegistrationEvidence = true,
            ),
        )
        assertEquals(
            CopyrightStatusTone.IN_COPYRIGHT,
            copyrightStatusTone("In copyright (renewal R1234)"),
        )
        assertEquals(CopyrightStatusTone.PUBLIC_DOMAIN, copyrightStatusTone("Cleared"))
        assertEquals(CopyrightStatusTone.INCONCLUSIVE, copyrightStatusTone("Search only"))
        assertEquals(CopyrightStatusTone.IN_COPYRIGHT, copyrightStatusTone("Restricted"))
        assertEquals(CopyrightStatusTone.UNKNOWN, copyrightStatusTone(""))
    }
}
