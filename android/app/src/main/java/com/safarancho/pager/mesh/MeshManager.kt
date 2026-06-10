package com.safarancho.pager.mesh

import android.content.Context
import android.util.Log
import java.util.Collections

/**
 * Orchestrates all mesh transports for the device.
 *
 * Responsibilities:
 *  - hold the active [Transport]s (internet, Wi-Fi, BLE) and start/stop them;
 *  - on send, fan an envelope across every available transport so the message
 *    finds *a* path (the backend router dedups by msg_id, so multi-transport
 *    delivery is safe);
 *  - on receive, dedup locally, surface messages addressed to us, and relay
 *    everything else onward (decrementing TTL) so this device acts as a hop.
 *
 * This is what makes the app a mesh node rather than a plain client: it can
 * raise/participate in the mesh over whichever transports are available.
 */
class MeshManager(
    context: Context,
    private val baseUrl: String,
    private val onMessageForMe: (MeshEnvelope) -> Unit,
) {
    private val ctx = context.applicationContext

    // Local dedup so a relayed message isn't processed twice when it arrives
    // over multiple transports.
    private val seen = Collections.synchronizedSet(object : LinkedHashSet<String>() {
        override fun add(element: String): Boolean {
            if (size > 2048) iterator().also { it.next(); it.remove() }
            return super.add(element)
        }
    })

    private val listener = MeshListener { envelope, via -> onEnvelope(envelope, via) }

    private val transports = mutableListOf<Transport>()

    /** Our own SSID; set after registration so we know which messages are "ours". */
    @Volatile var localSsid: String = ""

    fun startInternetOnly() {
        addAndStart(InternetTransport(baseUrl))
    }

    /** Start every transport. BLE/Wi-Fi require their runtime permissions first. */
    fun startAll(enableBle: Boolean, enableWifi: Boolean) {
        addAndStart(InternetTransport(baseUrl))
        if (enableWifi) addAndStart(WifiTransport(ctx))
        if (enableBle) addAndStart(BleTransport(ctx, listener))
    }

    private fun addAndStart(t: Transport) {
        transports.add(t)
        try { t.start() } catch (e: Exception) { Log.w(TAG, "start ${t.name} failed: ${e.message}") }
    }

    fun stop() {
        transports.forEach { runCatching { it.stop() } }
        transports.clear()
    }

    /** Send a new message into the mesh across all available transports. */
    fun send(targetSs: String, text: String): MeshEnvelope {
        val env = MeshEnvelope.create(localSsid, targetSs, text)
        seen.add(env.msgId)
        fanOut(env, exceptTransport = null)
        return env
    }

    private fun onEnvelope(env: MeshEnvelope, via: String) {
        if (!seen.add(env.msgId)) return  // duplicate
        if (env.targetSs == localSsid) {
            onMessageForMe(env)
            return
        }
        // Not for us → relay onward (this device is a hop), avoiding the
        // transport it came from to reduce echo.
        if (env.ttl > 0) {
            env.ttl -= 1
            fanOut(env, exceptTransport = via)
        }
    }

    private fun fanOut(env: MeshEnvelope, exceptTransport: String?) {
        var delivered = false
        for (t in transports) {
            if (t.name == exceptTransport) continue
            if (t.isAvailable && t.send(env)) delivered = true
        }
        if (!delivered) Log.d(TAG, "no transport delivered ${env.msgId}; will retry on reconnect")
    }

    val transportStatus: Map<String, Boolean>
        get() = transports.associate { it.name to it.isAvailable }

    companion object {
        private const val TAG = "MeshManager"
    }
}
