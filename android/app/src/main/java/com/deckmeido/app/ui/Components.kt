package com.deckmeido.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.deckmeido.app.data.ChatMessage
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ScreenScaffold(
    title: String,
    subtitle: String? = null,
    onBack: (() -> Unit)? = null,
    actions: @Composable RowScope.() -> Unit = {},
    bottomBar: @Composable () -> Unit = {},
    modifier: Modifier = Modifier,
    content: @Composable (PaddingValues) -> Unit,
) {
    Scaffold(
        modifier = modifier,
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(title, style = MaterialTheme.typography.titleMedium)
                        if (subtitle != null) {
                            Text(
                                subtitle,
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                },
                navigationIcon = {
                    if (onBack != null) {
                        TextButton(onClick = onBack) { Text("返回") }
                    }
                },
                actions = actions,
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.surface,
                    titleContentColor = MaterialTheme.colorScheme.onSurface,
                ),
            )
        },
        bottomBar = bottomBar,
        content = content,
    )
}

@Composable
fun StatusDot(state: ConnState) {
    Box(
        Modifier
            .size(8.dp)
            .background(connColor(state), CircleShape),
    )
}

@Composable
fun ConnectionLine(state: ConnState) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(8.dp)) {
        StatusDot(state)
        Text(
            connText(state),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

fun connColor(state: ConnState): Color = when (state) {
    is ConnState.Online -> Color(0xFF7FD8C8)
    is ConnState.Connecting -> Color(0xFFFFC66D)
    is ConnState.Offline -> Color(0xFFFF8A9B)
    else -> Color(0xFF8E879C)
}

fun connText(state: ConnState): String = when (state) {
    is ConnState.Online -> "已连接"
    is ConnState.Connecting -> "连接中…"
    is ConnState.Offline -> "离线：${state.reason}"
    else -> "未连接"
}

@Composable
fun MessageBubble(message: ChatMessage, onSpeak: (() -> Unit)? = null) {
    if (message.role == "system") {
        Text(
            message.content,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
            modifier = Modifier
                .fillMaxWidth()
                .padding(vertical = 4.dp),
        )
        return
    }
    val isUser = message.role == "user"
    Box(
        Modifier.fillMaxWidth(),
        contentAlignment = if (isUser) Alignment.CenterEnd else Alignment.CenterStart,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = if (isUser) {
                MaterialTheme.colorScheme.primaryContainer
            } else {
                MaterialTheme.colorScheme.surfaceVariant
            },
            modifier = Modifier.widthIn(max = 340.dp),
        ) {
            Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                if (!isUser) {
                    Text(
                        "DeckMeido",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.secondary,
                    )
                }
                Text(
                    message.content,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (isUser) {
                        MaterialTheme.colorScheme.onPrimaryContainer
                    } else {
                        MaterialTheme.colorScheme.onSurface
                    },
                )
                if (!isUser && onSpeak != null) {
                    Row(
                        Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.End,
                    ) {
                        TextButton(
                            onClick = onSpeak,
                            contentPadding = PaddingValues(horizontal = 8.dp),
                        ) {
                            Text("播报", style = MaterialTheme.typography.labelSmall)
                        }
                    }
                }
            }
        }
    }
}

fun formatTime(epochSeconds: Double): String {
    if (epochSeconds <= 0.0) return ""
    val format = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())
    return format.format(Date((epochSeconds * 1000).toLong()))
}

fun formatTime(epochSeconds: Long): String = formatTime(epochSeconds.toDouble())

/** 文件大小：与网页端 fbFmtSize 同一套单位，避免两端数字看起来对不上。 */
fun formatBytes(bytes: Long?): String {
    if (bytes == null) return "—"
    if (bytes < 1024) return "${bytes}B"
    if (bytes < 1024 * 1024) return "${bytes / 1024}K"
    if (bytes < 1024L * 1024 * 1024) {
        return String.format(Locale.US, "%.1fM", bytes / 1048576.0)
    }
    return String.format(Locale.US, "%.1fG", bytes / 1073741824.0)
}

/** 修改时间：后端没给（旧版本 / stat 失败）时显示 —，不要留空。 */
fun formatMtime(epochSeconds: Double?): String {
    if (epochSeconds == null || epochSeconds <= 0.0) return "—"
    return formatTime(epochSeconds)
}
