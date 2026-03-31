package com.mesh.pager

import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.mesh.pager.databinding.ActivityChatBinding
import org.java_websocket.client.WebSocketClient
import org.java_websocket.handshake.ServerHandshake
import java.net.URI

class ChatActivity : AppCompatActivity() {

    private lateinit var binding: ActivityChatBinding
    private var ws: WebSocketClient? = null
    private lateinit var mySS: String
    private lateinit var contactSS: String
    private lateinit var contactName: String
    private val messages = mutableListOf<Message>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        mySS = getSharedPreferences("pager_prefs", MODE_PRIVATE).getString("ss_id", "")!!
        contactSS = intent.getStringExtra("contact_ss_id") ?: ""
        contactName = intent.getStringExtra("contact_name") ?: intent.getStringExtra("to_name") ?: ""

        // Clear notification for this contact
        NotificationHelper.clearNotification(this, contactSS)

        binding.tvChatWith.text = contactName
        binding.rvMessages.layoutManager = LinearLayoutManager(this)

        binding.btnSend.setOnClickListener { sendMessage() }
        binding.btnBack.setOnClickListener { finish() }

        loadMessages()
        connectWebSocket()
    }

    private fun connectWebSocket() {
        val wsUrl = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$mySS"
        ws = object : WebSocketClient(URI(wsUrl)) {
            override fun onOpen(handshakedata: ServerHandshake?) {}
            override fun onMessage(message: String?) {
                message ?: return
                try {
                    val msg = ApiService.gson.fromJson(message, Message::class.java)
                    if (msg.from_ss == mySS) return  // Skip own echoes

                    runOnUiThread {
                        if (msg.from_ss == contactSS) {
                            // Message in current chat — display it
                            messages.add(msg)
                            binding.rvMessages.adapter?.notifyItemInserted(messages.size - 1)
                            binding.rvMessages.scrollToPosition(messages.size - 1)
                        } else {
                            // Message from OTHER contact — show push notification
                            // (don't disrupt current chat view)
                            NotificationHelper.showMessageNotification(
                                this@ChatActivity, msg.from_ss, msg.from_ss, msg.text
                            )
                        }
                    }
                } catch (_: Exception) {}
            }
            override fun onClose(code: Int, reason: String?, remote: Boolean) {
                android.os.Handler(mainLooper).postDelayed({
                    if (!isFinishing) connectWebSocket()
                }, 3000)
            }
            override fun onError(ex: Exception?) {}
        }
        ws?.connect()
    }

    private fun loadMessages() {
        Thread {
            try {
                val msgs = ApiService.getMessages(mySS)
                messages.clear()
                messages.addAll(msgs.filter {
                    (it.from_ss == mySS && it.to_ss == contactSS) ||
                    (it.from_ss == contactSS && it.to_ss == mySS)
                }.reversed())
                runOnUiThread {
                    binding.rvMessages.adapter = MessageAdapter(messages, mySS)
                    binding.rvMessages.scrollToPosition(messages.size - 1)
                }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Ошибка: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun sendMessage() {
        val text = binding.etMessage.text.toString().trim()
        if (text.isEmpty()) return
        binding.etMessage.setText("")
        Thread {
            try {
                ApiService.sendMessage(mySS, contactSS, text)
                runOnUiThread {
                    val msg = Message(
                        from_ss = mySS, to_ss = contactSS,
                        text = text, created_at = java.time.Instant.now().toString()
                    )
                    messages.add(0, msg)
                    binding.rvMessages.adapter?.notifyItemInserted(0)
                }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Ошибка: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close()
    }
}
