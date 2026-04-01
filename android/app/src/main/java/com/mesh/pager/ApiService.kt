package com.mesh.pager

import com.google.gson.Gson
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

// --- Data models ---
data class RegisterResponse(val ss_id: String, val display_name: String, val token: String)
data class Contact(val ss_id: String, val display_name: String, val public_key: String = "")
data class Message(val from_ss: String, val to_ss: String, val text: String, val created_at: String)

object ApiService {
    val gson = Gson()
    private val client = OkHttpClient()
    private val BASE = BuildConfig.BASE_URL
    private val JSON = "application/json; charset=utf-8".toMediaType()

    var authToken: String = ""
    var authSsId: String = ""

    private fun authHeader(): String = "Bearer $authSsId:$authToken"

    fun register(name: String, displayName: String, publicKey: String): RegisterResponse {
        val body = gson.toJson(mapOf(
            "name" to name,
            "display_name" to displayName,
            "label" to displayName,
            "public_key" to publicKey,
        ))
        val req = Request.Builder().url("$BASE/register")
            .post(body.toRequestBody(JSON)).build()
        client.newCall(req).execute().use { resp ->
            return gson.fromJson(resp.body!!.string(), RegisterResponse::class.java)
        }
    }

    fun getContacts(): List<Contact> {
        val req = Request.Builder().url("$BASE/contacts").build()
        client.newCall(req).execute().use { resp ->
            return gson.fromJson(resp.body!!.string(), Array<Contact>::class.java).toList()
        }
    }

    fun sendMessage(from: String, to: String, text: String) {
        val body = gson.toJson(mapOf(
            "from_ss" to from,
            "target" to to,
            "text" to text,
        ))
        val req = Request.Builder().url("$BASE/message")
            .addHeader("Authorization", authHeader())
            .post(body.toRequestBody(JSON)).build()
        client.newCall(req).execute().close()
    }

    fun getMessages(ssId: String): List<Message> {
        val req = Request.Builder().url("$BASE/messages/$ssId?limit=100").build()
        client.newCall(req).execute().use { resp ->
            return gson.fromJson(resp.body!!.string(), Array<Message>::class.java).toList()
        }
    }
}
