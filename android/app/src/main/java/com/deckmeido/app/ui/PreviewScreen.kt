package com.deckmeido.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun PreviewScreen(vm: DeckMeidoViewModel) {
    var showConfirm by remember { mutableStateOf(false) }
    val preview = vm.preview

    ScreenScaffold(
        title = preview?.title ?: "会话历史",
        subtitle = "只读预览 · 不会切换其他设备",
        onBack = { vm.back() },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            Box(Modifier.weight(1f)) {
                when {
                    vm.previewLoading && preview == null -> {
                        CircularProgressIndicator(Modifier.align(Alignment.Center))
                    }
                    preview == null -> {
                        Text(
                            "没有读到历史",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.align(Alignment.Center),
                        )
                    }
                    preview.messages.isEmpty() -> {
                        Text(
                            "这个会话还没有对话",
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            modifier = Modifier.align(Alignment.Center),
                        )
                    }
                    else -> {
                        LazyColumn(
                            Modifier.fillMaxSize(),
                            contentPadding = PaddingValues(16.dp),
                            verticalArrangement = Arrangement.spacedBy(10.dp),
                        ) {
                            items(preview.messages) { message ->
                                MessageBubble(message)
                            }
                        }
                    }
                }
            }
            Column(Modifier.padding(16.dp)) {
                if (vm.running) {
                    Text(
                        "有一轮回答正在生成，结束后才能切换会话",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
                Button(
                    onClick = { showConfirm = true },
                    enabled = preview != null && !vm.running && !vm.previewLoading,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text("切换到此会话并进入聊天")
                }
            }
        }
    }

    if (showConfirm) {
        AlertDialog(
            onDismissRequest = { showConfirm = false },
            title = { Text("切换当前会话？") },
            text = {
                Text("切换是全局动作，掌机和其他已连接设备都会跟着切到「${preview?.title ?: "这个会话"}」。")
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        showConfirm = false
                        vm.switchToPreviewSession()
                    },
                ) { Text("切换") }
            },
            dismissButton = {
                TextButton(onClick = { showConfirm = false }) { Text("取消") }
            },
        )
    }
}
