package com.safarancho.pager.crypto

import android.util.Base64
import org.json.JSONObject
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PublicKey
import java.security.spec.X509EncodedKeySpec
import javax.crypto.Cipher
import javax.crypto.KeyAgreement
import javax.crypto.spec.GCMParameterSpec
import javax.crypto.spec.SecretKeySpec
import java.security.MessageDigest
import java.security.SecureRandom

/**
 * Minimal end-to-end crypto for pager messages.
 *
 * Scheme: each device holds an EC P-256 key pair. To send to a peer we do
 * ECDH(our private, their public) → SHA-256 → AES-256-GCM key, encrypt with a
 * random 12-byte nonce. The public key is published to the node's key registry
 * (`POST /keys`) as base64-DER and fetched per recipient.
 *
 * Limitation: without signed envelopes the registry has no authenticity, so
 * this protects against passive reading (DB dump, wire sniffing on a hop), not
 * an active man-in-the-middle. Envelope signing is the documented next step.
 */
object CryptoBox {

    private val rng = SecureRandom()

    fun generateKeyPair(): KeyPair {
        val kpg = KeyPairGenerator.getInstance("EC")
        kpg.initialize(256)
        return kpg.generateKeyPair()
    }

    /** Public key as base64(DER) for publishing to the registry. */
    fun exportPublicKey(kp: KeyPair): String =
        Base64.encodeToString(kp.public.encoded, Base64.NO_WRAP)

    fun importPublicKey(b64: String): PublicKey {
        val der = Base64.decode(b64, Base64.NO_WRAP)
        return KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(der))
    }

    private fun sharedKey(kp: KeyPair, peerPub: PublicKey): SecretKeySpec {
        val ka = KeyAgreement.getInstance("ECDH")
        ka.init(kp.private)
        ka.doPhase(peerPub, true)
        val secret = MessageDigest.getInstance("SHA-256").digest(ka.generateSecret())
        return SecretKeySpec(secret, "AES")
    }

    /** Returns a JSON string {"enc":1,"n":<b64 nonce>,"c":<b64 ciphertext>}. */
    fun encrypt(kp: KeyPair, peerPub: PublicKey, plaintext: String): String {
        val key = sharedKey(kp, peerPub)
        val nonce = ByteArray(12).also { rng.nextBytes(it) }
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key, GCMParameterSpec(128, nonce))
        val ct = cipher.doFinal(plaintext.toByteArray())
        return JSONObject()
            .put("enc", 1)
            .put("n", Base64.encodeToString(nonce, Base64.NO_WRAP))
            .put("c", Base64.encodeToString(ct, Base64.NO_WRAP))
            .toString()
    }

    /** True if [text] looks like one of our encrypted envelopes. */
    fun isEncrypted(text: String): Boolean =
        text.startsWith("{") && text.contains("\"enc\"")

    /** Decrypt an envelope produced by [encrypt]; returns plaintext or null. */
    fun decrypt(kp: KeyPair, peerPub: PublicKey, envelope: String): String? {
        return try {
            val o = JSONObject(envelope)
            val nonce = Base64.decode(o.getString("n"), Base64.NO_WRAP)
            val ct = Base64.decode(o.getString("c"), Base64.NO_WRAP)
            val key = sharedKey(kp, peerPub)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            cipher.init(Cipher.DECRYPT_MODE, key, GCMParameterSpec(128, nonce))
            String(cipher.doFinal(ct))
        } catch (e: Exception) {
            null
        }
    }
}
