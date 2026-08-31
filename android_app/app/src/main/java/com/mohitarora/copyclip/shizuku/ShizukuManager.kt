package com.mohitarora.copyclip.shizuku

import android.content.ComponentName
import android.content.ServiceConnection
import android.content.pm.PackageManager
import android.os.IBinder
import com.mohitarora.copyclip.IUserService
import rikka.shizuku.Shizuku

/**
 * Wraps Shizuku's static API with two production gotchas the android-mastery skill documents
 * from real prior app-building experience (not from Shizuku's own official docs):
 *
 * 1. Handshake race: Shizuku.pingBinder() can return true before the async client-
 *    registration handshake finishes; calling checkSelfPermission()/requestPermission() in
 *    that window throws IllegalStateException("Not an attached client"). Every call below
 *    that touches the binder is wrapped in catch(Throwable), not a narrower exception type.
 * 2. Restart auto-reconnect: addBinderReceivedListenerSticky/addBinderDeadListener, wired
 *    once from CopyClipApp, so a Shizuku restart while this app is already running self-heals
 *    without the user reopening it.
 *
 * A third gotcha (the boot-startup race, polling pingBinder() before binding) applies once
 * this app has its own boot-triggered service -- not yet built in this spike milestone, so not
 * implemented here yet either.
 */
object ShizukuManager {
    private const val REQUEST_CODE = 1001

    var service: IUserService? = null
        private set

    @Volatile private var initialized = false

    /** Idempotent: CopyClipApp and SyncForegroundService both call this in the same process. */
    fun init(onBinderChange: () -> Unit) {
        if (initialized) return
        initialized = true
        Shizuku.addBinderReceivedListenerSticky { onBinderChange() }
        Shizuku.addBinderDeadListener {
            service = null
            onBinderChange()
        }
    }

    fun isBinderAlive(): Boolean = try {
        Shizuku.pingBinder()
    } catch (t: Throwable) {
        false
    }

    fun hasPermission(): Boolean = try {
        Shizuku.pingBinder() && Shizuku.checkSelfPermission() == PackageManager.PERMISSION_GRANTED
    } catch (t: Throwable) {
        false
    }

    /** Fire-and-forget; the result (if any) arrives via Shizuku.OnRequestPermissionResultListener. */
    fun requestPermission() {
        try {
            val alive = Shizuku.pingBinder()
            val already = hasPermission()
            android.util.Log.i("CopyClip", "requestPermission: pingBinder=$alive hasPermission=$already")
            if (alive && !already) {
                Shizuku.requestPermission(REQUEST_CODE)
                android.util.Log.i("CopyClip", "requestPermission: Shizuku.requestPermission() called")
            }
        } catch (t: Throwable) {
            android.util.Log.w("CopyClip", "requestPermission threw", t)
        }
    }

    fun bind(appId: String, onResult: (IUserService?) -> Unit) {
        val args = Shizuku.UserServiceArgs(ComponentName(appId, ClipBridgeService::class.java.name))
            .daemon(false)
            .processNameSuffix("service")
            .debuggable(true)
            .version(1)

        val conn = object : ServiceConnection {
            override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
                service = if (binder != null && binder.pingBinder()) {
                    IUserService.Stub.asInterface(binder)
                } else null
                onResult(service)
            }

            override fun onServiceDisconnected(name: ComponentName?) {
                service = null
                onResult(null)
            }
        }
        Shizuku.bindUserService(args, conn)
    }
}
