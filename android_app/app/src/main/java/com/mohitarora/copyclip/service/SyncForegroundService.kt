package com.mohitarora.copyclip.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import com.mohitarora.copyclip.MainActivity
import com.mohitarora.copyclip.SendClipActivity
import com.mohitarora.copyclip.SettingsStore
import com.mohitarora.copyclip.accessibility.ClipAccessibilityService
import com.mohitarora.copyclip.shizuku.ShizukuManager
import java.util.concurrent.atomic.AtomicInteger

data class SyncState(
    val shizukuAlive: Boolean = false,
    val permissionGranted: Boolean = false,
    val bridgeRunning: Boolean = false,
    val bridgeStatus: String = "not started",
    val port: Int = SettingsStore.DEFAULT_PORT,
    val accessibilityEnabled: Boolean = false,
    val accessibilityClipEvents: Int = 0,
)

/**
 * Foreground service, started at boot (see BootReceiver) or from MainActivity. Owns the one
 * long-lived Shizuku UserService binding and its bridge, and republishes state via a
 * StateFlow the UI observes -- kept as a companion-object singleton flow rather than a bound
 * service interface, since there is exactly one of these per device and the UI only ever
 * needs to *observe*, never call into it directly (all actual commands go through
 * ShizukuManager, which owns the AIDL binder).
 *
 * Android 12+'s background-foreground-service-start restriction (android-mastery's
 * documented `ForegroundServiceStartNotAllowedException`) does not apply to the boot path
 * specifically: responding to RECEIVE_BOOT_COMPLETED is one of the platform's own documented
 * exemptions. It would apply to a hypothetical "restart myself from a plain background
 * timer," which this service does not do.
 */
class SyncForegroundService : Service() {

    companion object {
        private const val CHANNEL_ID = "copyclip_sync"
        private const val NOTIFICATION_ID = 1
        private const val POLL_MS = 4000L

        private val _state = MutableStateFlow(SyncState())
        val state: StateFlow<SyncState> = _state.asStateFlow()
    }

    private lateinit var settings: SettingsStore
    private var pollThread: Thread? = null
    @Volatile private var running = false
    private val accessibilityClipEvents = AtomicInteger(0)
    // Sticky once set: retrying startBridge() against the same root-mode Shizuku would just
    // repeat the same refusal forever. Cleared only by a fresh app process (e.g. after the
    // user actually reconfigures Shizuku and reopens the app), not by this loop itself.
    @Volatile private var rootRefusalReason: String? = null

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        createChannel()
        registerAccessibilityClipListener()
    }

    /**
     * The pixel-tier spike, and (once confirmed) the real read path: a completely ordinary
     * ClipboardManager listener. Nothing here is privileged -- the privilege, if it exists,
     * comes entirely from ClipAccessibilityService being enabled, which is what
     * bugs/SPIKE-PIXEL-ACCESSIBILITY.md exists to actually measure rather than assume. This
     * listener is registered unconditionally (harmless on phone/tab too, it just won't fire
     * in the background there without their own accessibility grant), so the exact same build
     * runs on every device.
     */
    private fun registerAccessibilityClipListener() {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            ?: return
        clipboard.addPrimaryClipChangedListener {
            val count = accessibilityClipEvents.incrementAndGet()
            Log.i("CopyClip", "accessibility-path clip change observed (#$count)")
            publishState()
        }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startForegroundCompat()
        if (pollThread == null) {
            running = true
            pollThread = Thread({ runLoop() }, "copyclip-sync-service").apply {
                isDaemon = true
                start()
            }
        }
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    // -- main loop: boot-race-safe binder wait, bind once, poll status forever -------------

    private fun runLoop() {
        // android-mastery's documented boot-startup race: this service can start before
        // Shizuku's own binder is ready. Poll rather than assume.
        val deadline = System.currentTimeMillis() + 30_000L
        while (running && !ShizukuManager.isBinderAlive() && System.currentTimeMillis() < deadline) {
            Thread.sleep(500)
        }

        ShizukuManager.init { publishState() }

        var bound = false
        while (running) {
            publishState()
            if (!bound && ShizukuManager.hasPermission() && rootRefusalReason == null) {
                val key = settings.keyBytesOrNull()
                if (key != null) {
                    bound = true
                    ShizukuManager.bind(packageName) { service ->
                        if (service != null) {
                            try {
                                val dexBytes = assets.open("clipbridge.dex").readBytes()
                                val started = service.startBridge(settings.port, key, dexBytes)
                                if (!started) {
                                    // Covers the root-UID refusal (ClipBridgeService's own
                                    // requireShellUid() guard) and any other startup failure.
                                    // getBridgeStatus() still works here -- the process is
                                    // deliberately still alive to answer this, see
                                    // ClipBridgeService.kt's own comment on why.
                                    val reason = service.bridgeStatus
                                    if (reason.startsWith("refused:")) {
                                        rootRefusalReason = reason
                                        Log.e("CopyClip", "stopping: $reason")
                                    }
                                    service.destroy()
                                    bound = false
                                }
                            } catch (t: Throwable) {
                                bound = false
                            }
                        } else {
                            bound = false
                        }
                        publishState()
                    }
                }
            }
            Thread.sleep(POLL_MS)
        }
    }

    private fun publishState() {
        val status = rootRefusalReason ?: try {
            ShizukuManager.service?.bridgeStatus ?: "not started"
        } catch (t: Throwable) {
            "error: ${t.message}"
        }
        _state.value = SyncState(
            shizukuAlive = ShizukuManager.isBinderAlive(),
            permissionGranted = ShizukuManager.hasPermission(),
            bridgeRunning = status.startsWith("listening") || status.startsWith("client"),
            bridgeStatus = status,
            port = settings.port,
            accessibilityEnabled = ClipAccessibilityService.isEnabled(this),
            accessibilityClipEvents = accessibilityClipEvents.get(),
        )
        updateNotification(status)
    }

    // -- notification -----------------------------------------------------------------------

    private fun createChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID, "CopyClip sync", NotificationManager.IMPORTANCE_LOW
        ).apply { description = "Shows whether clipboard sync is connected" }
        mgr.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        // Manual fallback for devices background sync can't reach (see SendClipActivity's
        // own doc comment) -- always shown, not just when the bridge is down, so it works
        // as a one-tap "just in case" on any device, not only pixel.
        val sendClipIntent = PendingIntent.getActivity(
            this, 1, Intent(this, SendClipActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("CopyClip")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_menu_share)
            .setContentIntent(openIntent)
            .addAction(android.R.drawable.ic_menu_share, "Send Clip", sendClipIntent)
            .setOngoing(true)
            .setPriority(NotificationCompat.PRIORITY_LOW)
            .build()
    }

    private fun startForegroundCompat() {
        val notification = buildNotification("Starting...")
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(
                NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
            )
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
    }

    private fun updateNotification(status: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIFICATION_ID, buildNotification(status))
    }
}
