package com.safarancho.pager.call

import com.safarancho.pager.BuildConfig
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

/**
 * Dedicated WebSocket for call signaling. Rides the same `/ws/{ss_id}` endpoint
 * and `type=call_signal` envelope the web client and backend use, so a phone
 * and a browser can call each other.
 */
class SignalingClient(
    private val mySsId: String,
    private val onSignal: (from: String, payload: JSONObject) -> Unit,
) {
    private val client = OkHttpClient()
    private var ws: WebSocket? = null

    fun connect() {
        val url = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$mySsId"
        val req = Request.Builder().url(url).build()
        ws = client.newWebSocket(req, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                try {
                    val o = JSONObject(text)
                    if (o.optString("type") == "call_signal") {
                        onSignal(o.optString("from_ss"), o.optJSONObject("payload") ?: JSONObject())
                    }
                } catch (_: Exception) {}
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {}
        })
    }

    fun send(target: String, payload: JSONObject) {
        val msg = JSONObject()
            .put("type", "call_signal")
            .put("target", target)
            .put("payload", payload)
        ws?.send(msg.toString())
    }

    fun close() {
        try { ws?.close(1000, "call ended") } catch (_: Exception) {}
        ws = null
    }
}
