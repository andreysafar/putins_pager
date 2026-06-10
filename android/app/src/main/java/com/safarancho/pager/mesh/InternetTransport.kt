package com.safarancho.pager.mesh

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Internet transport: posts envelopes to a node's `/mesh/ingest` endpoint over
 * HTTP. This is the "online" path — used whenever the device has connectivity
 * to a reachable pager node.
 */
class InternetTransport(private val baseUrl: String) : Transport {
    override val name = "internet"

    @Volatile
    private var reachable = false

    override val isAvailable: Boolean get() = reachable

    private val client = OkHttpClient.Builder()
        .callTimeout(5, TimeUnit.SECONDS)
        .build()

    private val json = "application/json; charset=utf-8".toMediaType()

    override fun start() {
        // A lightweight health probe could run here on a background thread to
        // toggle `reachable`. We optimistically assume reachable; failed sends
        // flip it off.
        reachable = true
    }

    override fun stop() {
        reachable = false
    }

    override fun send(envelope: MeshEnvelope): Boolean {
        return try {
            val req = Request.Builder()
                .url("$baseUrl/mesh/ingest")
                .post(envelope.toJson().toRequestBody(json))
                .build()
            client.newCall(req).execute().use { resp ->
                reachable = resp.isSuccessful
                resp.isSuccessful
            }
        } catch (e: Exception) {
            Log.w(TAG, "internet send failed: ${e.message}")
            reachable = false
            false
        }
    }

    companion object {
        private const val TAG = "InternetTransport"
    }
}
