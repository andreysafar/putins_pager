package com.safarancho.pager

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.safarancho.pager.databinding.ActivityRegisterBinding
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException

class RegisterActivity : AppCompatActivity() {

    private lateinit var binding: ActivityRegisterBinding
    private val client = OkHttpClient()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityRegisterBinding.inflate(layoutInflater)
        setContentView(binding.root)

        binding.btnRegister.setOnClickListener {
            val name = binding.etName.text.toString().trim()
            val displayName = binding.etDisplayName.text.toString().trim()
            if (name.isEmpty()) {
                Toast.makeText(this, "ENTER CALLSIGN", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            doRegister(name, displayName.ifEmpty { name })
        }
    }

    private fun doRegister(name: String, displayName: String) {
        binding.btnRegister.isEnabled = false
        binding.btnRegister.text = "SYNCING..."

        val json = """{"name":"$name","display_name":"$displayName","label":"$displayName"}"""
        val body = json.toRequestBody("application/json; charset=utf-8".toMediaType())
        val request = Request.Builder()
            .url("${BuildConfig.BASE_URL}/register")
            .post(body)
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread {
                    binding.btnRegister.isEnabled = true
                    binding.btnRegister.text = "RETRY"
                    Toast.makeText(this@RegisterActivity, "NETWORK ERROR: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }

            override fun onResponse(call: Call, response: Response) {
                if (!response.isSuccessful) {
                    runOnUiThread {
                        binding.btnRegister.isEnabled = true
                        binding.btnRegister.text = "RETRY"
                        Toast.makeText(this@RegisterActivity, "SERVER ERROR: ${response.code}", Toast.LENGTH_SHORT).show()
                    }
                    return
                }
                val body = response.body?.string() ?: return
                try {
                    val gson = com.google.gson.Gson()
                    val data = gson.fromJson(body, RegisterResponse::class.java)
                    // Save SSID
                    getSharedPreferences("pager_prefs", Context.MODE_PRIVATE)
                        .edit().putString("ss_id", data.ss_id).apply()
                    // Launch main
                    runOnUiThread {
                        Toast.makeText(this@RegisterActivity, "SSID ACQUIRED: ${data.ss_id}", Toast.LENGTH_SHORT).show()
                        startActivity(Intent(this@RegisterActivity, MainActivity::class.java))
                        finish()
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        binding.btnRegister.isEnabled = true
                        binding.btnRegister.text = "RETRY"
                        Toast.makeText(this@RegisterActivity, "PARSE ERROR", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        })
    }
}
