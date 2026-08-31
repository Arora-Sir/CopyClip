package com.mohitarora.copyclip.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Warning
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mohitarora.copyclip.service.SyncState
import com.mohitarora.copyclip.ui.theme.StatusConnected
import com.mohitarora.copyclip.ui.theme.StatusError
import com.mohitarora.copyclip.ui.theme.StatusWaiting

private sealed class Status(val label: String, val color: @Composable () -> Color) {
    object Connected : Status("Connected", { StatusConnected })
    object Waiting : Status("Waiting for a device", { StatusWaiting })
    object NotReady : Status("Not set up", { StatusError })
}

private fun statusOf(state: SyncState): Status = when {
    !state.shizukuAlive || !state.permissionGranted -> Status.NotReady
    state.bridgeStatus.startsWith("client") -> Status.Connected
    state.bridgeRunning -> Status.Waiting
    else -> Status.NotReady
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MainScreen(
    state: SyncState,
    keyText: String,
    portText: String,
    hubHostText: String,
    deviceNameText: String,
    onKeyChange: (String) -> Unit,
    onPortChange: (String) -> Unit,
    onHubHostChange: (String) -> Unit,
    onDeviceNameChange: (String) -> Unit,
    onSave: () -> Unit,
    onGrantShizuku: () -> Unit,
    onSendClip: () -> Unit,
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("CopyClip", fontWeight = FontWeight.SemiBold) },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                ),
            )
        },
        containerColor = MaterialTheme.colorScheme.background,
    ) { padding ->
        Column(
            modifier = Modifier
                .padding(padding)
                .padding(horizontal = 20.dp)
                .fillMaxWidth()
                .verticalScroll(rememberScrollState()),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(4.dp))
            DeviceStatusCard(state)
            ShizukuCard(state, onGrantShizuku)
            SendClipCard(onSendClip)
            SettingsCard(
                keyText, portText, hubHostText, deviceNameText,
                onKeyChange, onPortChange, onHubHostChange, onDeviceNameChange, onSave,
            )
            Spacer(Modifier.height(24.dp))
        }
    }
}

@Composable
private fun DeviceStatusCard(state: SyncState) {
    val status = statusOf(state)
    Card(
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(
            modifier = Modifier.padding(20.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                modifier = Modifier
                    .size(48.dp)
                    .background(MaterialTheme.colorScheme.primaryContainer, CircleShape),
                contentAlignment = Alignment.Center,
            ) {
                Box(
                    modifier = Modifier
                        .size(16.dp)
                        .background(status.color(), CircleShape),
                )
            }
            Column(modifier = Modifier.padding(start = 16.dp)) {
                Text(
                    text = "This device",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
                Text(
                    text = status.label,
                    style = MaterialTheme.typography.bodyMedium,
                    color = status.color(),
                )
                Text(
                    text = "port ${state.port} · ${state.bridgeStatus}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun ShizukuCard(state: SyncState, onGrantShizuku: () -> Unit) {
    if (state.shizukuAlive && state.permissionGranted) return
    Card(
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Filled.Warning,
                    contentDescription = null,
                    tint = StatusWaiting,
                    modifier = Modifier.size(20.dp),
                )
                Text(
                    text = if (!state.shizukuAlive) "Shizuku is not running"
                    else "Shizuku access not granted",
                    style = MaterialTheme.typography.titleMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                    modifier = Modifier.padding(start = 10.dp),
                )
            }
            Text(
                text = if (!state.shizukuAlive)
                    "Open the Shizuku app and start it (ADB pairing or root), then come back."
                else "CopyClip needs Shizuku's privileged access to read and write the clipboard in the background.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )
            if (state.shizukuAlive && !state.permissionGranted) {
                Button(
                    onClick = onGrantShizuku,
                    modifier = Modifier.padding(top = 12.dp),
                ) { Text("Grant Shizuku access") }
            }
        }
    }
}

@Composable
private fun SendClipCard(onSendClip: () -> Unit) {
    Card(
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Text(
                "Send Clip",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                "Manual one-tap push of this device's current clipboard to the laptop " +
                    "and the rest of the mesh. Works even when automatic background sync " +
                    "can't (e.g. this device can't hold Shizuku without root) -- also " +
                    "available from the persistent notification. Requires the hub " +
                    "address below to be set.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
            )
            Button(onClick = onSendClip, modifier = Modifier.fillMaxWidth()) {
                Text("Send Clip Now")
            }
        }
    }
}

@Composable
private fun SettingsCard(
    keyText: String,
    portText: String,
    hubHostText: String,
    deviceNameText: String,
    onKeyChange: (String) -> Unit,
    onPortChange: (String) -> Unit,
    onHubHostChange: (String) -> Unit,
    onDeviceNameChange: (String) -> Unit,
    onSave: () -> Unit,
) {
    Card(
        shape = MaterialTheme.shapes.large,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(modifier = Modifier.padding(20.dp)) {
            Text(
                "Pairing",
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                "Paste the same key from the laptop's copyclip.key file.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 4.dp, bottom = 12.dp),
            )
            OutlinedTextField(
                value = keyText,
                onValueChange = onKeyChange,
                label = { Text("Shared key (hex)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = portText,
                onValueChange = onPortChange,
                label = { Text("Port (this device listens on)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = hubHostText,
                onValueChange = onHubHostChange,
                label = { Text("Hub address (laptop Tailscale IP, for Send Clip)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(12.dp))
            OutlinedTextField(
                value = deviceNameText,
                onValueChange = onDeviceNameChange,
                label = { Text("Device name (must match config.json)") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(16.dp))
            OutlinedButton(onClick = onSave, modifier = Modifier.fillMaxWidth()) {
                Text("Save")
            }
        }
    }
}
