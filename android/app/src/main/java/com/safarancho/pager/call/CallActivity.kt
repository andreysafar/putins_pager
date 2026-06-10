package com.safarancho.pager.call

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.safarancho.pager.databinding.ActivityCallBinding
import org.json.JSONObject
import org.webrtc.AudioTrack
import org.webrtc.Camera2Enumerator
import org.webrtc.DefaultVideoDecoderFactory
import org.webrtc.DefaultVideoEncoderFactory
import org.webrtc.EglBase
import org.webrtc.IceCandidate
import org.webrtc.MediaConstraints
import org.webrtc.MediaStream
import org.webrtc.PeerConnection
import org.webrtc.PeerConnectionFactory
import org.webrtc.SdpObserver
import org.webrtc.SessionDescription
import org.webrtc.SurfaceTextureHelper
import org.webrtc.VideoCapturer
import org.webrtc.VideoTrack

/**
 * One-to-one WebRTC video call. Signaling (offer/answer/ICE/end) rides the
 * [SignalingClient] WebSocket; media flows peer-to-peer. Interoperable with the
 * web client's call UI.
 *
 * Intent extras:
 *  - "my_ss"   : our SSID
 *  - "peer_ss" : the other party's SSID
 *  - "incoming": true if launched to answer an incoming offer
 *  - "offer_sdp": offer SDP string when incoming
 */
class CallActivity : AppCompatActivity() {

    private lateinit var binding: ActivityCallBinding
    private val eglBase: EglBase by lazy { EglBase.create() }
    private lateinit var factory: PeerConnectionFactory
    private var peerConnection: PeerConnection? = null
    private var localVideoTrack: VideoTrack? = null
    private var localAudioTrack: AudioTrack? = null
    private var videoCapturer: VideoCapturer? = null
    private lateinit var signaling: SignalingClient

    private lateinit var mySs: String
    private lateinit var peerSs: String
    private var incoming = false
    private var incomingOffer: String? = null

    private val rtcConfig = PeerConnection.RTCConfiguration(
        listOf(PeerConnection.IceServer.builder("stun:stun.l.google.com:19302").createIceServer())
    )

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityCallBinding.inflate(layoutInflater)
        setContentView(binding.root)

        mySs = intent.getStringExtra("my_ss") ?: ""
        peerSs = intent.getStringExtra("peer_ss") ?: ""
        incoming = intent.getBooleanExtra("incoming", false)
        incomingOffer = intent.getStringExtra("offer_sdp")

        binding.btnEnd.setOnClickListener { hangUp() }
        binding.btnMute.setOnClickListener { localAudioTrack?.let { it.setEnabled(!it.enabled()) } }
        binding.btnCam.setOnClickListener { localVideoTrack?.let { it.setEnabled(!it.enabled()) } }

