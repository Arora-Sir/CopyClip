package com.mohitarora.copyclip.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import androidx.core.content.ContextCompat
import com.mohitarora.copyclip.SettingsStore

/**
 * RECEIVE_BOOT_COMPLETED is one of Android's own documented exemptions from the Android 12+
 * background-foreground-service-start restriction (android-mastery's
 * ForegroundServiceStartNotAllowedException note) -- this is the one path that does not need
 * any Shizuku-privileged workaround to legally call startForegroundService().
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!SettingsStore(context).autoStartEnabled) return
        ContextCompat.startForegroundService(
            context, Intent(context, SyncForegroundService::class.java)
        )
    }
}
