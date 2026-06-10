package com.safarancho.pager.mesh

import com.google.gson.Gson
import com.google.gson.annotations.SerializedName
import java.security.SecureRandom

/**
 * Wire format for a mesh message. Mirrors the backend `MeshMessage` envelope so
 * the same message can travel over internet, Wi-Fi, and BLE transports and be
 * routed by any node.
 */
data class MeshEnvelope(
    @SerializedName("msg_id") val msgId: String,
    @SerializedName("from_ss") val fromSs: String,
    @SerializedName("target_ss") val targetSs: String,
    val text: String,
    var ttl: Int = DEFAULT_TTL,
    val path: MutableList<String> = mutableListOf(),
    val kind: String = "text",
    @SerializedName("created_at") val createdAt: Double = System.currentTimeMillis() / 1000.0,
) {
    fun toJson(): String = gson.toJson(this)

    companion object {
        const val DEFAULT_TTL = 8
        private val gson = Gson()
        private val rng = SecureRandom()

        /** 128-bit hex id, matching the backend's `new_message_id()`. */
        fun newId(): String {
            val bytes = ByteArray(16)
            rng.nextBytes(bytes)
            return bytes.joinToString("") { "%02x".format(it) }
        }

        fun create(fromSs: String, targetSs: String, text: String, kind: String = "text") =
            MeshEnvelope(newId(), fromSs, targetSs, text, kind = kind)

        fun fromJson(json: String): MeshEnvelope = gson.fromJson(json, MeshEnvelope::class.java)
    }
}
