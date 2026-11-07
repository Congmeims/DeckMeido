package com.deckmeido.app.ui

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

private val DeckColors = darkColorScheme(
    primary = Color(0xFFB79CFF),
    onPrimary = Color(0xFF241442),
    primaryContainer = Color(0xFF3A2A5C),
    onPrimaryContainer = Color(0xFFEDE3FF),
    secondary = Color(0xFF7FD8C8),
    onSecondary = Color(0xFF0B2B27),
    background = Color(0xFF0F0D14),
    onBackground = Color(0xFFE8E3F0),
    surface = Color(0xFF171420),
    onSurface = Color(0xFFE8E3F0),
    surfaceVariant = Color(0xFF232030),
    onSurfaceVariant = Color(0xFFB9B2C6),
    outline = Color(0xFF4A4259),
    error = Color(0xFFFF8A9B),
)

@Composable
fun DeckMeidoTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = DeckColors, content = content)
}
