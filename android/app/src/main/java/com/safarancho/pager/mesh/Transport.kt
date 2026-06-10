package com.safarancho.pager.mesh

/**
 * A bidirectional channel that can carry mesh envelopes to nearby/remote peers.
 *
 * Implementations: [InternetTransport] (HTTP/WS to a node), [WifiTransport]
 * (NSD-discovered LAN peers), [BleTransport] (offline BLE GATT relay). The
 * [MeshManager] owns the set of active transports and fans messages across all
 * of them; the router's dedup/TTL logic (backend side) prevents loops.
 */
interface Transport {
    /** Short stable name for logging/UI, e.g. "internet", "wifi", "ble". */
    val name: String

    /** Whether this transport currently has at least one reachable peer. */
    val isAvailable: Boolean

    fun start()
    fun stop()

    /**
     * Best-effort send of [envelope] to whatever peers this transport reaches.
     * Returns true if it was handed off to at least one peer.
     */
    fun send(envelope: MeshEnvelope): Boolean
}

/** Callback for envelopes arriving from any transport. */
fun interface MeshListener {
    fun onEnvelope(envelope: MeshEnvelope, viaTransport: String)
}
