package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Test

class CameraPreferencesTest {

    @Test
    fun cameraProfileDefaultsInvalidOrMissingValuesToFast() {
        assertEquals(Prefs.CAMERA_PROFILE_FAST, Prefs.validatedCameraProfile(null))
        assertEquals(Prefs.CAMERA_PROFILE_FAST, Prefs.validatedCameraProfile(""))
        assertEquals(Prefs.CAMERA_PROFILE_FAST, Prefs.validatedCameraProfile("unknown"))
    }

    @Test
    fun cameraProfileAcceptsDetail() {
        assertEquals(
            Prefs.CAMERA_PROFILE_DETAIL,
            Prefs.validatedCameraProfile(Prefs.CAMERA_PROFILE_DETAIL),
        )
    }
}
