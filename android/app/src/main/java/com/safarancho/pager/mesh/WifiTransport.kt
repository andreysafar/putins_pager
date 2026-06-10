package com.safarancho.pager.mesh

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * Wi-Fi (LAN) transport using NSD/mDNS.
 *
 * Works when devices share a Wi-Fi network (incl. a hotspot) but have no
 * internet: each node both **registers** an `_pagermesh._tcp` service and
 * **discovers** peers advertising it, then relays envelopes to their resolved
 * host:port via HTTP `/mesh/ingest`. This is the middle tier between full
 * internet and BLE.
 */
class WifiTransport(
    private val context: Context,
    private val localPort: Int = 9009,
) : Transport {

    override val name = "wifi"

    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val peers = ConcurrentHashMap<String, String>()  // serviceName -> "http://host:port"
    private val client = OkHttpClient.Builder().callTimeout(4, TimeUnit.SECONDS).build()
    private val json = "application/json; charset=utf-8".toMediaType()

    @Volatile private var running = false
    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var registrationListener: NsdManager.RegistrationListener? = null

    override val isAvailable: Boolean get() = running && peers.isNotEmpty()

    override fun start() {
        running = true
        registerService()
        startDiscovery()
    }

    override fun stop() {
        running = false
        try { discoveryListener?.let { nsd.stopServiceDiscovery(it) } } catch (_: Exception) {}
        try { registrationListener?.let { nsd.unregisterService(it) } } catch (_: Exception) {}
        peers.clear()
    }

    override fun send(envelope: MeshEnvelope): Boolean {
        var ok = false
        for (url in peers.values) {
            try {
                val req = Request.Builder().url("$url/mesh/ingest")
                    .post(envelope.toJson().toRequestBody(json)).build()
                client.newCall(req).execute().use { if (it.isSuccessful) ok = true }
            } catch (e: Exception) {
                Log.w(TAG, "wifi send to $url failed: ${e.message}")
            }
        }
        return ok
    }

    private fun registerService() {
        val info = NsdServiceInfo().apply {
            serviceName = "pager-${MeshEnvelope.newId().take(6)}"
            serviceType = SERVICE_TYPE
            port = localPort
        }
        registrationListener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(s: NsdServiceInfo) { Log.i(TAG, "registered ${s.serviceName}") }
            override fun onRegistrationFailed(s: NsdServiceInfo, err: Int) { Log.w(TAG, "register failed $err") }
            override fun onServiceUnregistered(s: NsdServiceInfo) {}
            override fun onUnregistrationFailed(s: NsdServiceInfo, err: Int) {}
        }
        try {
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, registrationListener)
        } catch (e: Exception) {
            Log.w(TAG, "registerService failed: ${e.message}")
        }
    }

    private fun startDiscovery() {
        discoveryListener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(t: String) {}
            override fun onDiscoveryStopped(t: String) {}
            override fun onStartDiscoveryFailed(t: String, err: Int) { Log.w(TAG, "discovery start failed $err") }
            override fun onStopDiscoveryFailed(t: String, err: Int) {}

            override fun onServiceFound(info: NsdServiceInfo) {
                if (info.serviceType.contains("pagermesh")) resolve(info)
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                peers.remove(info.serviceName)
            }
        }
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, discoveryListener)
        } catch (e: Exception) {
            Log.w(TAG, "discoverServices failed: ${e.message}")
        }
    }

    private fun resolve(info: NsdServiceInfo) {
        nsd.resolveService(info, object : NsdManager.ResolveListener {
            override fun onResolveFailed(s: NsdServiceInfo, err: Int) { Log.w(TAG, "resolve failed $err") }
            override fun onServiceResolved(s: NsdServiceInfo) {
                val host = s.host?.hostAddress ?: return
                peers[s.serviceName] = "http://$host:${s.port}"
            }
        })
    }

    companion object {
        private const val TAG = "WifiTransport"
        private const val SERVICE_TYPE = "_pagermesh._tcp."
    }
}
