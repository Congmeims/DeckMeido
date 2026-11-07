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
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp

@Composable
fun ConnectScreen(vm: DeckMeidoViewModel) {
    Column(
        Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 24.dp, vertical = 20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Spacer(Modifier.height(28.dp))
        Text(
            "DeckMeido",
            style = MaterialTheme.typography.displaySmall,
            color = MaterialTheme.colorScheme.primary,
        )
        Text(
            "掌机控制台 · Android P0",
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))

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
        ) {
            Text(if (vm.connecting) "连接中…" else "连接")
        }

        ConnectionLine(vm.connState)

        val info = vm.authInfo
        if (info != null && info.authenticated) {
            Text(
                "已通过设备凭据登录" + if (info.version.isNotBlank()) " · 后端 ${info.version}" else "",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.secondary,
            )
        } else if (info != null) {
            if (info.lanEnable) {
                PairSection(vm)
            } else {
                Text(
                    "后端没有开启局域网访问：请在掌机设置里打开后重试。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        } else {
            Text(
                "先在掌机打开 DeckMeido，再填它的局域网地址（Tailscale / WireGuard 的内网地址也可以）。首次连接需要配对。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        Spacer(Modifier.height(24.dp))
        Text(
            "P0 只做连接配对、会话只读预览、发消息与流式回复；语音、图片、Markdown 表格在后续阶段。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun PairSection(vm: DeckMeidoViewModel) {
    val session = vm.pairing
    if (session == null) {
        Card(Modifier.fillMaxWidth()) {
            Column(
                Modifier.padding(16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                Text("这台手机还没有配对", style = MaterialTheme.typography.titleSmall)
                Text(
                    "点下面的按钮后，掌机会弹出待配对设备；在掌机设置页点「允许」，再把掌机显示的四位 PIN 填回来。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Button(onClick = { vm.beginPairing() }, enabled = !vm.pairingBusy) {
                    Text(if (vm.pairingBusy) "请求中…" else "开始配对")
                }
            }
        }
        return
    }

    Card(Modifier.fillMaxWidth()) {
        Column(
            Modifier.padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("配对请求", style = MaterialTheme.typography.titleSmall)
                Text(
                    "剩余 ${session.expiresIn}s",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                "${session.name.ifBlank { "Android 手机" }} · ${session.ip}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                pairStateText(session.state),
                style = MaterialTheme.typography.bodyMedium,
                color = if (session.state == "approved") {
                    MaterialTheme.colorScheme.secondary
                } else {
                    MaterialTheme.colorScheme.onSurface
                },
            )
            if (session.attempts > 0) {
                Text(
                    "已尝试 ${session.attempts} 次（最多 3 次，错满作废）",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }

            OutlinedTextField(
                value = vm.pinInput,
                onValueChange = { value ->
                    vm.pinInput = value.filter { it.isDigit() }.take(4)
                },
                label = { Text("掌机显示的四位 PIN") },
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                modifier = Modifier.fillMaxWidth(),
            )

            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(
                    checked = vm.rememberDevice,
                    onCheckedChange = { vm.rememberDevice = it },
                )
                Text("保持登录 30 天", style = MaterialTheme.typography.bodySmall)
            }

            Button(
                onClick = { vm.confirmPairing() },
                enabled = !vm.pairingBusy &&
                    session.state == "approved" &&
                    vm.pinInput.length == 4,
                modifier = Modifier.fillMaxWidth(),
            ) {
                Text(if (vm.pairingBusy) "确认中…" else "确认配对")
            }

            TextButton(
                onClick = {
                    vm.cancelPairing()
                    vm.beginPairing()
                },
                enabled = !vm.pairingBusy,
            ) {
                Text("重新发起配对")
            }
        }
    }
}

private fun pairStateText(state: String): String = when (state) {
    "pending" -> "等待掌机允许…"
    "approved" -> "掌机已允许，请把 PIN 填到上面"
    "denied" -> "掌机拒绝了这次配对，请重新发起"
    "expired" -> "配对已过期，请重新发起"
    "failed" -> "PIN 输错次数过多，本次配对作废，请重新发起"
    "paired" -> "已配对"
    else -> state
}
