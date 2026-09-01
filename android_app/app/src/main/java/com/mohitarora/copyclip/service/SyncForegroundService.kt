package com.mohitarora.copyclip.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
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
import com.mohitarora.copyclip.shizuku.ShizukuManager

data class SyncState(
    val shizukuAlive: Boolean = false,
    val permissionGranted: Boolean = false,
    val bridgeRunning: Boolean = false,
    val bridgeStatus: String = "not started",
    val port: Int = SettingsStore.DEFAULT_PORT,
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
        // A new id, not a renamed "copyclip_sync": once a channel exists on-device, Android
        // ignores any importance change from createNotificationChannel() on a later app
        // update, by design, the user is assumed to have already made their own choice for
        // that channel id. A new id is the only way code can actually raise it. The old
        // "copyclip_sync" channel is left orphaned on any device that already had it,
        // harmless, just an unused entry in Settings.
        private const val CHANNEL_ID = "copyclip_sync_v2"
        private const val NOTIFICATION_ID = 1
        private const val POLL_MS = 4000L

        private val _state = MutableStateFlow(SyncState())
        val state: StateFlow<SyncState> = _state.asStateFlow()
    }

    private lateinit var settings: SettingsStore
    private var pollThread: Thread? = null
    @Volatile private var running = false
    // Sticky once set: retrying startBridge() against the same root-mode Shizuku would just
    // repeat the same refusal forever. Cleared only by a fresh app process (e.g. after the
    // user actually reconfigures Shizuku and reopens the app), not by this loop itself.
    @Volatile private var rootRefusalReason: String? = null

    override fun onCreate() {
        super.onCreate()
        settings = SettingsStore(this)
        createChannel()
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
        )
        updateNotification(status)
    }

    // -- notification -----------------------------------------------------------------------

    private fun createChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        // DEFAULT, not LOW: LOW is what made this collapse into the shade's quiet/minimized
        // section, needing a swipe to reveal the Send Clip action at all. DEFAULT sorts
        // higher and shows expanded (actions visible) without that swipe.
        // Deliberately not HIGH/MAX: those add a heads-up popup banner on every post, and
        // this notification updates on every poll tick (every few seconds, see POLL_MS) plus
        // every state change, a banner interrupting the screen that often would make the
        // phone unusable for anything else. DEFAULT plus setOnlyAlertOnce (below) is the
        // actual fix for "hard to find", HIGH/MAX solves a different problem this doesn't have.
        val channel = NotificationChannel(
            CHANNEL_ID, "CopyClip sync", NotificationManager.IMPORTANCE_DEFAULT
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
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            // This updates on every poll tick, not just real changes -- without this, a
            // channel that already had sound/vibration configured by the user would replay
            // it that often, which is a worse annoyance than the swipe-to-see-Send-Clip
            // problem this whole change exists to fix.
            .setOnlyAlertOnce(true)
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
