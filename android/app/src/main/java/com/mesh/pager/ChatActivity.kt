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
    private lateinit var myToken: String
    private lateinit var contactSS: String
    private lateinit var contactName: String
    private var contactPubKey: String = ""
    private val messages = mutableListOf<Message>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityChatBinding.inflate(layoutInflater)
        setContentView(binding.root)

        val prefs = getSharedPreferences("pager_prefs", MODE_PRIVATE)
        mySS = prefs.getString("ss_id", "")!!
        myToken = prefs.getString("token", "")!!
        contactSS = intent.getStringExtra("contact_ss_id") ?: ""
        contactName = intent.getStringExtra("contact_name") ?: intent.getStringExtra("to_name") ?: ""
        contactPubKey = intent.getStringExtra("contact_public_key") ?: ""

        NotificationHelper.clearNotification(this, contactSS)

        val e2eLabel = if (contactPubKey.isNotEmpty()) " [E2E]" else " [PLAIN]"
        binding.tvChatWith.text = "$contactName$e2eLabel"
        binding.rvMessages.layoutManager = LinearLayoutManager(this)

        binding.btnSend.setOnClickListener { sendMessage() }
        binding.btnBack.setOnClickListener { finish() }

        loadMessages()
        connectWebSocket()
    }

    private fun connectWebSocket() {
        val wsUrl = BuildConfig.BASE_URL.replace("http", "ws") + "/ws/$mySS?token=$myToken"
        ws = object : WebSocketClient(URI(wsUrl)) {
            override fun onOpen(handshakedata: ServerHandshake?) {}
            override fun onMessage(message: String?) {
                message ?: return
                try {
                    val msg = ApiService.gson.fromJson(message, Message::class.java)
                    if (msg.from_ss == mySS) return

                    // Decrypt E2E if possible
                    val (plainText, _) = CryptoHelper.tryDecryptMessageText(msg.text, this@ChatActivity)
                    val decryptedMsg = msg.copy(text = plainText)

                    runOnUiThread {
                        if (msg.from_ss == contactSS) {
                            messages.add(decryptedMsg)
                            binding.rvMessages.adapter?.notifyItemInserted(messages.size - 1)
                            binding.rvMessages.scrollToPosition(messages.size - 1)
                        } else {
                            NotificationHelper.showMessageNotification(
                                this@ChatActivity, msg.from_ss, msg.from_ss, plainText
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
                }.map { msg ->
                    val (plainText, _) = CryptoHelper.tryDecryptMessageText(msg.text, this@ChatActivity)
                    msg.copy(text = plainText)
                }.reversed())
                runOnUiThread {
                    binding.rvMessages.adapter = MessageAdapter(messages, mySS)
                    binding.rvMessages.scrollToPosition(messages.size - 1)
                }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun sendMessage() {
        val text = binding.etMessage.text.toString().trim()
        if (text.isEmpty()) return
        binding.etMessage.setText("")
        Thread {
            try {
                // Encrypt if we have recipient's public key
                val payload = if (contactPubKey.isNotEmpty()) {
                    CryptoHelper.encryptForContact(text, contactPubKey, this@ChatActivity) ?: text
                } else {
                    text
                }
                ApiService.sendMessage(mySS, contactSS, payload)
                runOnUiThread {
                    val msg = Message(
                        from_ss = mySS, to_ss = contactSS,
                        text = text, created_at = java.time.Instant.now().toString()
                    )
                    messages.add(msg)
                    binding.rvMessages.adapter?.notifyItemInserted(messages.size - 1)
                    binding.rvMessages.scrollToPosition(messages.size - 1)
                }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    override fun onDestroy() {
        super.onDestroy()
        ws?.close()
    }
}
