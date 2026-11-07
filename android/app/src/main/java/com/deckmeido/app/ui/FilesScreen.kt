package com.deckmeido.app.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.deckmeido.app.data.FsEntry

/** 与网页端一致的四种分区配色（见 index.html 的 fb-zone）。 */
private val ZONE_WORKSPACE = Color(0xFF5FD08A)
private val ZONE_CAUTION = Color(0xFFE0A33A)
private val ZONE_DANGER = Color(0xFFFF7A7A)
private val ZONE_FORBIDDEN = Color(0xFFE45050)

@Composable
fun FilesScreen(vm: DeckMeidoViewModel) {
    var renameTarget by remember { mutableStateOf<FsEntry?>(null) }
    var deleteTarget by remember { mutableStateOf<FsEntry?>(null) }
    var renameText by remember { mutableStateOf("") }

    val uploadLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocument(),
    ) { uri -> uri?.let { vm.uploadSelection(it) } }

    ScreenScaffold(
        title = "文件",
        subtitle = "当前目录：${vm.fsPath.ifBlank { "工作区" }}",
        actions = {
            TextButton(
                onClick = { uploadLauncher.launch(arrayOf("*/*")) },
                enabled = !vm.fsOpBusy && !vm.fsLoading,
            ) { Text("上传") }
            TextButton(onClick = { vm.loadFiles(vm.fsPath) }, enabled = !vm.fsLoading) {
                Text("刷新")
            }
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding),
        ) {
            val listing = vm.fsListing
            PathBar(
                path = listing?.path ?: vm.fsPath,
                onNavigate = { vm.loadFiles(it) },
            )
            if (listing != null) {
                ZoneRow(listing)
            }
            vm.fsError?.let { err ->
                NoticeCard(text = err, danger = true)
            }
            // 目录不存在 / 断链软链接等：后端 error 原文直接显示
            if (listing != null && listing.zone != "forbidden" && listing.error != null) {
                NoticeCard(text = listing.error, danger = true)
            }

            HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
            Box(
                Modifier
                    .fillMaxWidth()
                    .weight(1f),
            ) {
                when {
                    vm.fsLoading && listing == null -> {
                        CircularProgressIndicator(
                            Modifier
                                .align(Alignment.Center)
                                .size(28.dp),
                        )
                    }

                    listing == null -> {
                        Text(
                            "还没有目录内容",
                            Modifier
                                .align(Alignment.Center)
                                .padding(24.dp),
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }

                    // 禁止浏览：只提示，不列条目（/proc、/sys、/dev）
                    listing.zone == "forbidden" -> {
                        NoticeCard(
                            text = listing.error ?: "该区域禁止浏览",
                            danger = true,
                            modifier = Modifier.padding(12.dp),
                        )
                    }

                    else -> {
                        val entries = listing.entries
                        if (entries.isEmpty() && !vm.fsLoading && listing.error == null) {
                            Text(
                                "此目录为空",
                                Modifier
                                    .align(Alignment.Center)
                                    .padding(24.dp),
                                style = MaterialTheme.typography.bodyMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        } else {
                            LazyColumn(Modifier.fillMaxSize()) {
                                items(entries, key = { it.name }) { entry ->
                                    FsRow(
                                        entry = entry,
                                        busy = vm.fsOpBusy,
                                        onEnter = { vm.enterFsEntry(entry) },
                                        onDownload = { vm.downloadEntry(entry) },
                                        onRename = {
                                            renameText = entry.name
                                            renameTarget = entry
                                        },
                                        onDelete = { deleteTarget = entry },
                                    )
                                    HorizontalDivider(
                                        color = MaterialTheme.colorScheme.outlineVariant.copy(alpha = 0.4f),
                                    )
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    renameTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { renameTarget = null },
            title = { Text("重命名") },
            text = {
                OutlinedTextField(
                    value = renameText,
                    onValueChange = { renameText = it },
                    singleLine = true,
                    label = { Text("新名称") },
                )
            },
            confirmButton = {
                TextButton(
                    enabled = renameText.trim().isNotEmpty() && renameText.trim() != target.name,
                    onClick = {
                        vm.renameEntry(target, renameText)
                        renameTarget = null
                    },
                ) { Text("确定") }
            },
            dismissButton = {
                TextButton(onClick = { renameTarget = null }) { Text("取消") }
            },
        )
    }

    deleteTarget?.let { target ->
        AlertDialog(
            onDismissRequest = { deleteTarget = null },
            title = { Text("删除") },
            text = {
                Text(
                    "删除「${target.name}」后不可恢复。\n" +
                        if (target.isDir) "目录必须为空才能删除。" else "",
                )
            },
            confirmButton = {
                TextButton(
                    onClick = {
                        vm.deleteEntry(target)
                        deleteTarget = null
                    },
                ) { Text("删除", color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = {
                TextButton(onClick = { deleteTarget = null }) { Text("取消") }
            },
        )
    }
}

/** 当前绝对路径：分段可点回退；横屏/深路径用横向滚动，不挤成省略号。 */
@Composable
private fun PathBar(path: String, onNavigate: (String) -> Unit) {
    val segments = remember(path) { pathSegments(path) }
    Row(
        Modifier
            .fillMaxWidth()
            .horizontalScroll(rememberScrollState())
            .padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        segments.forEachIndexed { index, seg ->
            if (index > 0) {
                Text(
                    "›",
                    Modifier.padding(horizontal = 2.dp),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                seg.second,
                Modifier
                    .clickable(enabled = !seg.first.isNullOrBlank() || index == 0) {
                        seg.first?.let(onNavigate)
                    }
                    .padding(horizontal = 4.dp, vertical = 4.dp),
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                color = if (index == segments.lastIndex) {
                    MaterialTheme.colorScheme.onBackground
                } else {
                    MaterialTheme.colorScheme.primary
                },
                maxLines = 1,
            )
        }
    }
}

/**
 * 把绝对路径拆成可点的段。兼容两种后端：
 * Linux `/home/deck/x` 与 Windows 开发机 `M:/SDagent/x`。
 */
private fun pathSegments(path: String): List<Pair<String?, String>> {
    val raw = path.trim()
    if (raw.isEmpty()) return listOf(null to "工作区")
    val win = Regex("^[A-Za-z]:/").find(raw)
    val rootTarget: String?
    val rootLabel: String
    val rest: List<String>
    if (win != null) {
        rootTarget = raw.substring(0, 2) + "/"
        rootLabel = rootTarget
        rest = raw.removePrefix(rootTarget).trim('/').split('/').filter { it.isNotBlank() }
    } else if (raw.startsWith("/")) {
        rootTarget = "/"
        rootLabel = "/"
        rest = raw.trim('/').split('/').filter { it.isNotBlank() }
    } else {
        rootTarget = null
        rootLabel = raw
        rest = emptyList()
    }
    if (rest.isEmpty()) return listOf(rootTarget to rootLabel)
    val out = ArrayList<Pair<String?, String>>()
    out.add(rootTarget to rootLabel)
    var acc = rootTarget ?: ""
    for (seg in rest) {
        acc = if (acc.endsWith("/")) acc + seg else "$acc/$seg"
        out.add(acc to seg)
    }
    return out
}

/** 分区胶囊 + 只读挂载说明；forbidden 与 danger 用网页端同一套红黄绿。 */
@Composable
private fun ZoneRow(listing: com.deckmeido.app.data.FsListing) {
    val color = when (listing.zone) {
        "workspace" -> ZONE_WORKSPACE
        "caution" -> ZONE_CAUTION
        "forbidden" -> ZONE_FORBIDDEN
        else -> ZONE_DANGER
    }
    var label = listing.zoneLabel.ifBlank {
        when (listing.zone) {
            "workspace" -> "工作区域"
            "caution" -> "谨慎操作"
            "forbidden" -> "禁止浏览"
            else -> "危险区域 · 只读"
        }
    }
    if (listing.readonly && !label.contains("只读")) {
        label += " · 只读"
    }
    Column(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 2.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Surface(
                color = color.copy(alpha = 0.12f),
                shape = RoundedCornerShape(50),
                border = androidx.compose.foundation.BorderStroke(1.dp, color.copy(alpha = 0.55f)),
            ) {
                Row(
                    Modifier.padding(horizontal = 10.dp, vertical = 4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Box(
                        Modifier
                            .size(6.dp)
                            .background(color, RoundedCornerShape(50)),
                    )
                    Spacer(Modifier.width(6.dp))
                    Text(label, color = color, style = MaterialTheme.typography.labelMedium)
                }
            }
            if (listing.readonly) {
                Spacer(Modifier.width(8.dp))
                Text(
                    if (listing.zone == "caution") "只读挂载" else "写操作会被后端拒绝",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        if (listing.zoneReason.isNotBlank()) {
            Text(
                listing.zoneReason,
                Modifier.padding(top = 4.dp),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        if (listing.linkBroken) {
            Text(
                "断链软链接：${listing.linkTarget}",
                Modifier.padding(top = 4.dp),
                style = MaterialTheme.typography.labelSmall,
                color = ZONE_DANGER,
            )
        }
    }
}

@Composable
private fun FsRow(
    entry: FsEntry,
    busy: Boolean,
    onEnter: () -> Unit,
    onDownload: () -> Unit,
    onRename: () -> Unit,
    onDelete: () -> Unit,
) {
    var menuOpen by remember(entry.name) { mutableStateOf(false) }
    Row(
        Modifier
            .fillMaxWidth()
            .clickable { if (entry.isDir) onEnter() else onDownload() }
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        FsTypeIcon(entry)
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(
                entry.name + if (entry.isDir) "/" else "",
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                buildString {
                    append(typeLabel(entry))
                    if (!entry.isDir) append(" · ").append(formatBytes(entry.size))
                    append(" · ").append(formatMtime(entry.mtime))
                    if (entry.isLink) append(" · 链接")
                },
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (entry.broken) {
                Text(
                    "断链软链接 → ${entry.linkTarget}",
                    style = MaterialTheme.typography.labelSmall,
                    color = ZONE_DANGER,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
        Box {
            TextButton(onClick = { menuOpen = true }, enabled = !busy) {
                Text("⋮", fontSize = 18.sp)
            }
            DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                if (!entry.isDir) {
                    DropdownMenuItem(
                        text = { Text("下载") },
                        onClick = {
                            menuOpen = false
                            onDownload()
                        },
                    )
                }
                DropdownMenuItem(
                    text = { Text("重命名") },
                    onClick = {
                        menuOpen = false
                        onRename()
                    },
                )
                DropdownMenuItem(
                    text = { Text("删除") },
                    onClick = {
                        menuOpen = false
                        onDelete()
                    },
                )
            }
        }
    }
}

/** 目录 / 文件 / 图片… 的小图标：Canvas 画，不额外引图标库。 */
@Composable
private fun FsTypeIcon(entry: FsEntry) {
    val tint = when {
        entry.broken -> ZONE_DANGER
        entry.isDir -> ZONE_WORKSPACE
        else -> MaterialTheme.colorScheme.primary
    }
    Canvas(Modifier.size(28.dp)) {
        val w = size.width
        val h = size.height
        if (entry.isDir) {
            drawRoundRect(
                tint,
                topLeft = Offset(w * 0.05f, h * 0.20f),
                size = Size(w * 0.42f, h * 0.16f),
                cornerRadius = CornerRadius(w * 0.06f),
            )
            drawRoundRect(
                tint.copy(alpha = 0.85f),
                topLeft = Offset(w * 0.05f, h * 0.30f),
                size = Size(w * 0.90f, h * 0.50f),
                cornerRadius = CornerRadius(w * 0.10f),
                style = Stroke(1.4.dp.toPx()),
            )
        } else {
            drawRoundRect(
                tint,
                topLeft = Offset(w * 0.18f, h * 0.06f),
                size = Size(w * 0.64f, h * 0.88f),
                cornerRadius = CornerRadius(w * 0.10f),
                style = Stroke(1.4.dp.toPx()),
            )
            drawLine(
                tint,
                Offset(w * 0.30f, h * 0.38f),
                Offset(w * 0.70f, h * 0.38f),
                1.2.dp.toPx(),
            )
            drawLine(
                tint,
                Offset(w * 0.30f, h * 0.56f),
                Offset(w * 0.70f, h * 0.56f),
                1.2.dp.toPx(),
            )
            drawLine(
                tint,
                Offset(w * 0.30f, h * 0.74f),
                Offset(w * 0.58f, h * 0.74f),
                1.2.dp.toPx(),
            )
        }
    }
}

private fun typeLabel(entry: FsEntry): String {
    if (entry.isDir) return "目录"
    return when (entry.kind) {
        "image" -> "图片"
        "video" -> "视频"
        "audio" -> "音频"
        "pdf" -> "PDF"
        "text" -> "文本"
        else -> "文件"
    }
}

@Composable
private fun NoticeCard(
    text: String,
    danger: Boolean,
    modifier: Modifier = Modifier,
) {
    val color = if (danger) ZONE_DANGER else MaterialTheme.colorScheme.primary
    Surface(
        modifier = modifier
            .fillMaxWidth()
            .padding(horizontal = 12.dp, vertical = 6.dp),
        color = color.copy(alpha = 0.10f),
        shape = RoundedCornerShape(10.dp),
        border = androidx.compose.foundation.BorderStroke(1.dp, color.copy(alpha = 0.4f)),
    ) {
        Text(
            text,
            Modifier.padding(10.dp),
            style = MaterialTheme.typography.bodySmall,
            color = color,
        )
    }
}
