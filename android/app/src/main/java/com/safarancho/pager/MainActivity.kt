package com.safarancho.pager

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.google.gson.Gson
import com.safarancho.pager.databinding.ActivityMainBinding
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
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
    private var mySsId: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        mySsId = prefs.getString("ss_id", null)

        if (mySsId != null) {
            showContactsView()
        } else {
            showRegisterView()
        }
    }

    private fun showRegisterView() {
        binding.layoutRegister.visibility = View.VISIBLE
        binding.layoutContacts.visibility = View.GONE

        binding.btnRegister.setOnClickListener {
            val name = binding.etName.text.toString().trim()
            if (name.isEmpty()) {
                Toast.makeText(this, "ENTER CALLSIGN", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            val displayName = binding.etDisplayName.text.toString().trim().ifEmpty { name }
            doRegister(name, displayName)
        }
    }

    private fun showContactsView() {
        binding.layoutRegister.visibility = View.GONE
        binding.layoutContacts.visibility = View.VISIBLE

        binding.tvMyId.text = "SSID: $mySsId"
        setupRecyclerView()
        refreshContacts()
        connectWebSocket(mySsId!!)

        // LOGOUT: long-press SSID
        binding.tvMyId.setOnLongClickListener {
            prefs.edit().remove("ss_id").apply()
            Toast.makeText(this, "SSID CLEARED. RESTARTING.", Toast.LENGTH_SHORT).show()
            recreate()
            true
        }

        // REFRESH button
        binding.btnRefresh.setOnClickListener {
            refreshContacts()
            Toast.makeText(this, "SCANNING...", Toast.LENGTH_SHORT).show()
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
                    binding.btnRegister.text = "⚡ ACQUIRE SSID"
                    Toast.makeText(this@MainActivity, "NETWORK ERROR: ${e.message}", Toast.LENGTH_SHORT).show()
                }
            }

            override fun onResponse(call: Call, response: Response) {
                if (!response.isSuccessful) {
                    runOnUiThread {
                        binding.btnRegister.isEnabled = true
                        binding.btnRegister.text = "⚡ ACQUIRE SSID"
                        Toast.makeText(this@MainActivity, "SERVER ERROR: ${response.code}", Toast.LENGTH_SHORT).show()
                    }
                    return
                }
                val respBody = response.body?.string() ?: return
                try {
                    val data = gson.fromJson(respBody, RegisterResponse::class.java)
                    prefs.edit().putString("ss_id", data.ss_id).apply()
                    mySsId = data.ss_id
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, "SSID: ${data.ss_id}", Toast.LENGTH_SHORT).show()
                        showContactsView()
                    }
                } catch (e: Exception) {
                    runOnUiThread {
                        binding.btnRegister.isEnabled = true
                        binding.btnRegister.text = "⚡ ACQUIRE SSID"
                        Toast.makeText(this@MainActivity, "PARSE ERROR", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        })
    }

    private fun setupRecyclerView() {
        binding.rvContacts.layoutManager = LinearLayoutManager(this)
        binding.rvContacts.adapter = ContactAdapter(contactsList, unreadMap) { contact ->
            val intent = Intent(this, ChatActivity::class.java)
            intent.putExtra("contact_ss_id", contact.ss_id)
            intent.putExtra("contact_name", contact.display_name)
            startActivity(intent)
        }
    }

    private fun refreshContacts() {
        val request = Request.Builder()
            .url("${BuildConfig.BASE_URL}/contacts")
            .build()

        client.newCall(request).enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                runOnUiThread {
                    Toast.makeText(this@MainActivity, "CONTACTS FETCH FAILED", Toast.LENGTH_SHORT).show()
                }
            }

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
                } catch (e: Exception) {
                    runOnUiThread {
                        Toast.makeText(this@MainActivity, "PARSE ERROR: ${e.message}", Toast.LENGTH_SHORT).show()
                    }
                }
            }
        })
    }

    private fun connectWebSocket(ssId: String) {
        val wsUrl = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$ssId"
        val request = Request.Builder().url(wsUrl).build()

        ws = client.newWebSocket(request, object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                runOnUiThread { pulseLamp() }
                // Parse for unread badge update
                try {
                    val msg = gson.fromJson(text, Message::class.java)
                    if (msg.from_ss != ssId) {
                        runOnUiThread {
                            unreadMap[msg.from_ss] = (unreadMap[msg.from_ss] ?: 0) + 1
                            binding.rvContacts.adapter?.notifyDataSetChanged()
                        }
                    }
                } catch (_: Exception) {}
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, reason)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                handler.postDelayed({ if (!isFinishing) connectWebSocket(ssId) }, 5000)
            }
        })
    }

    private fun pulseLamp() {
        binding.tvMyId.animate().alpha(0.3f).setDuration(500).withEndAction {
            binding.tvMyId.animate().alpha(1.0f).setDuration(500).start()
        }.start()
    }

    override fun onResume() {
        super.onResume()
        if (mySsId != null) refreshContacts()
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close(1000, "User closed app")
    }
}
