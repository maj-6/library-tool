package org.whl.bookcapture

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

class RowSeparatorResourceTest {

    @Test
    fun stackedRowsOwnOnlyOneBottomDottedSeparator() {
        val document = DocumentBuilderFactory.newInstance().apply {
            isNamespaceAware = true
        }.newDocumentBuilder().parse(File("src/main/res/drawable/whl_row.xml"))
        val items = document.getElementsByTagName("item")
        val androidNs = "http://schemas.android.com/apk/res/android"
        var clippedStrokeLayers = 0
        for (index in 0 until items.length) {
            val item = items.item(index) as Element
            if (item.getAttributeNS(androidNs, "top") == "-2dp") {
                assertEquals("-2dp", item.getAttributeNS(androidNs, "left"))
                assertEquals("-2dp", item.getAttributeNS(androidNs, "right"))
                assertTrue(item.getAttributeNS(androidNs, "bottom").isEmpty())
                clippedStrokeLayers += 1
            }
        }
        assertEquals(2, clippedStrokeLayers)
    }
}
