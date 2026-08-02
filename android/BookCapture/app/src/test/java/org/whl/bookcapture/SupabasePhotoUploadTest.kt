package org.whl.bookcapture

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Protocol
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File
import java.io.IOException
import java.io.InterruptedIOException
import java.net.InetAddress
import java.net.Proxy
import java.net.ServerSocket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

class SupabasePhotoUploadTest {

    @Test
    fun photoUploadClientBoundsEveryNetworkPhase() {
        val client = newSupabasePhotoUploadClient()

        assertEquals(SUPABASE_PHOTO_CONNECT_TIMEOUT_MS.toInt(), client.connectTimeoutMillis)
        assertEquals(SUPABASE_PHOTO_READ_TIMEOUT_MS.toInt(), client.readTimeoutMillis)
        assertEquals(SUPABASE_PHOTO_WRITE_TIMEOUT_MS.toInt(), client.writeTimeoutMillis)
        assertEquals(SUPABASE_PHOTO_CALL_TIMEOUT_MS.toInt(), client.callTimeoutMillis)
    }

    @Test
    fun photoUploadRequestPreservesSupabaseHeadersAndFixedFileLength() {
        withTempPhoto(byteArrayOf(1, 2, 3, 4)) { photo ->
            val request = newSupabasePhotoUploadRequest(
                url = "https://project.supabase.co/storage/v1/object/captures/device/id/photo_1.jpg",
                anonKey = "anon-key",
                accessToken = "access-token",
                file = photo,
            )

            assertEquals("POST", request.method)
            assertEquals("anon-key", request.header("apikey"))
            assertEquals("Bearer access-token", request.header("Authorization"))
            assertEquals("image/jpeg", request.header("Content-Type"))
            assertEquals("true", request.header("x-upsert"))
            assertEquals(photo.length(), request.body?.contentLength())
            assertEquals("image/jpeg", request.body?.contentType().toString())
        }
    }

    @Test
    fun nonSuccessfulUploadRetainsHttpExceptionAndOwnerCheck() {
        val ownerChecks = AtomicInteger()
        val client = newSupabasePhotoUploadClient().newBuilder()
            .addInterceptor { chain ->
                Response.Builder()
                    .request(chain.request())
                    .protocol(Protocol.HTTP_1_1)
                    .code(409)
                    .message("Conflict")
                    .body(
                        "{\"message\":\"object exists\"}"
                            .toResponseBody("application/json".toMediaType()),
                    )
                    .build()
            }
            .build()

        withTempPhoto(byteArrayOf(5, 6, 7)) { photo ->
            val error = expectThrows<SupabaseClient.HttpException> {
                executeSupabasePhotoUpload(
                    client = client,
                    request = newSupabasePhotoUploadRequest(
                        url = "https://project.supabase.co/storage/v1/object/captures/photo.jpg",
                        anonKey = "anon-key",
                        accessToken = "access-token",
                        file = photo,
                    ),
                ) {
                    ownerChecks.incrementAndGet()
                }
            }

            assertEquals(1, ownerChecks.get())
            assertEquals(409, error.code)
            assertEquals("{\"message\":\"object exists\"}", error.responseBody)
            assertTrue(error.message.orEmpty().contains("HTTP 409"))
        }
    }

    @Test
    fun accountChangeStillTakesPrecedenceOverServerError() {
        val client = newSupabasePhotoUploadClient().newBuilder()
            .addInterceptor { chain ->
                Response.Builder()
                    .request(chain.request())
                    .protocol(Protocol.HTTP_1_1)
                    .code(403)
                    .message("Forbidden")
                    .body("forbidden".toResponseBody())
                    .build()
            }
            .build()

        withTempPhoto(byteArrayOf(8)) { photo ->
            expectThrows<SupabaseClient.AccountChanged> {
                executeSupabasePhotoUpload(
                    client = client,
                    request = newSupabasePhotoUploadRequest(
                        url = "https://project.supabase.co/storage/v1/object/captures/photo.jpg",
                        anonKey = "anon-key",
                        accessToken = "access-token",
                        file = photo,
                    ),
                ) {
                    throw SupabaseClient.AccountChanged()
                }
            }
        }
    }

    @Test
    fun wholeCallTimeoutCancelsAStalledPeer() {
        val releasePeer = CountDownLatch(1)
        val accepted = CountDownLatch(1)
        val loopback = InetAddress.getByName("127.0.0.1")
        ServerSocket(0, 1, loopback).use { server ->
            val serverThread = thread(isDaemon = true, name = "stalled-supabase-peer") {
                try {
                    server.accept().use {
                        accepted.countDown()
                        releasePeer.await(5, TimeUnit.SECONDS)
                    }
                } catch (_: IOException) {
                    // The test closes the listener and accepted socket during cleanup.
                }
            }
            try {
                val client = newSupabasePhotoUploadClient(
                    connectTimeoutMs = 1_000,
                    readTimeoutMs = 5_000,
                    writeTimeoutMs = 5_000,
                    callTimeoutMs = 250,
                ).newBuilder()
                    .proxy(Proxy.NO_PROXY)
                    .build()
                withTempPhoto(byteArrayOf(9, 10, 11)) { photo ->
                    val startedAt = System.nanoTime()
                    val error = expectThrows<IOException> {
                        executeSupabasePhotoUpload(
                            client = client,
                            request = newSupabasePhotoUploadRequest(
                                url = "http://127.0.0.1:${server.localPort}/photo.jpg",
                                anonKey = "anon-key",
                                accessToken = "access-token",
                                file = photo,
                            ),
                            ensureOwnerStillCurrent = {},
                        )
                    }
                    val elapsedMs = TimeUnit.NANOSECONDS.toMillis(
                        System.nanoTime() - startedAt,
                    )

                    assertTrue("server never accepted the request", accepted.await(1, TimeUnit.SECONDS))
                    assertTrue("expected timeout cancellation, got $error", error is InterruptedIOException)
                    assertTrue("stalled upload took ${elapsedMs}ms", elapsedMs < 3_000)
                }
            } finally {
                releasePeer.countDown()
                server.close()
                serverThread.join(1_000)
            }
        }
    }

    private inline fun withTempPhoto(bytes: ByteArray, block: (File) -> Unit) {
        val file = File.createTempFile("supabase-photo-upload", ".jpg")
        try {
            file.writeBytes(bytes)
            block(file)
        } finally {
            file.delete()
        }
    }

    private inline fun <reified T : Throwable> expectThrows(block: () -> Unit): T {
        try {
            block()
        } catch (error: Throwable) {
            if (error is T) return error
            throw error
        }
        fail("Expected ${T::class.java.simpleName}")
        throw AssertionError("unreachable")
    }
}
