package com.mohitarora.copyclip.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val CopyClipDarkScheme = darkColorScheme(
    primary = BluePrimary,
    onPrimary = Color(0xFF0B1530),
    primaryContainer = BluePrimaryContainer,
    onPrimaryContainer = Color(0xFFD7E1FF),
    secondary = Teal,
    onSecondary = Color(0xFF07332C),
    secondaryContainer = TealContainer,
    onSecondaryContainer = Color(0xFFCFF5EC),
    background = Background,
    onBackground = OnSurface,
    surface = Surface,
    onSurface = OnSurface,
    surfaceVariant = SurfaceVariant,
    onSurfaceVariant = OnSurfaceMuted,
    outline = Outline,
    error = StatusError,
    onError = Color(0xFF3A0A12),
)

/** Dark-only, deliberately -- see Color.kt's note. isSystemInDarkTheme() is unused on purpose. */
@Composable
fun CopyClipTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = CopyClipDarkScheme,
        typography = CopyClipTypography,
        content = content,
    )
}
