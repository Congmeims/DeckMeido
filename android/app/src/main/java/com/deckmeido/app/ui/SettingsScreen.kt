package com.deckmeido.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp

/**
 * 设置页：只放连接、扫码配对、设备与关于四块。
 * 会话/文件/终端的操作入口都在各自的页签里，这里不重复。
 */
@Composable
fun SettingsScreen(vm: DeckMeidoViewModel) {
    var confirmForget by remember { mutableStateOf(false) }
    val context = LocalContext.current
    val appVersion = remember {
        runCatching {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        }.getOrNull() ?: "—"
    }

    ScreenScaffold(title = "设置", subtitle = "连接与设备") { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Card(Modifier.fillMaxWidth()) {
                Column(
                    Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    Text("连接", style = MaterialTheme.typography.titleSmall)
                    ConnectionLine(vm.connState)
                    OutlinedTextField(
                        value = vm.baseUrlInput,
                        onValueChange = { vm.baseUrlInput = it },
                        label = { Text("服务器地址") },
                        placeholder = { Text("192.168.1.20:18765") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(
                            keyboardType = KeyboardType.Uri,
                            imeAction = ImeAction.Done,
                        ),
                        keyboardActions = KeyboardActions(onDone = { vm.connect() }),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Button(
                        onClick = { vm.connect() },
                        enabled = !vm.connecting,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text(if (vm.connecting) "连接中…" else "连接 / 重连") }

                    OutlinedButton(
                        onClick = { vm.startScan() },
                        enabled = !vm.connecting,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("扫码配对 / 换一台后端") }
                    Text(
                        "二维码在掌机 http://127.0.0.1:18765/pair/qr 生成，带一次性短 token；"
                            + "扫码会自动填地址并换取设备凭据。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            Card(Modifier.fillMaxWidth()) {
                Column(
                    Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    Text("设备", style = MaterialTheme.typography.titleSmall)
                    val info = vm.authInfo
                    Text(
                        if (info?.authenticated == true) "本机已通过设备凭据配对"
                        else "本机还没有可用凭据",
                        style = MaterialTheme.typography.bodySmall,
                        color = if (info?.authenticated == true) {
                            MaterialTheme.colorScheme.secondary
                        } else {
                            MaterialTheme.colorScheme.error
                        },
                    )
                    Text(
                        "凭据保存在应用私有存储（配对 Cookie），与掌机 PIN 配对同权；"
                            + "清除后需要重新扫码或 PIN 配对。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        OutlinedButton(
                            onClick = { vm.disconnect() },
                            modifier = Modifier.weight(1f),
                        ) { Text("断开连接") }
                        OutlinedButton(
                            onClick = { confirmForget = true },
                            modifier = Modifier.weight(1f),
                        ) { Text("清除凭据") }
                    }
                }
            }

            Card(Modifier.fillMaxWidth()) {
                Column(
                    Modifier.padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    Text("关于", style = MaterialTheme.typography.titleSmall)
                    Text(
                        "DeckMeido Android $appVersion",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        "后端版本：${vm.authInfo?.version?.ifBlank { "未知" } ?: "未知"}",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(4.dp))
                    Text(
                        "远程只保证局域网 + Tailscale / WireGuard，不经过公网中转；"
                            + "所有请求都带设备凭据，回环地址除外。",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
    }

    if (confirmForget) {
        AlertDialog(
            onDismissRequest = { confirmForget = false },
            title = { Text("清除本机凭据？") },
            text = {
                Text(
                    "清除后本机需要重新配对；掌机设备表里的记录仍然保留，"
                        + "可以在掌机设置页单独吊销。",
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        confirmForget = false
                        vm.forgetDevice()
                    },
                ) { Text("清除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { confirmForget = false }) { Text("取消") }
            },
        )
    }
}
