package com.deckmeido.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp

@Composable
fun AppRoot(vm: DeckMeidoViewModel) {
    val snackbar = remember { SnackbarHostState() }

    LaunchedEffect(vm.notice) {
        val text = vm.notice
        if (text != null) {
            snackbar.showSnackbar(text)
            vm.clearNotice()
        }
    }

    Box(Modifier.fillMaxSize()) {
        when {
            vm.scanOpen -> ScanScreen(vm)
            !vm.mainReady -> ConnectScreen(vm)
            else -> MainShell(vm)
        }
        SnackbarHost(
            snackbar,
            Modifier
                .align(Alignment.BottomCenter)
                // 五项导航栏占高度，提示条要抬到它上面
                .padding(bottom = if (vm.mainReady) 88.dp else 16.dp),
        )
    }

    // 返回键只用来退出扫码；主界面交给系统默认（回桌面）
    BackHandler(enabled = vm.scanOpen) { vm.cancelScan() }
}
