package com.mohitarora.copyclip

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.mohitarora.copyclip.service.SyncForegroundService
import com.mohitarora.copyclip.shizuku.ShizukuManager
import com.mohitarora.copyclip.ui.MainScreen
import com.mohitarora.copyclip.ui.theme.CopyClipTheme
import rikka.shizuku.Shizuku

class MainActivity : ComponentActivity() {

    private lateinit var settings: SettingsStore

    private val permissionListener =
        Shizuku.OnRequestPermissionResultListener { _, _ -> }

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = SettingsStore(this)
        Shizuku.addRequestPermissionResultListener(permissionListener)
        requestNotificationPermissionIfNeeded()
        startForegroundServiceCompat()

        setContent {
            CopyClipTheme {
                var keyText by remember { mutableStateOf(settings.hmacKeyHex) }
                var portText by remember { mutableStateOf(settings.port.toString()) }
                var hubHostText by remember { mutableStateOf(settings.hubHost) }
                var deviceNameText by remember { mutableStateOf(settings.deviceName) }
                val state by SyncForegroundService.state.collectAsState()

                MainScreen(
                    state = state,
                    keyText = keyText,
                    portText = portText,
                    hubHostText = hubHostText,
                    deviceNameText = deviceNameText,
                    onKeyChange = { keyText = it },
                    onPortChange = { portText = it },
                    onHubHostChange = { hubHostText = it },
                    onDeviceNameChange = { deviceNameText = it },
                    onSave = {
                        settings.hmacKeyHex = keyText
                        portText.toIntOrNull()?.let { settings.port = it }
                        settings.hubHost = hubHostText
                        deviceNameText.takeIf { it.isNotBlank() }?.let { settings.deviceName = it }
                        startForegroundServiceCompat()
                    },
                    onGrantShizuku = { ShizukuManager.requestPermission() },
                    onSendClip = { startActivity(Intent(this, SendClipActivity::class.java)) },
                )
            }
        }
    }

    override fun onDestroy() {
        Shizuku.removeRequestPermissionResultListener(permissionListener)
        super.onDestroy()
    }

    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    private fun startForegroundServiceCompat() {
        ContextCompat.startForegroundService(this, Intent(this, SyncForegroundService::class.java))
    }
}
