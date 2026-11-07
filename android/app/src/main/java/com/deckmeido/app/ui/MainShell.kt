package com.deckmeido.app.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.Stroke

import androidx.compose.ui.unit.dp

/**
 * 五项底部导航 + 左右滑动切页。
 *
 * 聊天页自己的输入条仍在页面底部（在导航栏上方），不是放在导航栏里；
 * 五页共用同一个 ViewModel 状态，滑动与点按走同一份 vm.tab，避免两套选中态。
 */
@Composable
fun MainShell(vm: DeckMeidoViewModel) {
    val pager = rememberPagerState(
        initialPage = vm.tab.ordinal,
        pageCount = { Tab.entries.size },
    )

    // 滑动 → vm.tab
    LaunchedEffect(pager) {
        snapshotFlow { pager.settledPage }.collect { page ->
            Tab.entries.getOrNull(page)?.let { if (vm.tab != it) vm.tab = it }
        }
    }
    // 点按/程序切页 → 翻页，并补一次该页的初始数据
    LaunchedEffect(vm.tab) {
        val target = vm.tab.ordinal
        if (pager.currentPage != target) pager.animateScrollToPage(target)
        when (vm.tab) {
            Tab.Files -> if (vm.fsListing == null) vm.loadFiles(vm.fsPath)
            Tab.Terminal -> vm.refreshTerminal()
            else -> Unit
        }
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        // 内层各页自己的 Scaffold 会处理状态栏/导航栏 inset，外层再吃一遍会多出一条空白
        contentWindowInsets = WindowInsets(0, 0, 0, 0),
        bottomBar = {
            NavigationBar(containerColor = MaterialTheme.colorScheme.surface) {
                Tab.entries.forEach { tab ->
                    NavigationBarItem(
                        selected = vm.tab == tab,
                        onClick = {
                            if (vm.tab == tab) {
                                if (tab == Tab.Terminal) vm.refreshTerminal()
                            } else {
                                vm.tab = tab
                            }
                        },
                        icon = { TabIcon(tab) },
                        label = { Text(tab.label) },
                        alwaysShowLabel = true,
                        colors = NavigationBarItemDefaults.colors(
                            selectedIconColor = MaterialTheme.colorScheme.primary,
                            selectedTextColor = MaterialTheme.colorScheme.primary,
                            indicatorColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.16f),
                            unselectedIconColor = MaterialTheme.colorScheme.onSurfaceVariant,
                            unselectedTextColor = MaterialTheme.colorScheme.onSurfaceVariant,
                        ),
                    )
                }
            }
        },
    ) { padding ->
        HorizontalPager(
            state = pager,
            modifier = Modifier
                .fillMaxSize()
                .padding(padding),
        ) { page ->
            when (Tab.entries[page]) {
                Tab.Chat -> ChatScreen(vm)
                Tab.Sessions -> SessionsScreen(vm)
                Tab.Files -> FilesScreen(vm)
                Tab.Terminal -> TerminalScreen(vm)
                Tab.Settings -> SettingsScreen(vm)
            }
        }
    }
}

/**
 * 底部导航图标：只用 Canvas 画，不额外引入 material-icons-extended
 * （Compose 核心图标集里没有文件夹/终端，混用两套风格反而更乱）。
 */
@Composable
private fun TabIcon(tab: Tab) {
    val color = LocalContentColor.current
    Canvas(Modifier.size(22.dp)) {
        val w = size.width
        val h = size.height
        val stroke = 1.8.dp.toPx()
        when (tab) {
            Tab.Chat -> {
                drawRoundRect(
                    color = color,
                    topLeft = Offset(w * 0.06f, h * 0.10f),
                    size = Size(w * 0.88f, h * 0.68f),
                    cornerRadius = CornerRadius(w * 0.22f),
                    style = Stroke(stroke),
                )
                val tail = Path().apply {
                    moveTo(w * 0.28f, h * 0.78f)
                    lineTo(w * 0.28f, h * 0.96f)
                    lineTo(w * 0.48f, h * 0.78f)
                    close()
                }
                drawPath(tail, color)
            }

            Tab.Sessions -> {
                repeat(3) { i ->
                    val y = h * (0.28f + i * 0.22f)
                    drawCircle(color, radius = w * 0.045f, center = Offset(w * 0.16f, y))
                    drawRoundRect(
                        color = color,
                        topLeft = Offset(w * 0.30f, y - h * 0.035f),
                        size = Size(w * 0.56f, h * 0.07f),
                        cornerRadius = CornerRadius(h * 0.035f),
                    )
                }
            }

            Tab.Files -> {
                drawRoundRect(
                    color = color,
                    topLeft = Offset(w * 0.04f, h * 0.22f),
                    size = Size(w * 0.40f, h * 0.16f),
                    cornerRadius = CornerRadius(w * 0.06f),
                )
                drawRoundRect(
                    color = color,
                    topLeft = Offset(w * 0.04f, h * 0.30f),
                    size = Size(w * 0.92f, h * 0.52f),
                    cornerRadius = CornerRadius(w * 0.10f),
                    style = Stroke(stroke),
                )
            }

            Tab.Terminal -> {
                drawRoundRect(
                    color = color,
                    topLeft = Offset(w * 0.04f, h * 0.12f),
                    size = Size(w * 0.92f, h * 0.76f),
                    cornerRadius = CornerRadius(w * 0.12f),
                    style = Stroke(stroke),
                )
                drawLine(
                    color, Offset(w * 0.20f, h * 0.34f), Offset(w * 0.34f, h * 0.46f), stroke,
                )
                drawLine(
                    color, Offset(w * 0.34f, h * 0.46f), Offset(w * 0.20f, h * 0.58f), stroke,
                )
                drawLine(
                    color, Offset(w * 0.44f, h * 0.60f), Offset(w * 0.66f, h * 0.60f), stroke,
                )
            }

            Tab.Settings -> {
                // 推子（tune）图标：三条横线 + 三个滑块，比齿轮更好在小尺寸下认
                repeat(3) { i ->
                    val y = h * (0.24f + i * 0.26f)
                    drawLine(color, Offset(w * 0.08f, y), Offset(w * 0.92f, y), stroke)
                    val x = w * (0.30f + i * 0.22f)
                    drawCircle(color, radius = w * 0.075f, center = Offset(x, y))
                }
            }
        }
    }
}
