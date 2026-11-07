package com.deckmeido.app.data

data class AuthInfo(
    val authenticated: Boolean,
    val local: Boolean,
    val lanEnable: Boolean,
    val version: String,
)

data class PairSession(
    val pairId: String,
    val state: String,
    val name: String,
    val ip: String,
    val expiresIn: Int,
    val attempts: Int,
)

data class SessionItem(
    val id: String,
    val title: String,
    val updated: Long,
    val messageCount: Int,
)

/** 历史里只关心能直接显示的部分：role / content / ts。 */
data class ChatMessage(
    val role: String,
    val content: String,
    val ts: Double = 0.0,
)

data class HistoryPreview(
    val id: String,
    val title: String,
    val messages: List<ChatMessage>,
)

data class SessionsResult(
    val current: String,
    val items: List<SessionItem>,
)

class ApiException(val status: Int, override val message: String) : Exception(message)
