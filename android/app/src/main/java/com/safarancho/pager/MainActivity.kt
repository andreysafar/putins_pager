package com.safarancho.pager

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.view.View
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import com.safarancho.pager.databinding.ActivityMainBinding
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.net.URI

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: android.content.SharedPreferences
    private var ws: WebSocketClient? = null
    private val unreadCounts = mutableMapOf<String, Int>()
    private var currentContacts: List<Contact> = emptyList()

    // Notification permission request (Android 13+)
    private val notifPermLauncher = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) {
            Toast.makeText(this, "Уведомления включены", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences("pager", MODE_PRIVATE)

        // Create notification channel
        NotificationHelper.createChannel(this)

        // Request notification permission on Android 13+
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
                notifPermLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
            }
        }

        // Check if already registered
        val ssId = prefs.getString("ss_id", null)
        if (ssId != null) {
            showContacts(ssId)
        }

        binding.btnRegister.setOnClickListener { doRegister() }
        binding.btnRefresh.setOnClickListener { refreshContacts() }
    }

    private fun doRegister() {
        val name = binding.etName.text.toString().trim()
        val displayName = binding.etDisplayName.text.toString().trim()
        if (name.isEmpty() || displayName.isEmpty()) {
            Toast.makeText(this, "Заполни все поля", Toast.LENGTH_SHORT).show()
            return
        }
        Thread {
            try {
                val body = ApiService.register(name, displayName)
                prefs.edit()
                    .putString("ss_id", body.ss_id)
                    .putString("display_name", body.display_name)
                    .apply()
                runOnUiThread { showContacts(body.ss_id) }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Ошибка: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun showContacts(ssId: String) {
        binding.layoutRegister.visibility = View.GONE
        binding.layoutContacts.visibility = View.VISIBLE
        binding.tvMyId.text = "SS-ID: $ssId"
        binding.rvContacts.layoutManager = LinearLayoutManager(this)
        refreshContacts()
        connectBackgroundWS(ssId)
        loadSmtpStatus()
    }

    private fun refreshContacts() {
        Thread {
            try {
                val contacts = ApiService.getContacts()
                val myId = prefs.getString("ss_id", "")!!
                currentContacts = contacts.filter { it.ss_id != myId }
                runOnUiThread { updateContactsList() }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Ошибка: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun updateContactsList() {
        binding.rvContacts.adapter = ContactAdapter(currentContacts, unreadCounts) { contact ->
            // Clear badge when opening chat
            unreadCounts.remove(contact.ss_id)
            NotificationHelper.clearNotification(this, contact.ss_id)
            updateBadgeLamp()

            val intent = Intent(this, ChatActivity::class.java)
            intent.putExtra("contact_ss_id", contact.ss_id)
            intent.putExtra("contact_name", contact.display_name)
            startActivity(intent)
        }
        updateBadgeLamp()
    }

    // ─── Background WebSocket for notifications on main screen ───
    private fun connectBackgroundWS(ssId: String) {
        val wsUrl = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$ssId"
        ws = object : WebSocketClient(URI(wsUrl)) {
            override fun onOpen(handshakedata: ServerHandshake?) {}
            override fun onMessage(message: String?) {
                message ?: return
                try {
                    val msg = ApiService.gson.fromJson(message, Message::class.java)
                    if (msg.from_ss == ssId) return  // Skip own echoes

                    val contact = currentContacts.find { it.ss_id == msg.from_ss }
                    val displayName = contact?.display_name ?: msg.from_ss

                    // Increment unread count
                    unreadCounts[msg.from_ss] = (unreadCounts[msg.from_ss] ?: 0) + 1

                    runOnUiThread {
                        updateContactsList()
                        // Show system notification
                        NotificationHelper.showMessageNotification(
                            this@MainActivity, msg.from_ss, displayName, msg.text
                        )
                    }
                } catch (_: Exception) {}
            }
            override fun onClose(code: Int, reason: String?, remote: Boolean) {
                // Reconnect after 3s
                android.os.Handler(mainLooper).postDelayed({
                    if (!isFinishing) connectBackgroundWS(ssId)
                }, 3000)
            }
            override fun onError(ex: Exception?) {}
        }
        ws?.connect()
    }

    // ─── Header badge lamp ───
    private fun updateBadgeLamp() {
        val total = unreadCounts.values.sum()
        // Update the header title with badge indicator
        val badgeText = if (total > 0) "🏊 Safarancho Pager  ●" else "🏊 Safarancho Pager"
        // The binding uses a TextView directly, so update it
        try {
            val tvHeader = findViewById<android.widget.TextView>(R.id.tvHeaderTitle)
                ?: return
            tvHeader.text = badgeText
            if (total > 0) {
                tvHeader.setTextColor(getColor(R.color.accent))  // Orange when unread
                // Pulse animation
                tvHeader.animate().alpha(0.6f).setDuration(500).withEndAction {
                    tvHeader.animate().alpha(1f).setDuration(500).start()
                }.start()
            } else {
                tvHeader.setTextColor(getColor(R.color.accent))
            }
        } catch (_: Exception) {}
    }

    // ─── SMTP Status ───
    private fun loadSmtpStatus() {
        Thread {
            try {
                val req = okhttp3.Request.Builder().url("${BuildConfig.BASE_URL}/smtp/status").build()
                val client = okhttp3.OkHttpClient()
                client.newCall(req).execute().use { resp ->
                    val body = resp.body?.string() ?: return@use
                    val gson = com.google.gson.Gson()
                    val data = gson.fromJson(body, SmtpStatus::class.java)
                    runOnUiThread { showSmtpStatus(data) }
                }
            } catch (_: Exception) {
                runOnUiThread {
                    // SMTP section is optional; silently skip on error
                }
            }
        }.start()
    }

    private fun showSmtpStatus(status: SmtpStatus) {
        // SMTP status text in the contacts screen header area
        val smtpText = if (status.configured) {
            "📡 SMTP: ● ${status.host}:${status.port}"
        } else {
            "📡 SMTP: не настроен"
        }
        // Add a small toast or subtitle — the layout doesn't have an SMTP field yet
        // so we show it as a toast on first load
        if (!status.configured) {
            binding.tvMyId.text = "${binding.tvMyId.text}\n$smtpText"
        }
    }

    override fun onResume() {
        super.onResume()
        // Refresh contacts (and badges) when returning from ChatActivity
        if (prefs.getString("ss_id", null) != null) {
            refreshContacts()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close()
    }
}

// SMTP status model
data class SmtpStatus(
    val configured: Boolean = false,
    val host: String = "",
    val port: Int = 0,
    val user: String = "",
    val from_addr: String = "",
    val reachable: Boolean = false
)
