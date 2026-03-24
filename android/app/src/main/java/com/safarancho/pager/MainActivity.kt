package com.safarancho.pager

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Toast
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
    
    private var contactsList: MutableList<Contact> = mutableListOf()
    private var unreadMap: MutableMap<String, Int> = mutableMapOf()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val ssId = prefs.getString("ss_id", null)
        if (ssId == null) {
            val intent = Intent(this, RegisterActivity::class.java)
            startActivity(intent)
            finish()
            return
        }

        binding.tvMyId.text = "SSID: $ssId"
        setupRecyclerView()
        refreshContacts()
        connectWebSocket(ssId)

        // LOGOUT BUTTON
        binding.tvMyId.setOnClickListener {
            prefs.edit().remove("ss_id").apply()
            Toast.makeText(this, "SSID CLEARED. RESTARTING.", Toast.LENGTH_SHORT).show()
            startActivity(Intent(this, RegisterActivity::class.java))
            finish()
        }
    }

    private fun setupRecyclerView() {
        binding.rvContacts.layoutManager = LinearLayoutManager(this)
        binding.rvContacts.adapter = ContactAdapter(contactsList, unreadMap) { contact ->
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
            override fun onFailure(call: Call, e: IOException) {}

            override fun onResponse(call: Call, response: Response) {
                if (!response.isSuccessful) return
                val body = response.body?.string() ?: return
                try {
                    val athletes = gson.fromJson(body, Array<Contact>::class.java).toList()
                    runOnUiThread {
                        contactsList.clear()
                        contactsList.addAll(athletes)
                        binding.rvContacts.adapter?.notifyDataSetChanged()
                    }
                } catch (e: Exception) {}
            }
        })
    }

    private fun connectWebSocket(ssId: String) {
        val wsUrl = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$ssId"
        val request = Request.Builder().url(wsUrl).build()

        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                runOnUiThread { pulseLamp() }
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                handler.postDelayed({ connectWebSocket(ssId) }, 5000)
            }
        })
    }

    private fun pulseLamp() {
        // Use tvMyId for pulsing
        binding.tvMyId.animate().alpha(0.3f).setDuration(500).withEndAction {
            binding.tvMyId.animate().alpha(1.0f).setDuration(500).start()
        }.start()
    }

    override fun onResume() {
        super.onResume()
        refreshContacts()
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close()
    }
}
