package com.mohitarora.copyclip.accessibility

import android.accessibilityservice.AccessibilityService
import android.content.ComponentName
import android.provider.Settings
import android.view.accessibility.AccessibilityEvent

/**
 * Deliberately near-empty. Its only job is existing and being user-enabled -- Android exempts
 * an app with an active AccessibilityService from the Android-10+ background-clipboard-read
 * restriction, regardless of what the service actually does with events. Unlike Sefirah's own
 * accessibility service (root cause of this project's BUG-006 -- a spurious clipboard entry
 * traced to it reacting to a screenshot notification), this one does not react to anything,
 * which avoids that failure mode by construction rather than by luck. The actual clipboard
 * read/write happens elsewhere (SyncForegroundService, via plain ClipboardManager calls) once
 * this service being enabled has unlocked it -- see bugs/SPIKE-PIXEL-ACCESSIBILITY.md for
 * whether that causal claim actually holds, measured, not just cited from research.
 */
class ClipAccessibilityService : AccessibilityService() {
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        // Intentionally empty.
    }

    override fun onInterrupt() {
        // Intentionally empty.
    }

    companion object {
        fun isEnabled(context: android.content.Context): Boolean {
            val expected = ComponentName(context, ClipAccessibilityService::class.java)
            val enabledServices = Settings.Secure.getString(
                context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES
            ) ?: return false
            return enabledServices.split(':').any {
                ComponentName.unflattenFromString(it) == expected
            }
        }
    }
}
