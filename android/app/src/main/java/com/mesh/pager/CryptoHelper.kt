package com.mesh.pager

import android.content.Context
import android.util.Base64
import com.goterl.lazysodium.LazySodiumAndroid
import com.goterl.lazysodium.SodiumAndroid
import com.goterl.lazysodium.interfaces.Box

object CryptoHelper {

    private val sodium = SodiumAndroid()
    private val lazySodium = LazySodiumAndroid(sodium)

    data class KeyPair(val publicKey: ByteArray, val secretKey: ByteArray)
    data class EncryptedMessage(val ciphertext: String, val nonce: String, val fromPub: String)

    fun generateKeyPair(): KeyPair {
        val pk = ByteArray(Box.PUBLICKEYBYTES)
        val sk = ByteArray(Box.SECRETKEYBYTES)
        lazySodium.cryptoBoxKeypair(pk, sk)
        return KeyPair(pk, sk)
    }

    fun saveKeyPair(context: Context, kp: KeyPair) {
        val prefs = context.getSharedPreferences("pager_crypto", Context.MODE_PRIVATE)
        prefs.edit()
            .putString("public_key", toBase64(kp.publicKey))
            .putString("secret_key", toBase64(kp.secretKey))
            .apply()
    }

    fun loadKeyPair(context: Context): KeyPair? {
        val prefs = context.getSharedPreferences("pager_crypto", Context.MODE_PRIVATE)
        val pkB64 = prefs.getString("public_key", null) ?: return null
        val skB64 = prefs.getString("secret_key", null) ?: return null
        return KeyPair(fromBase64(pkB64), fromBase64(skB64))
    }

    fun getPublicKeyBase64(context: Context): String {
        val kp = loadKeyPair(context) ?: return ""
        return toBase64(kp.publicKey)
    }

    fun encrypt(plaintext: String, recipientPubKey: ByteArray, senderSecretKey: ByteArray, senderPubKey: ByteArray): EncryptedMessage {
        val nonce = ByteArray(Box.NONCEBYTES)
        lazySodium.randomBytesBuf(nonce, nonce.size)
        val messageBytes = plaintext.toByteArray(Charsets.UTF_8)
        val ciphertext = ByteArray(messageBytes.size + Box.MACBYTES)
        lazySodium.cryptoBoxEasy(ciphertext, messageBytes, messageBytes.size.toLong(), nonce, recipientPubKey, senderSecretKey)
        return EncryptedMessage(
            ciphertext = toBase64(ciphertext),
            nonce = toBase64(nonce),
            fromPub = toBase64(senderPubKey)
        )
    }

    fun decrypt(ciphertextB64: String, nonceB64: String, senderPubB64: String, receiverSecretKey: ByteArray): String? {
        return try {
            val ciphertext = fromBase64(ciphertextB64)
            val nonce = fromBase64(nonceB64)
            val senderPub = fromBase64(senderPubB64)
            val plaintext = ByteArray(ciphertext.size - Box.MACBYTES)
            val success = lazySodium.cryptoBoxOpenEasy(plaintext, ciphertext, ciphertext.size.toLong(), nonce, senderPub, receiverSecretKey)
            if (success) String(plaintext, Charsets.UTF_8) else null
        } catch (e: Exception) {
            null
        }
    }

    fun tryDecryptMessageText(text: String, context: Context): Pair<String, Boolean> {
        val kp = loadKeyPair(context) ?: return Pair(text, false)
        return try {
            val obj = com.google.gson.JsonParser.parseString(text).asJsonObject
            val ct = obj.get("ciphertext")?.asString ?: return Pair(text, false)
            val nc = obj.get("nonce")?.asString ?: return Pair(text, false)
            val fp = obj.get("from_pub")?.asString ?: return Pair(text, false)
            val plain = decrypt(ct, nc, fp, kp.secretKey)
            if (plain != null) Pair(plain, true) else Pair("[DECRYPT FAILED]", true)
        } catch (e: Exception) {
            Pair(text, false)
        }
    }

    fun encryptForContact(plaintext: String, recipientPubKeyB64: String, context: Context): String? {
        val kp = loadKeyPair(context) ?: return null
        if (recipientPubKeyB64.isEmpty()) return null
        val recipientPub = fromBase64(recipientPubKeyB64)
        val enc = encrypt(plaintext, recipientPub, kp.secretKey, kp.publicKey)
        return com.google.gson.Gson().toJson(mapOf(
            "ciphertext" to enc.ciphertext,
            "nonce" to enc.nonce,
            "from_pub" to enc.fromPub,
        ))
    }

    private fun toBase64(bytes: ByteArray): String = Base64.encodeToString(bytes, Base64.NO_WRAP)
    private fun fromBase64(str: String): ByteArray = Base64.decode(str, Base64.NO_WRAP)
}
