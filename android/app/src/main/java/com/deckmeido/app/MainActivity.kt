package com.deckmeido.app

import android.graphics.Color
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.lifecycle.viewmodel.compose.viewModel
import com.deckmeido.app.ui.AppRoot
import com.deckmeido.app.ui.DeckMeidoTheme
import com.deckmeido.app.ui.DeckMeidoViewModel

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(Color.TRANSPARENT),
        )
        setContent {
            DeckMeidoTheme {
                val vm: DeckMeidoViewModel = viewModel()
                AppRoot(vm)
            }
        }
    }
}
