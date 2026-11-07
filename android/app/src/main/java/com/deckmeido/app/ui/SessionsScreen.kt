package com.deckmeido.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.deckmeido.app.data.SessionItem

@Composable
fun SessionsScreen(vm: DeckMeidoViewModel) {
    ScreenScaffold(
        title = "会话",
        subtitle = connText(vm.connState) + " · 点会话直接进入聊天",
        actions = {
            TextButton(onClick = { vm.refreshSessions() }, enabled = !vm.sessionsLoading) {
                Text("刷新")
            }
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            Box(Modifier.weight(1f)) {
                when {
                    vm.sessionsLoading && vm.sessions.isEmpty() -> {
                        CircularProgressIndicator(Modifier.align(Alignment.Center))
                    }
                    vm.sessions.isEmpty() -> {
                        Text(
                            "还没有会话",
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
                            items(vm.sessions, key = { it.id }) { item ->
                                SessionRow(
                                    item = item,
                                    current = item.id == vm.currentSid,
                                    switching = vm.switchingSession && vm.currentSid != item.id,
                                    onClick = { vm.openSession(item.id) },
                                )
                            }
                        }
                    }
                }
                if (vm.connState is ConnState.Offline) {
                    Text(
                        "连接已断开，正在自动重连…",
                        style = MaterialTheme.typography.labelSmall,
                        color = MaterialTheme.colorScheme.error,
                        modifier = Modifier
                            .align(Alignment.BottomCenter)
                            .padding(16.dp),
                    )
                }
            }
        }
    }
}

@Composable
private fun SessionRow(
    item: SessionItem,
    current: Boolean,
    switching: Boolean,
    onClick: () -> Unit,
) {
    Card(
        Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
    ) {
        Row(
            Modifier.padding(14.dp),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(Modifier.weight(1f)) {
                Text(
                    item.title,
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
                Text(
                    "${item.messageCount} 条 · ${formatTime(item.updated)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (current) {
                Text(
                    "当前",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.secondary,
                )
                Box(Modifier.width(6.dp))
            }
            if (switching) {
                CircularProgressIndicator(Modifier.size(14.dp), strokeWidth = 1.5.dp)
                Box(Modifier.width(6.dp))
            }
            Text(
                "聊天 ›",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}
