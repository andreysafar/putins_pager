package com.safarancho.pager

import com.google.gson.Gson
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

// --- Data models ---
data class RegisterResponse(val ss_id: String, val display_name: String)
data class Contact(val ss_id: String, val display_name: String)
data class Message(val from_ss: String, val to_ss: String, val text: String, val created_at: String)

object ApiService {
    val gson = Gson()
    private val client = OkHttpClient()
    private val BASE = BuildConfig.BASE_URL
    private val JSON = "application/json; charset=utf-8".toMediaType()

    fun register(name: String, displayName: String): RegisterResponse {
        val body = """{"name":"$name","display_name":"$displayName"}"""
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
        val body = """{"from_ss":"$from","to_ss":"$to","text":"$text"}"""
        val req = Request.Builder().url("$BASE/message")
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
