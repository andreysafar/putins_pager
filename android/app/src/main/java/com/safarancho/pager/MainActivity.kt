package com.safarancho.pager

import android.content.Context
import android.content.Intent
import android.graphics.Canvas
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.Editable
import android.text.TextWatcher
import android.view.View
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.ItemTouchHelper
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
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
    private var filteredList: MutableList<Contact> = mutableListOf()
    private var unreadMap: MutableMap<String, Int> = mutableMapOf()
    private var mySsId: String? = null
    private var hiddenContacts: MutableSet<String> = mutableSetOf()
    private var showAll: Boolean = false
    private var searchQuery: String = ""
    private val contactsWithMessages: MutableSet<String> = mutableSetOf()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        // Create notification channel for Android 8+
        NotificationHelper.createChannel(this)

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
        setupSearch()
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

        // LOGOUT / SWITCH SSID button
        binding.btnLogout.setOnClickListener {
            android.app.AlertDialog.Builder(this)
                .setTitle("SWITCH SSID")
                .setMessage("Clear current identity and register new one?")
                .setPositiveButton("YES") { _, _ ->
                    prefs.edit().remove("ss_id").apply()
                    showRegisterView()
                    Toast.makeText(this, "SSID cleared", Toast.LENGTH_SHORT).show()
                }
                .setNegativeButton("NO", null)
                .show()
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
        binding.rvContacts.adapter = ContactAdapter(filteredList, unreadMap) { contact ->
            contactsWithMessages.add(contact.ss_id)
            val intent = Intent(this, ChatActivity::class.java)
            intent.putExtra("contact_ss_id", contact.ss_id)
            intent.putExtra("contact_name", contact.display_name)
            startActivity(intent)
        }

        // Swipe left to delete (from UI only, not from database)
        val swipeCallback = object : ItemTouchHelper.SimpleCallback(0, ItemTouchHelper.LEFT) {
            override fun onMove(rv: RecyclerView, vh: RecyclerView.ViewHolder, t: RecyclerView.ViewHolder) = false

            override fun onSwiped(vh: RecyclerView.ViewHolder, dir: Int) {
                val pos = vh.adapterPosition
                if (pos in filteredList.indices) {
                    val contact = filteredList[pos]
                    hiddenContacts.add(contact.ss_id)
                    applyFilter()
                    Toast.makeText(this@MainActivity, "${contact.display_name} СКРЫТ", Toast.LENGTH_SHORT).show()
                }
            }

            override fun onChildDraw(c: Canvas, rv: RecyclerView, vh: RecyclerView.ViewHolder,
                dX: Float, dY: Float, actionState: Int, isCurrentlyActive: Boolean) {
                val itemView = vh.itemView
                val alpha = 1f - Math.abs(dX) / itemView.width
                itemView.alpha = alpha
                super.onChildDraw(c, rv, vh, dX, dY, actionState, isCurrentlyActive)
            }
        }
        ItemTouchHelper(swipeCallback).attachToRecyclerView(binding.rvContacts)
    }

    private fun setupSearch() {
        binding.etSearch.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {}
            override fun afterTextChanged(s: Editable?) {
                searchQuery = s?.toString()?.trim() ?: ""
                applyFilter()
            }
        })

        // Toggle show all / hide inactive
        binding.btnShowAll.setOnClickListener {
            showAll = !showAll
            binding.btnShowAll.text = if (showAll) "🔒" else "👁"
            applyFilter()
        }
    }

    private fun applyFilter() {
        filteredList.clear()
        val query = searchQuery.lowercase()
        for (contact in contactsList) {
            // Hidden contacts only shown if search query is not empty
            val isHidden = hiddenContacts.contains(contact.ss_id)
            if (isHidden && query.isEmpty()) continue

            // If not showing all and no search, only show contacts with messages
            if (!showAll && query.isEmpty() && !isHidden) {
                if (!contactsWithMessages.contains(contact.ss_id)) continue
            }

            // Search filter
            if (query.isNotEmpty()) {
                val name = contact.display_name.lowercase()
                val ssid = contact.ss_id.lowercase()
                if (!name.contains(query) && !ssid.contains(query)) continue
            }

            filteredList.add(contact)
        }
        binding.rvContacts.adapter?.notifyDataSetChanged()
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
                        applyFilter()
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
                            contactsWithMessages.add(msg.from_ss)
                            applyFilter()
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
