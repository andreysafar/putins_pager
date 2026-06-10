package com.safarancho.pager.mesh

import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattServer
import android.bluetooth.BluetoothGattServerCallback
import android.bluetooth.BluetoothGattService
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.AdvertiseCallback
import android.bluetooth.le.AdvertiseData
import android.bluetooth.le.AdvertiseSettings
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.ParcelUuid
import android.util.Log
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap

/**
 * Offline BLE mesh transport.
 *
 * Each device runs simultaneously as:
 *  - a **GATT server** (peripheral) advertising [MESH_SERVICE_UUID] with a
 *    writable [MESH_CHAR_UUID] characteristic that peers write envelopes into;
 *  - a **scanner/central** that finds nearby peers advertising the same service
 *    and connects to push outgoing envelopes.
 *
 * Envelopes larger than the negotiated MTU are split into framed chunks
 * (see [ChunkAssembler]). This is the path that works with no internet and no
 * Wi-Fi — pure phone-to-phone relay, hop by hop.
 *
 * Requires runtime permissions BLUETOOTH_ADVERTISE / BLUETOOTH_SCAN /
 * BLUETOOTH_CONNECT (Android 12+) — the caller must grant them before start().
 */
@SuppressLint("MissingPermission")
class BleTransport(
    private val context: Context,
    private val listener: MeshListener,
) : Transport {

    override val name = "ble"

    private val btManager = context.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager
    private val adapter: BluetoothAdapter? = btManager.adapter

    private var gattServer: BluetoothGattServer? = null
    private var advertiser = adapter?.bluetoothLeAdvertiser
    private var scanner = adapter?.bluetoothLeScanner

    // Devices we've discovered and can push to.
    private val knownPeers = ConcurrentHashMap<String, BluetoothDevice>()
    private val assembler = ChunkAssembler()

    @Volatile
    private var running = false

    override val isAvailable: Boolean
        get() = running && adapter?.isEnabled == true && knownPeers.isNotEmpty()

    override fun start() {
        if (adapter?.isEnabled != true) {
            Log.w(TAG, "Bluetooth disabled; BLE transport inactive")
            return
        }
        running = true
        startGattServer()
        startAdvertising()
        startScanning()
    }

    override fun stop() {
        running = false
        try { advertiser?.stopAdvertising(advertiseCallback) } catch (_: Exception) {}
        try { scanner?.stopScan(scanCallback) } catch (_: Exception) {}
        try { gattServer?.close() } catch (_: Exception) {}
        knownPeers.clear()
    }

    override fun send(envelope: MeshEnvelope): Boolean {
        if (!running) return false
        val frames = ChunkAssembler.frame(envelope.msgId, envelope.toJson().toByteArray())
        var sentToAny = false
        for (device in knownPeers.values) {
            if (writeFrames(device, frames)) sentToAny = true
        }
        return sentToAny
    }

    // --- GATT server (receive side) --------------------------------------
    private fun startGattServer() {
        val server = btManager.openGattServer(context, serverCallback) ?: return
        val service = BluetoothGattService(MESH_SERVICE_UUID, BluetoothGattService.SERVICE_TYPE_PRIMARY)
        val characteristic = BluetoothGattCharacteristic(
            MESH_CHAR_UUID,
            BluetoothGattCharacteristic.PROPERTY_WRITE or BluetoothGattCharacteristic.PROPERTY_WRITE_NO_RESPONSE,
            BluetoothGattCharacteristic.PERMISSION_WRITE,
        )
        service.addCharacteristic(characteristic)
        server.addService(service)
        gattServer = server
    }

    private val serverCallback = object : BluetoothGattServerCallback() {
        override fun onCharacteristicWriteRequest(
            device: BluetoothDevice, requestId: Int, characteristic: BluetoothGattCharacteristic,
            preparedWrite: Boolean, responseNeeded: Boolean, offset: Int, value: ByteArray,
        ) {
            if (characteristic.uuid == MESH_CHAR_UUID) {
                val complete = assembler.accept(value)
                if (complete != null) {
                    try {
                        val env = MeshEnvelope.fromJson(String(complete))
                        listener.onEnvelope(env, name)
                    } catch (e: Exception) {
                        Log.w(TAG, "bad BLE envelope: ${e.message}")
                    }
                }
                // Remember the peer so we can relay back to it.
                knownPeers[device.address] = device
            }
            if (responseNeeded) {
                gattServer?.sendResponse(device, requestId, BluetoothGatt.GATT_SUCCESS, offset, null)
            }
        }
    }

    // --- Advertising (be discoverable) -----------------------------------
    private fun startAdvertising() {
        val settings = AdvertiseSettings.Builder()
            .setAdvertiseMode(AdvertiseSettings.ADVERTISE_MODE_BALANCED)
            .setConnectable(true)
            .setTxPowerLevel(AdvertiseSettings.ADVERTISE_TX_POWER_MEDIUM)
            .build()
        val data = AdvertiseData.Builder()
            .addServiceUuid(ParcelUuid(MESH_SERVICE_UUID))
            .setIncludeDeviceName(false)
            .build()
        try {
            advertiser?.startAdvertising(settings, data, advertiseCallback)
        } catch (e: Exception) {
            Log.w(TAG, "advertise failed: ${e.message}")
        }
    }

    private val advertiseCallback = object : AdvertiseCallback() {
        override fun onStartFailure(errorCode: Int) {
            Log.w(TAG, "advertise onStartFailure=$errorCode")
        }
    }

    // --- Scanning (discover peers) ---------------------------------------
    private fun startScanning() {
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(MESH_SERVICE_UUID)).build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        try {
            scanner?.startScan(listOf(filter), settings, scanCallback)
        } catch (e: Exception) {
            Log.w(TAG, "scan failed: ${e.message}")
        }
    }

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            knownPeers[result.device.address] = result.device
        }
    }

    // --- Outgoing write (central side) -----------------------------------
    private fun writeFrames(device: BluetoothDevice, frames: List<ByteArray>): Boolean {
        return try {
            // A production build would maintain persistent GATT connections and
            // a write queue; here we connect, write all frames, and disconnect.
            val gatt = device.connectGatt(context, false, object : BluetoothGattCallback() {
                override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
                    if (newState == BluetoothProfile.STATE_CONNECTED) {
                        g.discoverServices()
                    } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                        g.close()
                    }
                }

                override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
                    val ch = g.getService(MESH_SERVICE_UUID)?.getCharacteristic(MESH_CHAR_UUID)
                    if (ch == null) { g.disconnect(); return }
                    for (frame in frames) {
                        ch.value = frame
                        ch.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                        g.writeCharacteristic(ch)
                    }
                    g.disconnect()
                }
            })
            gatt != null
        } catch (e: Exception) {
            Log.w(TAG, "writeFrames failed: ${e.message}")
            false
        }
    }

    companion object {
        private const val TAG = "BleTransport"
        // Custom 128-bit UUIDs identifying the Safarancho mesh GATT service.
        val MESH_SERVICE_UUID: UUID = UUID.fromString("5a4a4f4d-0001-4a4f-8e4a-7061676572aa")
        val MESH_CHAR_UUID: UUID = UUID.fromString("5a4a4f4d-0002-4a4f-8e4a-7061676572bb")
    }
}
