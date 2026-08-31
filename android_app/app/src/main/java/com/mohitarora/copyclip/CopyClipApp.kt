package com.mohitarora.copyclip

import android.app.Application
import android.util.Log
import com.mohitarora.copyclip.shizuku.ShizukuManager

class CopyClipApp : Application() {
    override fun onCreate() {
        super.onCreate()
        // Wired here, once, regardless of which Activity is on screen -- this is what lets a
        // Shizuku restart while the app is already running self-heal without reopening it,
        // per android-mastery's documented restart-auto-reconnect pattern.
        ShizukuManager.init {
            Log.i("CopyClip", "Shizuku binder changed, alive=${ShizukuManager.isBinderAlive()}")
        }
    }
}
