package com.safarancho.pager

import com.google.gson.Gson
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File

// --- Data models ---
data class RegisterResponse(val ss_id: String, val display_name: String)
data class Contact(val ss_id: String, val display_name: String)
data class Message(val from_ss: String, val to_ss: String, val text: String, val created_at: String)
data class UploadResponse(val url: String)

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

    fun uploadFile(file: File): UploadResponse {
        val mimeType = getMimeType(file.extension)
        val requestBody = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("file", file.name, file.asRequestBody(mimeType.toMediaTypeOrNull()))
            .build()
        val req = Request.Builder().url("$BASE/upload")
            .post(requestBody).build()
        client.newCall(req).execute().use { resp ->
            if (!resp.isSuccessful) {
                throw Exception("Upload failed: ${resp.code}")
            }
            val body = resp.body?.string()
            if (body.isNullOrEmpty()) {
                throw Exception("Empty response from server")
            }
            return gson.fromJson(body, UploadResponse::class.java)
        }
    }

    private fun getMimeType(ext: String): String {
        return when (ext.lowercase()) {
            "jpg", "jpeg" -> "image/jpeg"
            "png" -> "image/png"
            "gif" -> "image/gif"
            "webp" -> "image/webp"
            "mp4" -> "video/mp4"
            "pdf" -> "application/pdf"
            else -> "application/octet-stream"
        }
    }
}
