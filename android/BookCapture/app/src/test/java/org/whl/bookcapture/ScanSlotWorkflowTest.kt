package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ScanSlotWorkflowTest {
    private val scanA = BookCollection(
        "11111111-1111-4111-8111-111111111111",
        "Digitize A",
        "Scan room",
        collectionType = CollectionType.SCAN,
    )
    private val scanB = BookCollection(
        "22222222-2222-4222-8222-222222222222",
        "Digitize B",
        "Scan room",
        collectionType = CollectionType.SCAN,
    )
    private val scanC = BookCollection(
        "33333333-3333-4333-8333-333333333333",
        "Digitize C",
        "Scan room",
        collectionType = CollectionType.SCAN,
    )

    @Test
    fun threeSelectionsResolveIndependentlyWithoutFallbackDuplicates() {
        val resolved = resolveScanCollectionSlots(
            listOf(scanA, scanB, scanC),
            mapOf(
                ScanCollectionSlot.A to scanA.id,
                ScanCollectionSlot.B to scanB.id,
                ScanCollectionSlot.C to scanC.id,
            ),
        )
        assertEquals(scanA, resolved[ScanCollectionSlot.A])
        assertEquals(scanB, resolved[ScanCollectionSlot.B])
        assertEquals(scanC, resolved[ScanCollectionSlot.C])

        val only = resolveScanCollectionSlots(listOf(scanA), emptyMap())
        assertEquals(scanA, only[ScanCollectionSlot.A])
        assertNull(only[ScanCollectionSlot.B])
        assertNull(only[ScanCollectionSlot.C])
    }

    @Test
    fun assigningACollectionToANewSlotRemovesItsOldSlot() {
        val assigned = assignScanCollectionSlot(
            mapOf(
                ScanCollectionSlot.A to scanA.id,
                ScanCollectionSlot.B to scanB.id,
                ScanCollectionSlot.C to scanC.id,
            ),
            ScanCollectionSlot.C,
            scanA.id,
        )
        assertNull(assigned[ScanCollectionSlot.A])
        assertEquals(scanB.id, assigned[ScanCollectionSlot.B])
        assertEquals(scanA.id, assigned[ScanCollectionSlot.C])
    }

    @Test
    fun abcAreFinalOnlySaveAndScanCommands() {
        ScanCollectionSlot.entries.forEach { slot ->
            assertEquals(slot, scanSlotForCaptureCommand(slot.wireValue))
            assertTrue(isCaptureSaveTerminalCommand(slot.wireValue))
            assertNull(VoiceController.commandFromPartial(slot.wireValue))
            assertEquals(slot.wireValue, VoiceController.commandFromFinal(slot.wireValue))
        }
        assertTrue(isCaptureSaveTerminalCommand("done"))
        assertFalse(isCaptureSaveTerminalCommand("scan"))
        assertFalse(isCaptureSaveTerminalCommand("cover"))
        assertNull(VoiceController.commandFromFinal("please a"))
        assertEquals("photo", VoiceController.commandFromFinal("photo a"))
    }
}
