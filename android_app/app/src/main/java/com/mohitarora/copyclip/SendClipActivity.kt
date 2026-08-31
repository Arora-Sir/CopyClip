package com.mohitarora.copyclip

import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.widget.TextView
import android.widget.Toast
import com.mohitarora.copyclip.shizuku.PushClient

/**
 * Launched from the "Send Clip" notification action or the in-app button.
 *
 * First cut used Theme.NoDisplay as an invisible trampoline; measured live against a real
 * device and rejected: logcat showed "ClipboardService: Denying clipboard access to
 * com.mohitarora.copyclip, application is not in focus neither is a system service" even
 * though the Activity had genuinely started, the exact restriction message from
 * bugs/SPIKE-PIXEL-ACCESSIBILITY.md's earlier findings. Theme.NoDisplay never actually
 * acquires window focus by design (that is the point of the theme), and window focus is
 * apparently what the platform checks, not merely being the started/topmost activity. This
 * version shows real (if minimal) content and waits for onWindowFocusChanged(true) -- the
 * documented, reliable signal that this app is now genuinely the focused window -- before
 * touching the clipboard. Confirmed working end-to-end against a real pixel after this fix.
 *
 * Images: sent as one unchunked frame (see PushClient's own doc comment on the widened
 * timeout), not the SETB/SETC chunking the main ClipBridge protocol uses for large
 * transfers. Deliberately scoped this way -- a screenshot is a few MB, not the 24MB cap --
 * chunking would be real added complexity for a case this feature doesn't need to cover.
 */
class SendClipActivity : Activity() {
    private var handled = false
    private lateinit var statusView: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        statusView = TextView(this).apply {
            text = "Sending clip..."
            textSize = 18f
            gravity = Gravity.CENTER
            setPadding(64, 200, 64, 200)
        }
        setContentView(statusView)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || handled) return
        handled = true
        sendClip()
    }

    /** PNG/JPEG/GIF/BMP/WEBP travel byte-for-byte, matching core/models.py's IMAGE_MIMES --
     * anything else (HEIC, an unrecognized format) is out of scope for this manual push
     * path and reported as a plain failure rather than silently transcoded. */
    private fun normalizedImageMime(reported: String?): String? = when (reported?.lowercase()) {
        "image/png" -> "image/png"
        "image/jpeg", "image/jpg" -> "image/jpeg"
        "image/gif" -> "image/gif"
        "image/bmp" -> "image/bmp"
        "image/webp" -> "image/webp"
        else -> null
    }

    private fun readClip(): Triple<String, ByteArray, String>? {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
        val clip = clipboard?.primaryClip
        if (clip == null || clip.itemCount == 0) return null
        val item = clip.getItemAt(0)
        val reportedMime = clip.description?.getMimeType(0)

        val imageMime = normalizedImageMime(reportedMime)
        val uri = item.uri
        if (imageMime != null && uri != null) {
            val bytes = try {
                contentResolver.openInputStream(uri)?.use { it.readBytes() }
            } catch (t: Throwable) {
                Log.e("CopyClip", "SendClip: failed reading image URI", t)
                null
            }
            if (bytes != null && bytes.isNotEmpty()) {
                return Triple(imageMime, bytes, "image (${bytes.size}B, $imageMime)")
            }
            // Fell through: a URI was present but unreadable. Do not silently degrade to
            // whatever coerceToText() makes of an image URI (a filename/placeholder string
            // that would land on every other clipboard as garbage); report empty instead.
            return null
        }

        val text = item.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) return null
        return Triple("text/plain", text.toByteArray(Charsets.UTF_8), "${text.length} chars")
    }

    private fun sendClip() {
        val appContext = applicationContext
        val settings = SettingsStore(this)
        val read = readClip()

        Log.i("CopyClip", "SendClip: clip=${read?.third ?: "empty/unreadable"} " +
            "hubHost='${settings.hubHost}' keyPresent=${settings.keyBytesOrNull() != null}")

        if (read == null) {
            statusView.text = "Clipboard is empty (or an unsupported image format)"
            finishDelayed()
            return
        }
        val (mime, data, _) = read
        val key = settings.keyBytesOrNull()
        if (key == null || settings.hubHost.isBlank()) {
            statusView.text = "Set shared key and hub address in the app first"
            finishDelayed()
            return
        }

        val host = settings.hubHost
        val port = settings.hubPushPort
        val deviceName = settings.deviceName
        val mainHandler = Handler(Looper.getMainLooper())
        Thread({
            val result = PushClient.push(
                host = host, port = port, hmacKey = key, deviceName = deviceName,
                mime = mime, sensitive = false, data = data,
            )
            mainHandler.post {
                val message = when (result) {
                    is PushClient.Result.Ok -> "Clip sent"
                    is PushClient.Result.Failed -> "Send failed: ${result.reason}"
                }
                statusView.text = message
                Toast.makeText(appContext, message, Toast.LENGTH_SHORT).show()
                finishDelayed()
            }
        }, "copyclip-send-clip").start()
    }

    private fun finishDelayed() {
        Handler(Looper.getMainLooper()).postDelayed({ finish() }, 600)
    }
}
