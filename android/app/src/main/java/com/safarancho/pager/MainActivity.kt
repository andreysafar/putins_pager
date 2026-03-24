package com.safarancho.pager

import android.content.Intent
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import com.safarancho.pager.databinding.ActivityMainBinding

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: android.content.SharedPreferences

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        prefs = getSharedPreferences("pager", MODE_PRIVATE)

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
            Toast.makeText(this, "Fill all fields", Toast.LENGTH_SHORT).show()
            return
        }
        // Simple synchronous-style for brevity; use coroutines in production
        Thread {
            try {
                val body = ApiService.register(name, displayName)
                prefs.edit().putString("ss_id", body.ss_id).putString("display_name", body.display_name).apply()
                runOnUiThread { showContacts(body.ss_id) }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }

    private fun showContacts(ssId: String) {
        binding.layoutRegister.visibility = android.view.View.GONE
        binding.layoutContacts.visibility = android.view.View.VISIBLE
        binding.tvMyId.text = "SS-ID: $ssId"
        binding.rvContacts.layoutManager = LinearLayoutManager(this)
        refreshContacts()
    }

    private fun refreshContacts() {
        Thread {
            try {
                val contacts = ApiService.getContacts()
                val myId = prefs.getString("ss_id", "")!!
                val filtered = contacts.filter { it.ss_id != myId }
                runOnUiThread {
                    binding.rvContacts.adapter = ContactAdapter(filtered) { contact ->
                        val intent = Intent(this, ChatActivity::class.java)
                        intent.putExtra("contact_ss_id", contact.ss_id)
                        intent.putExtra("contact_name", contact.display_name)
                        startActivity(intent)
                    }
                }
            } catch (e: Exception) {
                runOnUiThread { Toast.makeText(this, "Error: ${e.message}", Toast.LENGTH_SHORT).show() }
            }
        }.start()
    }
}
