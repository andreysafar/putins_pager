package com.safarancho.pager

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.gson.Gson
import com.safarancho.pager.databinding.ActivityMainBinding
import okhttp3.*
import java.io.IOException

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val client = OkHttpClient()
    private val gson = Gson()
    private val handler = Handler(Looper.getMainLooper())
    private var ws: WebSocket? = null
    private val prefs by lazy { getSharedPreferences("pager_prefs", Context.MODE_PRIVATE) }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val ssId = prefs.getString("ss_id", null)
        if (ssId == null) {
            startActivity(Intent(this, RegisterActivity::class.java))
            finish()
            return
        }

        binding.tvMyId.text = "MY SSID: $ssId"
        setupRecyclerView()
        refreshContacts()
        connectWebSocket(ssId)
    }

    private fun setupRecyclerView() {
        binding.rvContacts.layoutManager = LinearLayoutManager(this)
        binding.rvContacts.adapter = ContactAdapter(emptyList()) { contact ->
            val intent = Intent(this, ChatActivity::class.java)
            intent.putExtra("to_ss", contact.ss_id)
            intent.putExtra("to_name", contact.display_name)
            startActivity(intent)
        }
    }

    private fun refreshContacts() {
        val request = Request.Builder()
            .url("${BuildConfig.BASE_URL}/api/athletes")
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread { /* handle error silently */ }
            }

            override fun onResponse(call: Call, response: Response) {
                response.use { 
                    if (!response.isSuccessful) return
                    val body = response.body?.string() ?: return
                    try {
                        val athletes = gson.fromJson(body, Array<Athlete>::class.java).toList()
                        runOnUiThread {
                            (binding.rvContacts.adapter as ContactAdapter).updateContacts(athletes)
                        }
                    } catch (e: Exception) {
                       /* parse error */
                    }
                }
            }
        })
    }

    private fun connectWebSocket(ssId: String) {
        val request = Request.Builder()
            .url("${BuildConfig.BASE_URL.replace("http", "ws")}/ws/$ssId")
            .build()

        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                runOnUiThread { pulseLamp() }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                handler.postDelayed({ connectWebSocket(ssId) }, 5000)
            }
        })
    }

    private fun pulseLamp() {
        // Find lamp view by ID or simulate pulsing
        val tvHeader = binding.root.findViewById<TextView>(R.id.tvHeader) ?: return
        try {
            tvHeader.setTextColor(getColor(R.color.neon_orange))
            tvHeader.animate().alpha(0.6f).setDuration(500).withEndAction {
                tvHeader.animate().alpha(1f).setDuration(500).start()
            }.start()
        } catch (_: Exception) {}
    }

    override fun onResume() {
        super.onResume()
        if (prefs.getString("ss_id", null) != null) {
            refreshContacts()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close()
    }
}

data class Athlete(val ss_id: String, val display_name: String)
