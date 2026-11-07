package com.deckmeido.app.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay

@Composable
fun ChatScreen(vm: DeckMeidoViewModel) {
    val listState = rememberLazyListState()
    val title = vm.sessions.firstOrNull { it.id == vm.currentSid }?.title
        ?: if (vm.currentSid.isNotBlank()) "会话 ${vm.currentSid.take(6)}" else "当前会话"

    ScreenScaffold(
        title = title,
        subtitle = connText(vm.connState),
        modifier = Modifier.imePadding(),
        actions = {
            if (vm.running || vm.awaitingReply || vm.queuedCount > 0) {
                TextButton(onClick = { vm.stop() }, enabled = !vm.stopping) {
                    Text("停止")
                }
            }
        },
        bottomBar = { ChatBottomBar(vm) },
    ) { padding ->
        LazyColumn(
            state = listState,
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
            contentPadding = PaddingValues(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            items(vm.messages) { message ->
                MessageBubble(message, onSpeak = { vm.speak(message.content) })
            }
            if (vm.thoughtText.isNotBlank()) {
                item("thought") { ThoughtBlock(vm.thoughtText) }
            }
            if (vm.toolNote.isNotBlank()) {
                item("tool") { ToolBlock(vm.toolNote) }
            }
            if (vm.streamingText.isNotEmpty()) {
                item("stream") { StreamingBubble(vm.streamingText) }
            }
            if ((vm.running || vm.awaitingReply) &&
                vm.streamingText.isEmpty() &&
                vm.thoughtText.isBlank() &&
                vm.toolNote.isBlank()
            ) {
                item("waiting") { WaitingRow(vm.runningSince) }
            }
            if (vm.askQuestion != null) {
                item("ask") { AskCard(vm) }
            }
        }
    }

    // 新消息或新片段出现时贴到底部
    LaunchedEffect(Unit) {
        snapshotFlow { listState.layoutInfo.totalItemsCount }.collect { total ->
            if (total > 0) listState.scrollToItem(total - 1)
        }
    }
    LaunchedEffect(vm.streamingText.length) {
        val total = listState.layoutInfo.totalItemsCount
        if (total > 0) listState.scrollToItem(total - 1)
    }
}

@Composable
private fun ChatBottomBar(vm: DeckMeidoViewModel) {
    val context = LocalContext.current
    val micLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        if (granted) vm.startRecording() else vm.onMicPermissionDenied()
    }
    val onMicClick = {
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) vm.startRecording() else micLauncher.launch(Manifest.permission.RECORD_AUDIO)
    }

    Surface(color = MaterialTheme.colorScheme.surface) {
        Column {
            if (vm.ttsLoading || vm.ttsActive) {
                SpeakBar(vm)
            }
            if (vm.queuedCount > 0) {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 16.dp, vertical = 2.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        "已排队 ${vm.queuedCount} 条，等这一轮结束后自动发送",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.secondary,
                    )
                    Spacer(Modifier.weight(1f))
                    TextButton(onClick = { vm.clearQueue() }) { Text("清空") }
                }
            }
            if (vm.recording) {
                RecordBar(vm)
            } else {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    verticalAlignment = Alignment.Bottom,
                ) {
                    TextButton(
                        onClick = onMicClick,
                        enabled = !vm.sttBusy,
                    ) {
                        Text(if (vm.sttBusy) "识别中…" else "语音")
                    }
                    OutlinedTextField(
                        value = vm.inputText,
                        onValueChange = { vm.inputText = it },
                        modifier = Modifier.weight(1f),
                        placeholder = {
                            Text(
                                if (vm.running || vm.awaitingReply) {
                                    "本轮进行中，发送会排队"
                                } else {
                                    "说点什么…"
                                },
                            )
                        },
                        maxLines = 4,
                    )
                    Spacer(Modifier.width(8.dp))
                    Button(
                        onClick = { vm.send() },
                        enabled = vm.inputText.isNotBlank(),
                    ) {
                        Text("发送")
                    }
                }
            }
        }
    }
}

/** 录音中：显示秒数与上限，完成即上传 STT 并填进输入框（不自动发送）。 */
@Composable
private fun RecordBar(vm: DeckMeidoViewModel) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
        Spacer(Modifier.width(10.dp))
        Text(
            "录音中 ${vm.recordSeconds}s / 60s",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.secondary,
        )
        Spacer(Modifier.weight(1f))
        TextButton(onClick = { vm.cancelRecording() }) { Text("取消") }
        Button(onClick = { vm.finishRecording() }) { Text("完成") }
    }
}

/** 播报控制条：合成中 / 播放中 / 暂停中。 */
@Composable
private fun SpeakBar(vm: DeckMeidoViewModel) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            when {
                vm.ttsLoading -> "正在合成语音…"
                vm.ttsPaused -> "播报已暂停"
                else -> "正在播报…"
            },
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.secondary,
        )
        Spacer(Modifier.weight(1f))
        if (!vm.ttsLoading) {
            TextButton(onClick = { vm.toggleSpeaking() }) {
                Text(if (vm.ttsPaused) "继续" else "暂停")
            }
            TextButton(onClick = { vm.stopSpeaking() }) { Text("停止") }
        }
    }
}

@Composable
private fun StreamingBubble(text: String) {
    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterStart) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            modifier = Modifier.widthIn(max = 340.dp),
        ) {
            Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp)) {
                Text(
                    "DeckMeido · 生成中",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.secondary,
                )
                Text(
                    "$text▍",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurface,
                )
            }
        }
    }
}

@Composable
private fun ThoughtBlock(text: String) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.5f),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(horizontal = 12.dp, vertical = 8.dp)) {
            Text(
                "思考过程",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                text,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 8,
                overflow = TextOverflow.Ellipsis,
            )
        }
    }
}

@Composable
private fun ToolBlock(text: String) {
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
        Text(
            text,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun WaitingRow(runningSince: Double) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(runningSince) {
        while (true) {
            delay(1000)
            now = System.currentTimeMillis()
        }
    }
    val seconds = if (runningSince > 0.0) {
        ((now / 1000.0) - runningSince).toInt().coerceAtLeast(0)
    } else {
        0
    }
    Row(
        Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 2.dp)
        Text(
            if (seconds > 0) "思考中… 已等待 ${seconds}s" else "思考中…",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun AskCard(vm: DeckMeidoViewModel) {
    val question = vm.askQuestion ?: return
    Card(Modifier.fillMaxWidth()) {
        Column(
            Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                "需要你的回答",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.secondary,
            )
            Text(question, style = MaterialTheme.typography.bodyMedium)
            vm.askOptions.forEach { option ->
                TextButton(
                    onClick = { vm.answerAsk(option) },
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(option)
                }
            }
        }
    }
}