        if (!hasPermissions()) {
            ActivityCompat.requestPermissions(this, PERMS, REQ)
        } else {
            startCall()
        }
    }

    private fun hasPermissions() = PERMS.all {
        ContextCompat.checkSelfPermission(this, it) == PackageManager.PERMISSION_GRANTED
    }

    override fun onRequestPermissionsResult(req: Int, perms: Array<out String>, results: IntArray) {
        super.onRequestPermissionsResult(req, perms, results)
        if (req == REQ && results.isNotEmpty() && results.all { it == PackageManager.PERMISSION_GRANTED }) {
            startCall()
        } else {
            Toast.makeText(this, "Нужны камера и микрофон", Toast.LENGTH_LONG).show()
            finish()
        }
    }

    private fun startCall() {
        initFactory()
        initRenderers()
        createPeerConnection()
        startLocalMedia()
        signaling = SignalingClient(mySs) { from, payload -> onSignal(from, payload) }
        signaling.connect()

        if (incoming && incomingOffer != null) {
            binding.tvStatus.text = "CONNECTING…"
            peerConnection?.setRemoteDescription(
                SimpleSdpObserver(),
                SessionDescription(SessionDescription.Type.OFFER, incomingOffer)
            )
            createAnswer()
        } else {
            binding.tvStatus.text = "CALLING $peerSs…"
            createOffer()
        }
    }

    private fun initFactory() {
        PeerConnectionFactory.initialize(
            PeerConnectionFactory.InitializationOptions.builder(applicationContext).createInitializationOptions()
        )
        val encoder = DefaultVideoEncoderFactory(eglBase.eglBaseContext, true, true)
        val decoder = DefaultVideoDecoderFactory(eglBase.eglBaseContext)
        factory = PeerConnectionFactory.builder()
            .setVideoEncoderFactory(encoder)
            .setVideoDecoderFactory(decoder)
            .createPeerConnectionFactory()
    }

    private fun initRenderers() {
        binding.localView.init(eglBase.eglBaseContext, null)
        binding.localView.setMirror(true)
        binding.remoteView.init(eglBase.eglBaseContext, null)
    }

    private fun createPeerConnection() {
        peerConnection = factory.createPeerConnection(rtcConfig, object : PeerConnection.Observer {
            override fun onIceCandidate(candidate: IceCandidate) {
                val p = JSONObject().put("kind", "ice").put("candidate", JSONObject()
                    .put("sdpMid", candidate.sdpMid)
                    .put("sdpMLineIndex", candidate.sdpMLineIndex)
                    .put("candidate", candidate.sdp))
                signaling.send(peerSs, p)
            }
            override fun onAddStream(stream: MediaStream) {
                runOnUiThread {
                    stream.videoTracks.firstOrNull()?.addSink(binding.remoteView)
                    binding.tvStatus.text = "● LIVE"
                }
            }
            override fun onConnectionChange(newState: PeerConnection.PeerConnectionState) {
                if (newState == PeerConnection.PeerConnectionState.DISCONNECTED ||
                    newState == PeerConnection.PeerConnectionState.FAILED) {
                    runOnUiThread { hangUp() }
                }
            }
            override fun onSignalingChange(p0: PeerConnection.SignalingState?) {}
            override fun onIceConnectionChange(p0: PeerConnection.IceConnectionState?) {}
            override fun onIceConnectionReceivingChange(p0: Boolean) {}
            override fun onIceGatheringChange(p0: PeerConnection.IceGatheringState?) {}
            override fun onIceCandidatesRemoved(p0: Array<out IceCandidate>?) {}
            override fun onRemoveStream(p0: MediaStream?) {}
            override fun onDataChannel(p0: org.webrtc.DataChannel?) {}
            override fun onRenegotiationNeeded() {}
            override fun onAddTrack(p0: org.webrtc.RtpReceiver?, p1: Array<out MediaStream>?) {}
        })
    }

    private fun startLocalMedia() {
        val helper = SurfaceTextureHelper.create("CaptureThread", eglBase.eglBaseContext)
        videoCapturer = createCameraCapturer()
        val source = factory.createVideoSource(false)
        videoCapturer?.initialize(helper, applicationContext, source.capturerObserver)
        videoCapturer?.startCapture(1280, 720, 30)
        localVideoTrack = factory.createVideoTrack("v0", source).apply {
            addSink(binding.localView)
        }
        val audioSource = factory.createAudioSource(MediaConstraints())
        localAudioTrack = factory.createAudioTrack("a0", audioSource)

        val stream = factory.createLocalMediaStream("local")
        stream.addTrack(localVideoTrack)
        stream.addTrack(localAudioTrack)
        peerConnection?.addStream(stream)
    }

    private fun createCameraCapturer(): VideoCapturer? {
        val enumerator = Camera2Enumerator(this)
        // Prefer front camera.
        enumerator.deviceNames.firstOrNull { enumerator.isFrontFacing(it) }?.let {
            return enumerator.createCapturer(it, null)
        }
        return enumerator.deviceNames.firstOrNull()?.let { enumerator.createCapturer(it, null) }
    }

    private fun mediaConstraints() = MediaConstraints().apply {
        mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveAudio", "true"))
        mandatory.add(MediaConstraints.KeyValuePair("OfferToReceiveVideo", "true"))
    }

    private fun createOffer() {
        peerConnection?.createOffer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(sdp: SessionDescription) {
                peerConnection?.setLocalDescription(SimpleSdpObserver(), sdp)
                signaling.send(peerSs, JSONObject().put("kind", "offer").put("sdp", sdp.description))
            }
        }, mediaConstraints())
    }

    private fun createAnswer() {
        peerConnection?.createAnswer(object : SimpleSdpObserver() {
            override fun onCreateSuccess(sdp: SessionDescription) {
                peerConnection?.setLocalDescription(SimpleSdpObserver(), sdp)
                signaling.send(peerSs, JSONObject().put("kind", "answer").put("sdp", sdp.description))
            }
        }, mediaConstraints())
    }

    private fun onSignal(from: String, payload: JSONObject) {
        when (payload.optString("kind")) {
            "answer" -> peerConnection?.setRemoteDescription(
                SimpleSdpObserver(),
                SessionDescription(SessionDescription.Type.ANSWER, payload.optString("sdp"))
            )
            "ice" -> payload.optJSONObject("candidate")?.let { c ->
                peerConnection?.addIceCandidate(
                    IceCandidate(c.optString("sdpMid"), c.optInt("sdpMLineIndex"), c.optString("candidate"))
                )
            }
            "end", "unavailable" -> runOnUiThread { hangUp() }
        }
    }

    private fun hangUp() {
        try { signaling.send(peerSs, JSONObject().put("kind", "end")) } catch (_: Exception) {}
        cleanup()
        finish()
    }

    private fun cleanup() {
        try { videoCapturer?.stopCapture() } catch (_: Exception) {}
        videoCapturer?.dispose()
        localVideoTrack?.dispose()
        localAudioTrack?.dispose()
        peerConnection?.close()
        peerConnection = null
        try { signaling.close() } catch (_: Exception) {}
        try { binding.localView.release(); binding.remoteView.release() } catch (_: Exception) {}
    }

    override fun onDestroy() {
        cleanup()
        super.onDestroy()
    }

    /** SDP observer with no-op defaults so subclasses override only what they need. */
    private open class SimpleSdpObserver : SdpObserver {
        override fun onCreateSuccess(sdp: SessionDescription) {}
        override fun onSetSuccess() {}
        override fun onCreateFailure(error: String?) {}
        override fun onSetFailure(error: String?) {}
    }

    companion object {
        private const val REQ = 4201
        private val PERMS = arrayOf(Manifest.permission.CAMERA, Manifest.permission.RECORD_AUDIO)
    }
}
