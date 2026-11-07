package com.deckmeido.app.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.deckmeido.app.data.TerminalSessionInfo

/** 与文件页分区同一套状态色：绿=正常 / 黄=过渡 / 红=结束。 */
private val TERM_OK = Color(0xFF5FD08A)
private val TERM_WAIT = Color(0xFFE0A33A)
private val TERM_BAD = Color(0xFFFF7A7A)

/**
 * 终端页：把后端远程 shell（docs/remote-terminal-api.md）接到手机上。
 *
 * - 开关关闭（403 remote_shell_disabled）显示空状态，不当网络错误；
 * - 会话列表 → 会话内等宽滚动区 + 命令输入 + 终止；
 * - 输出按块追加，WS 断线用 REST 续读，不整屏重画。
 *
 * 手机端**没有**自己起 shell 的入口，全部走后端既有接口。
 */
@Composable
fun TerminalScreen(vm: DeckMeidoViewModel) {
    TermConfirmDialog(vm)
    val active = vm.termActive
    ScreenScaffold(
        title = "终端",
        subtitle = when {
            active != null -> active.cwd.ifBlank { "远程 shell" }
            vm.termEnabled == false -> "远程终端已关闭"
            else -> "掌机上的远程 shell"
        },
        actions = {
            if (active != null) {
                TextButton(onClick = { vm.backToTerminalList() }) { Text("列表") }
                TextButton(
                    onClick = { vm.terminateTerminal() },
                    enabled = !vm.termStopping && !vm.termExited,
                ) {
                    Text(
                        if (vm.termStopping) "终止中…" else "终止",
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            } else {
                TextButton(onClick = { vm.refreshTerminal() }, enabled = !vm.termLoading) {
                    Text("刷新")
                }
            }
        },
        bottomBar = { if (active != null) TermCommandBar(vm, active) },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            when {
                vm.termEnabled == false -> TermDisabled(vm)
                active != null -> TermSession(vm, active)
                else -> TermSessionList(vm)
            }
        }
    }
}

// ---------- 开关关闭 ----------

@Composable
private fun TermDisabled(vm: DeckMeidoViewModel) {
    Column(
        Modifier
            .fillMaxSize()
            .padding(28.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text("远程终端已在掌机设置里关闭", style = MaterialTheme.typography.titleMedium)
        Spacer(Modifier.heightIn(min = 12.dp))
        Text(
            "在掌机「设置 → 远程终端」里打开开关，\n" +
                "或把 config.toml 的 [terminal] remote_shell 改成 true，然后回到这里点重新检查。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.heightIn(min = 16.dp))
        OutlinedButton(onClick = { vm.refreshTerminal() }, enabled = !vm.termLoading) {
            Text(if (vm.termLoading) "检查中…" else "重新检查")
        }
    }
}

// ---------- 会话列表 ----------

@Composable
private fun TermSessionList(vm: DeckMeidoViewModel) {
    val settings = vm.termSettings
    val maxSessions = settings?.maxSessions?.takeIf { it > 0 } ?: 3
    Column(Modifier.fillMaxSize()) {
        vm.termError?.let { err ->
            TermNoticeCard(text = err, danger = true)
        }
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 12.dp, vertical = 8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    "会话 ${vm.termSessions.count { it.alive }}/$maxSessions",
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (settings != null) {
                    Text(
                        "空闲 ${settings.idleTimeout} 秒回收 · 单会话输出上限 ${formatBytes(settings.maxOutputBytes)}",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            TextButton(onClick = { vm.createTerminalSession() }, enabled = !vm.termBusy) {
                Text(if (vm.termBusy) "新建中…" else "新建会话")
            }
        }
        Box(
            Modifier
                .fillMaxWidth()
                .weight(1f),
        ) {
            when {
                vm.termLoading && vm.termSessions.isEmpty() -> {
                    CircularProgressIndicator(
                        Modifier
                            .align(Alignment.Center)
                            .size(28.dp),
                    )
                }

                vm.termSessions.isEmpty() -> {
                    Text(
                        "还没有终端会话\n点右上角「新建会话」在掌机上开一个 shell",
                        Modifier
                            .align(Alignment.Center)
                            .padding(24.dp),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        textAlign = TextAlign.Center,
                    )
                }

                else -> LazyColumn(Modifier.fillMaxSize()) {
                    items(vm.termSessions, key = { it.id }) { session ->
                        TermSessionRow(session) { vm.openTerminalSession(session) }
                    }
                }
            }
        }
    }
}

@Composable
private fun TermSessionRow(session: TerminalSessionInfo, onOpen: () -> Unit) {
    val color = when {
        session.alive -> TERM_OK
        else -> TERM_BAD
    }
    Surface(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp)
            .clickable(onClick = onOpen),
        color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.3f),
        shape = RoundedCornerShape(10.dp),
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(
                    Modifier
                        .size(7.dp)
                        .background(color, CircleShape),
                )
                Spacer(Modifier.width(6.dp))
                Text(
                    "会话 ${session.id.take(8)}",
                    style = MaterialTheme.typography.labelLarge,
                )
                Spacer(Modifier.weight(1f))
                Text(
                    termStateText(session.state),
                    style = MaterialTheme.typography.labelSmall,
                    color = color,
                )
            }
            Text(
                session.cwd.ifBlank { "家目录" },
                Modifier.padding(top = 6.dp),
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                buildString {
                    append("PID ${session.pid}")
                    val exit = session.exitCode ?: session.lastExitCode
                    append(" · 退出码 ${exit ?: "—"}")
                    append(" · 输出 ${formatBytes(session.outputTotal)}")
                },
                Modifier.padding(top = 2.dp),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

// ---------- 会话内 ----------

@Composable
private fun TermSession(vm: DeckMeidoViewModel, session: TerminalSessionInfo) {
    Column(Modifier.fillMaxSize()) {
        TermStatusLine(vm, session)
        Box(
            Modifier
                .fillMaxWidth()
                .weight(1f)
                .background(MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.22f)),
        ) {
            TermOutput(vm)
        }
        if (vm.termTruncated) {
            TermNoticeCard(
                text = "输出超过后端单会话上限，较早内容已被丢弃；更早内容需从掌机端查看。",
                danger = false,
            )
        }
        if (vm.termError != null) {
            TermNoticeCard(text = vm.termError!!, danger = true)
        }
    }
}

@Composable
private fun TermStatusLine(vm: DeckMeidoViewModel, session: TerminalSessionInfo) {
    val exit = session.exitCode ?: session.lastExitCode
    val color = when {
        vm.termExited -> TERM_BAD
        vm.termLive -> TERM_OK
        else -> TERM_WAIT
    }
    val text = when {
        vm.termExited -> "已结束（${termStateText(session.state)}）"
        vm.termLive -> "实时输出（WS）"
        else -> "WS 已断，正在重连 / 用 REST 续读"
    }
    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(
            Modifier
                .size(7.dp)
                .background(color, CircleShape),
        )
        Spacer(Modifier.width(6.dp))
        Text(text, style = MaterialTheme.typography.labelSmall, color = color)
        Spacer(Modifier.weight(1f))
        Text(
            "退出码 ${exit ?: "—"} · ${session.cols}×${session.rows}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** 等宽滚动区：新输出自动贴底，用户上滑后不再抢滚动；长按可选中复制。 */
@Composable
private fun TermOutput(vm: DeckMeidoViewModel) {
    val vScroll = rememberScrollState()
    val hScroll = rememberScrollState()
    var stick by remember { mutableStateOf(true) }

    LaunchedEffect(vScroll) {
        snapshotFlow { vScroll.maxValue - vScroll.value }.collect { distance ->
            if (vScroll.maxValue > 0) stick = distance < 48
        }
    }
    // maxValue 变化本身就代表文本重新排版完成，这时再贴底不会落到旧的最大值
    LaunchedEffect(vScroll.maxValue, vm.termOutput.length) {
        if (stick) vScroll.scrollTo(vScroll.maxValue)
    }

    if (vm.termOutput.isEmpty()) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Text(
                if (vm.termExited) "这个会话没有留下输出" else "会话已就绪，在下面输入命令后回车",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }
    Box(
        Modifier
            .fillMaxSize()
            .verticalScroll(vScroll)
            .padding(horizontal = 10.dp, vertical = 8.dp),
    ) {
        SelectionContainer {
            Text(
                vm.termOutput,
                Modifier.horizontalScroll(hScroll),
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                fontSize = 12.sp,
                lineHeight = 16.sp,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
    }
}

/** 底部命令行：回车发送；最近命令点一下复用；Ctrl+C / Tab 走原始输入。 */
@Composable
private fun TermCommandBar(vm: DeckMeidoViewModel, session: TerminalSessionInfo) {
    Surface(color = MaterialTheme.colorScheme.surface) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 8.dp, vertical = 6.dp),
        ) {
            if (vm.termHistory.isNotEmpty() && !vm.termExited) {
                Row(
                    Modifier
                        .fillMaxWidth()
                        .horizontalScroll(rememberScrollState()),
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                ) {
                    vm.termHistory.take(6).forEach { command ->
                        TextButton(
                            onClick = { vm.useTermHistory(command) },
                            contentPadding = androidx.compose.foundation.layout.PaddingValues(
                                horizontal = 8.dp,
                                vertical = 0.dp,
                            ),
                        ) {
                            Text(
                                command,
                                style = MaterialTheme.typography.labelSmall,
                                fontFamily = FontFamily.Monospace,
                                maxLines = 1,
                            )
                        }
                    }
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = vm.termCommand,
                    onValueChange = { vm.termCommand = it },
                    modifier = Modifier.weight(1f),
                    placeholder = { Text("输入命令，回车发送") },
                    textStyle = MaterialTheme.typography.bodySmall.copy(
                        fontFamily = FontFamily.Monospace,
                    ),
                    singleLine = true,
                    enabled = !vm.termExited && session.alive,
                    keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                    keyboardActions = KeyboardActions(onSend = { vm.sendTermCommand() }),
                )
                TextButton(
                    onClick = { vm.sendTermCommand() },
                    enabled = !vm.termBusy && !vm.termExited && vm.termCommand.isNotBlank(),
                ) { Text(if (vm.termBusy) "发送中" else "发送") }
            }
            Row(
                Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                TextButton(
                    onClick = { vm.sendTermInput("\u0003") },
                    enabled = !vm.termExited,
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(
                        horizontal = 8.dp,
                        vertical = 0.dp,
                    ),
                ) { Text("Ctrl+C", style = MaterialTheme.typography.labelSmall) }
                TextButton(
                    onClick = { vm.sendTermInput("\t") },
                    enabled = !vm.termExited,
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(
                        horizontal = 8.dp,
                        vertical = 0.dp,
                    ),
                ) { Text("Tab", style = MaterialTheme.typography.labelSmall) }
                Spacer(Modifier.weight(1f))
                if (vm.termExited) {
                    Text(
                        "会话已结束",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
            }
        }
    }
}

// ---------- 二次确认 ----------

@Composable
private fun TermConfirmDialog(vm: DeckMeidoViewModel) {
    val prompt = vm.termConfirm ?: return
    AlertDialog(
        onDismissRequest = { vm.dismissTermConfirm() },
        title = { Text("危险命令，需要确认") },
        text = {
            Column {
                Text(prompt.reason, style = MaterialTheme.typography.bodyMedium)
                Surface(
                    Modifier
                        .padding(top = 10.dp)
                        .fillMaxWidth()
                        .heightIn(max = 180.dp)
                        .verticalScroll(rememberScrollState()),
                    color = MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.4f),
                    shape = RoundedCornerShape(8.dp),
                ) {
                    Text(
                        prompt.command,
                        Modifier.padding(10.dp),
                        style = MaterialTheme.typography.bodySmall,
                        fontFamily = FontFamily.Monospace,
                    )
                }
                val meta = buildList {
                    if (prompt.level.isNotBlank()) add("级别 ${prompt.level}")
                    if (prompt.rule.isNotBlank()) add("规则 ${prompt.rule}")
                }
                if (meta.isNotEmpty()) {
                    Text(
                        meta.joinToString(" · "),
                        Modifier.padding(top = 8.dp),
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Text(
                    "确认后会用上面的命令原文带 confirm=true 重新提交一次，不会自动重试。",
                    Modifier.padding(top = 6.dp),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { vm.confirmTermCommand() }) {
                Text("确认执行", color = MaterialTheme.colorScheme.error)
            }
        },
        dismissButton = {
            TextButton(onClick = { vm.dismissTermConfirm() }) { Text("取消") }
        },
    )
}

// ---------- 小部件 ----------

@Composable
private fun TermNoticeCard(text: String, danger: Boolean) {
    Surface(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 4.dp),
        color = if (danger) {
            MaterialTheme.colorScheme.errorContainer.copy(alpha = 0.4f)
        } else {
            MaterialTheme.colorScheme.surfaceVariant.copy(alpha = 0.35f)
        },
        shape = RoundedCornerShape(8.dp),
    ) {
        Text(
            text,
            Modifier.padding(10.dp),
            style = MaterialTheme.typography.bodySmall,
            color = if (danger) {
                MaterialTheme.colorScheme.error
            } else {
                MaterialTheme.colorScheme.onSurfaceVariant
            },
        )
    }
}

private fun termStateText(state: String): String = when (state) {
    "running" -> "运行中"
    "exited" -> "已退出"
    "terminated" -> "已终止"
    "timeout" -> "空闲回收"
    "shutdown" -> "后端关闭"
    "disabled" -> "开关关闭"
    else -> state.ifBlank { "未知" }
}
