package com.mohitarora.copyclip

import android.app.Activity
import android.content.ClipboardManager
import android.content.ContentUris
import android.content.Context
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.util.Log
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import com.mohitarora.copyclip.shizuku.PushClient
import java.io.File
import java.io.FileInputStream

/**
 * Launched from the "Send Clip" notification action or the in-app button.
 *
 * Implements two distinct detection layers:
 * 1. Fresh Screenshot Detection: Neither Stock Android (Pixel 1) nor Samsung One UI
 *    copies screenshots to the system clipboard automatically upon capture. This activity
 *    first checks MediaStore and disk for a screenshot captured within the last 90 seconds.
 * 2. System Clipboard Fallback: If no recent screenshot exists, it reads the current
 *    system clipboard (text or image) after onWindowFocusChanged(true).
 *
 * UI Design follows modern HUD design tokens:
 * - Centered elevated slate card (#181A1F) with subtle 1dp border (#2C3038) and 20dp corners
 * - Clean typographic hierarchy (title, bold headline, detail subtitle)
 * - Animated spinner during transmission, transitioning to emerald success or rose error
 */
class SendClipActivity : Activity() {
    private var handled = false
    private lateinit var titleView: TextView
    private lateinit var statusView: TextView
    private lateinit var detailView: TextView
    private lateinit var spinner: ProgressBar

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Root container with dark scrim overlay
        val rootLayout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setBackgroundColor(Color.parseColor("#99000000"))
            setPadding(dpToPx(24), dpToPx(24), dpToPx(24), dpToPx(24))
        }

        // Sleek elevated surface card
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(dpToPx(24), dpToPx(24), dpToPx(24), dpToPx(24))

            val bg = GradientDrawable().apply {
                setColor(Color.parseColor("#181A1F"))
                cornerRadius = dpToPx(20).toFloat()
                setStroke(dpToPx(1), Color.parseColor("#2C3038"))
            }
            background = bg
        }

        titleView = TextView(this).apply {
            text = "COPYCLIP"
            textSize = 12f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#3B82F6"))
            letterSpacing = 0.15f
            gravity = Gravity.CENTER
        }

        statusView = TextView(this).apply {
            text = "Preparing clip..."
            textSize = 17f
            typeface = Typeface.DEFAULT_BOLD
            setTextColor(Color.parseColor("#F3F4F6"))
            gravity = Gravity.CENTER
            setPadding(0, dpToPx(10), 0, dpToPx(4))
        }

        detailView = TextView(this).apply {
            text = "Checking system clipboard and screenshots"
            textSize = 13f
            setTextColor(Color.parseColor("#9CA3AF"))
            gravity = Gravity.CENTER
            setPadding(0, 0, 0, dpToPx(16))
        }

        spinner = ProgressBar(this).apply {
            isIndeterminate = true
        }

        card.addView(titleView)
        card.addView(statusView)
        card.addView(detailView)
        card.addView(spinner)

        rootLayout.addView(card)
        setContentView(rootLayout)
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (!hasFocus || handled) return
        handled = true
        sendClip()
    }

    private fun dpToPx(dp: Int): Int =
        TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, dp.toFloat(), resources.displayMetrics).toInt()

    private fun normalizedImageMime(reported: String?): String? = when (reported?.lowercase()) {
        "image/png" -> "image/png"
        "image/jpeg", "image/jpg" -> "image/jpeg"
        "image/gif" -> "image/gif"
        "image/bmp" -> "image/bmp"
        "image/webp" -> "image/webp"
        else -> null
    }

    /**
     * Checks if a screenshot was captured on the device within the last 90 seconds.
     * Tries MediaStore first; falls back to direct filesystem scan on screenshot directories.
     */
    private fun findRecentScreenshot(): Triple<String, ByteArray, String>? {
        // 1. MediaStore query
        try {
            val projection = arrayOf(
                MediaStore.Images.Media._ID,
                MediaStore.Images.Media.DISPLAY_NAME,
                MediaStore.Images.Media.DATE_ADDED,
                MediaStore.Images.Media.MIME_TYPE
            )
            val sortOrder = "${MediaStore.Images.Media.DATE_ADDED} DESC"
            contentResolver.query(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                projection,
                null,
                null,
                sortOrder
            )?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameIdx = cursor.getColumnIndex(MediaStore.Images.Media.DISPLAY_NAME)
                    val dateIdx = cursor.getColumnIndex(MediaStore.Images.Media.DATE_ADDED)
                    val mimeIdx = cursor.getColumnIndex(MediaStore.Images.Media.MIME_TYPE)
                    val idIdx = cursor.getColumnIndex(MediaStore.Images.Media._ID)

                    val name = if (nameIdx >= 0) cursor.getString(nameIdx) else ""
                    val dateAddedSec = if (dateIdx >= 0) cursor.getLong(dateIdx) else 0L
                    val mime = if (mimeIdx >= 0) cursor.getString(mimeIdx) else "image/png"
                    val id = if (idIdx >= 0) cursor.getLong(idIdx) else -1L

                    val isScreenshot = name.contains("Screenshot", ignoreCase = true) ||
                                       name.contains("Screen_Capture", ignoreCase = true)
                    val nowSec = System.currentTimeMillis() / 1000
                    if (isScreenshot && (nowSec - dateAddedSec) in 0..90 && id >= 0) {
                        val uri = ContentUris.withAppendedId(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, id)
                        val bytes = contentResolver.openInputStream(uri)?.use { it.readBytes() }
                        if (bytes != null && bytes.isNotEmpty()) {
                            val normMime = normalizedImageMime(mime) ?: "image/png"
                            val sizeKb = (bytes.size + 1023) / 1024
                            return Triple(normMime, bytes, "screenshot ($name, ${sizeKb} KB)")
                        }
                    }
                }
            }
        } catch (t: Throwable) {
            Log.d("CopyClip", "SendClip: MediaStore lookup bypassed: ${t.message}")
        }

        // 2. Direct filesystem fallback
        val candidateDirs = arrayOf(
            File("/sdcard/Pictures/Screenshots"),
            File("/sdcard/DCIM/Screenshots")
        )
        for (dir in candidateDirs) {
            if (!dir.exists() || !dir.isDirectory) continue
            val files = dir.listFiles { f ->
                f.isFile && (f.name.contains("Screenshot", ignoreCase = true) || f.name.contains("Screen_Capture", ignoreCase = true))
            } ?: continue

            var newestFile: File? = null
            var newestTime = 0L
            for (f in files) {
                val mod = f.lastModified()
                if (mod > newestTime) {
                    newestTime = mod
                    newestFile = f
                }
            }
            if (newestFile != null) {
                val ageMs = System.currentTimeMillis() - newestTime
                if (ageMs in 0..90_000) {
                    try {
                        val bytes = FileInputStream(newestFile).use { it.readBytes() }
                        if (bytes.isNotEmpty()) {
                            val mime = if (newestFile.name.endsWith(".jpg", ignoreCase = true) ||
                                           newestFile.name.endsWith(".jpeg", ignoreCase = true)) {
                                "image/jpeg"
                            } else {
                                "image/png"
                            }
                            val sizeKb = (bytes.size + 1023) / 1024
                            return Triple(mime, bytes, "screenshot (${newestFile.name}, ${sizeKb} KB)")
                        }
                    } catch (t: Throwable) {
                        Log.d("CopyClip", "SendClip: direct file read bypassed: ${t.message}")
                    }
                }
            }
        }
        return null
    }

    private fun readClip(): Triple<String, ByteArray, String>? {
        // Priority 1: Fresh screenshot captured in the last 90 seconds
        val recentScreenshot = findRecentScreenshot()
        if (recentScreenshot != null) {
            return recentScreenshot
        }

        // Priority 2: System clipboard (text or image)
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
                val sizeKb = (bytes.size + 1023) / 1024
                return Triple(imageMime, bytes, "image ($sizeKb KB, $imageMime)")
            }
            return null
        }

        val text = item.coerceToText(this)?.toString()
        if (text.isNullOrEmpty()) return null
        return Triple("text/plain", text.toByteArray(Charsets.UTF_8), "${text.length} chars text")
    }

    private fun sendClip() {
        val appContext = applicationContext
        val settings = SettingsStore(this)
        val read = readClip()

        Log.i("CopyClip", "SendClip: clip=${read?.third ?: "empty/unreadable"} " +
            "hubHost='${settings.hubHost}' keyPresent=${settings.keyBytesOrNull() != null}")

        if (read == null) {
            spinner.visibility = View.GONE
            statusView.text = "Nothing to send"
            detailView.text = "No recent screenshot or readable clipboard content"
            finishDelayed(1400)
            return
        }

        val (mime, data, description) = read
        val isImage = mime.startsWith("image/")
        statusView.text = if (isImage) "Sending screenshot..." else "Sending clipboard..."
        detailView.text = description

        val key = settings.keyBytesOrNull()
        if (key == null || settings.hubHost.isBlank()) {
            spinner.visibility = View.GONE
            statusView.text = "Configuration required"
            detailView.text = "Set shared key and hub address in settings"
            finishDelayed(1500)
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
                spinner.visibility = View.GONE
                when (result) {
                    is PushClient.Result.Ok -> {
                        statusView.text = "Synced successfully"
                        statusView.setTextColor(Color.parseColor("#10B981"))
                        detailView.text = "Delivered $description to hub"
                        Toast.makeText(appContext, "Clip sent ($description)", Toast.LENGTH_SHORT).show()
                    }
                    is PushClient.Result.Failed -> {
                        statusView.text = "Send failed"
                        statusView.setTextColor(Color.parseColor("#EF4444"))
                        detailView.text = result.reason
                        Toast.makeText(appContext, "Send failed: ${result.reason}", Toast.LENGTH_LONG).show()
                    }
                }
                finishDelayed(900)
            }
        }, "copyclip-send-clip").start()
    }

    private fun finishDelayed(delayMs: Long = 800) {
        Handler(Looper.getMainLooper()).postDelayed({ finish() }, delayMs)
    }
}
