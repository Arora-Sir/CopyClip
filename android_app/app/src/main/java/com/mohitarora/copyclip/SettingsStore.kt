package com.mohitarora.copyclip

import android.content.Context

/**
 * Plain SharedPreferences, deliberately -- this project's own zero-third-party-dependency
 * rule (see the Windows/Python side's raw ctypes over pywin32) extends here: no DataStore
 * library, just the stock Android API. The hmac key must be the exact hex contents of the
 * laptop's copyclip.key (transports/tcp_transport.py's key_file), pasted in once.
 */
class SettingsStore(context: Context) {
    private val prefs = context.getSharedPreferences("copyclip_settings", Context.MODE_PRIVATE)

    var port: Int
        get() = prefs.getInt("port", DEFAULT_PORT)
        set(value) = prefs.edit().putInt("port", value).apply()

    var hmacKeyHex: String
        get() = prefs.getString("hmac_key_hex", "") ?: ""
        set(value) = prefs.edit().putString("hmac_key_hex", value.trim()).apply()

    var autoStartEnabled: Boolean
        get() = prefs.getBoolean("auto_start", true)
        set(value) = prefs.edit().putBoolean("auto_start", value).apply()

    /** Tailscale IP/hostname of the laptop -- only "Send Clip" needs this, since it is the
     * one direction where this device dials the hub instead of the other way round. */
    var hubHost: String
        get() = prefs.getString("hub_host", "") ?: ""
        set(value) = prefs.edit().putString("hub_host", value.trim()).apply()

    var hubPushPort: Int
        get() = prefs.getInt("hub_push_port", DEFAULT_PUSH_PORT)
        set(value) = prefs.edit().putInt("hub_push_port", value).apply()

    /** Must match this device's "name" entry in the laptop's config.json (e.g. "pixel"),
     * so a manual push routes and dedups exactly like an automatic one would. */
    var deviceName: String
        get() = prefs.getString("device_name", "pixel") ?: "pixel"
        set(value) = prefs.edit().putString("device_name", value.trim()).apply()

    var isProvisioned: Boolean
        get() = prefs.getBoolean("is_provisioned", false) || hmacKeyHex.length >= 32
        set(value) = prefs.edit().putBoolean("is_provisioned", value).apply()

    var autoSyncEnabled: Boolean
        get() = prefs.getBoolean("auto_sync_enabled", true)
        set(value) = prefs.edit().putBoolean("auto_sync_enabled", value).apply()

    fun saveFromConf(keyHex: String, host: String, pushPort: Int, name: String) {
        prefs.edit()
            .putString("hmac_key_hex", keyHex.trim())
            .putString("hub_host", host.trim())
            .putInt("hub_push_port", pushPort)
            .putString("device_name", name.trim())
            .putBoolean("is_provisioned", true)
            .apply()
    }

    /**
     * The laptop (copyclip.py's make_transport_factory / termux/copyclip_node.py) reads
     * copyclip.key's raw file bytes as-is for HMAC -- it never hex-decodes them, even though
     * the file's content happens to look like hex text. The key IS that text, byte for byte.
     * Decoding it here would silently compute a different HMAC key than the laptop and fail
     * every AUTH handshake with AUTH-FAIL -- exactly what happened before this comment did.
     */
    fun keyBytesOrNull(): ByteArray? {
        val text = hmacKeyHex
        if (text.length < 32) return null
        return text.toByteArray(Charsets.US_ASCII)
    }

    companion object {
        const val DEFAULT_PORT = 8386
        const val DEFAULT_PUSH_PORT = 8387
    }
}
