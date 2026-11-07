package com.deckmeido.app.ui

import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier

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
        when (vm.screen) {
            Screen.Connect -> ConnectScreen(vm)
            Screen.Sessions -> SessionsScreen(vm)
            Screen.Preview -> PreviewScreen(vm)
            Screen.Chat -> ChatScreen(vm)
        }
        SnackbarHost(
            snackbar,
            Modifier
                .align(Alignment.BottomCenter)
                .navigationBarsPadding(),
        )
    }

    BackHandler(enabled = vm.screen != Screen.Connect) {
        vm.back()
    }
}
