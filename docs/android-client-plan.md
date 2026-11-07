# DeckMeido Android 客户端：方案与可行性盘点

> 状态：方案稿，尚未开始实现。
> 目标：用原生 Android 客户端替代手机浏览器里的网页控制台。
> 前提：MVP 只连接用户自建的 DeckMeido 后端（局域网或内网穿透），不做公网云服务。

## 结论速览

1. 后端已经有一套完整的 HTTP + WebSocket 协议，**协议层几乎不用改就能支撑原生客户端**：配对、会话、发送、流式正文、思考链、工具事件、状态、STT、上传、截图、文件浏览都已具备。
2. 有三个能力需要明确处理：
   - **手机端“播报”不能直接用**：现有 TTS 是后端用 `tts.speak()` 在掌机本机播放，没有“把回复音频交给手机”的接口。
   - **按会话读取历史不能直接用**：没有 `GET /api/sessions/{sid}/history`；切换会话是全局动作，会影响掌机和其他客户端。
   - **没有任何推送/发现机制**：没有 FCM、没有 mDNS，地址需要手输或扫码；后台只能靠前台服务保活。
3. 技术选型推荐 **Kotlin + Jetpack Compose 原生**。现有前端是单文件 vanilla JS + DOM，四种方案里只有 WebView 壳能真正复用界面；如果目标是“不像浏览器”，原生是最合理的长期路线。
4. 构建环境**已实测可用**：JDK 21、Android SDK 35、build-tools 35、platform-tools、Gradle 8.10.2 缓存都在；用临时最小 Compose 工程已经成功构建出 debug APK。当前没有连接真机，也没装 emulator 和系统镜像。
5. 建议 MVP 按 P0–P6 分 7 个阶段（P3b 是语音播报增量），总代码量约 10k–18k 行 Kotlin（含界面和状态机，不含第三方库），另加约 100 行后端小接口。

---

## 1. 现有能力盘点

标记说明：

- **直接可用**：协议现成，客户端实现即可。
- **需适配**：协议可用，但原生端要自己处理 Cookie、权限、格式或布局。
- **需后端补充**：当前后端没有对应能力，或行为不适合手机端复用。
- **本期不做**：接口存在，但不进入 MVP。

### 1.1 设备鉴权与配对

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 连通性/登录态探测 | `GET /api/auth/session` | 直接可用 | 未登录可访问；返回 `authenticated`、`local`、`device`、`lan_enable`、`device_days`、`version`。App 启动先打这个判断“已配对/未配对/连不上”。 |
| 发起配对 | `POST /api/pair/start` | 直接可用 | 不需要 Cookie；返回 `pair_id`；本次请求的 User-Agent 会作为设备名。配对会话使用 4 位 PIN、60 秒有效、最多 3 次尝试。 |
| 轮询配对状态 | `GET /api/pair/status?pair_id=...` | 直接可用 | 只返回状态，**不返回 PIN**。PIN 只显示在掌机设置页。 |
| 确认配对 | `POST /api/pair/confirm`，body `{pair_id,pin,remember}` | 直接可用 | 成功后 `Set-Cookie: deckmeido_device=...; Path=/; HttpOnly; SameSite=Strict`；`remember=true` 时 Cookie 有效期 `device_days`（默认 30 天），否则是会话 Cookie。服务端令牌本身不随 Cookie 过期。 |
| 掌机侧审批 | `GET /api/pair/pending`、`POST /api/pair/decision` | 需适配 | 只有掌机回环会话能看/批准。**手机无法自己批准配对**，必须去 Deck 设置页点确认并读 PIN。 |
| 设备列表/移除 | `GET /api/devices`、`POST /api/devices/rename`、`POST /api/devices/remove` | 直接可用 | 移除后该设备的 HTTP 请求变 401，WebSocket 以 4401 关闭；App 必须清本地令牌并重新配对。 |
| 本机回环免验证 | 服务端 `netcfg.is_local()` | 不适用 | 只认 `127.0.0.1`/`::1`。手机无论在同不在同一局域网都算远程设备，必须配对。 |
| Cookie 写操作 CSRF | `Origin` / `Sec-Fetch-Site` 校验 | 直接可用 | 原生请求不带这两个头时会放行；不要手动设置与后端 Host 不同的 `Origin`。 |
| WebSocket 认证 | `ws://host:18765/ws` | 需适配 | 必须在握手请求里手动带 `Cookie: deckmeido_device=...`；未认证会先接受再以 4401 关闭。 |
| HTTPS | 无 | 需注意 | 后端只有 HTTP。远程访问时 Cookie 是明文传输；建议用 Tailscale/WireGuard 之类加密隧道，或在反向代理上加 TLS。 |

补充：配对成功后设备令牌是随机明文，只在客户端保存；服务端只存 SHA-256。原生端应把令牌放进加密存储（EncryptedSharedPreferences / Keystore 包装的 DataStore），不要落到普通 SharedPreferences。

### 1.2 WebSocket 事件协议

连接 `ws://host:18765/ws`。服务端连上后先推 `history` 和 `status`；客户端发送 `{type:"ping"}` 得到 `pong`。服务端不会主动 ping，客户端需要自己每 20 秒一次心跳。

| 方向/事件 | 关键字段 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 服务端 → `history` | `messages`、`sid`、`my_client`、`inflight`、`boot`、`partial` | 需适配 | 当前会话的**全量**历史；`content` 可能是字符串，也可能是分段数组，解析要容错。`partial` 是断线时未收尾那一轮的正文和 parts。 |
| 服务端 → `status` | `data`（`running`、`running_since`、电池、模式等） | 直接可用 | 全量状态快照；手机主要取 `running` 和 `running_since`。 |
| 客户端 → `{type:"history"}` | 无 | 直接可用 | 重连后主动拉一次历史，用于补齐断线期间的消息。 |
| 客户端 → `{type:"chat"}` | `text`、`image`、`image_prompt`、`workspace`、`client`、`device`、`brief`、`turn_id`、`surface` | 直接可用 | `text` 必填；`image` 是 base64（重，建议改用上传后拼 Markdown）；`workspace` 手机不要传；建议传 `client:"android"`，并让 User-Agent 含 `Android`，服务端会把来源标成 mobile。 |
| 服务端 → `user_message` | `text`、`ts`、`sid`、`client`、`device`、`brief` | 直接可用 | 用于回显自己或别人的提问。 |
| 服务端 → `stream_start` | `ts`、`sid`、`turn_id` | 直接可用 | 进入流式状态，显示“正在回复”。 |
| 服务端 → `delta` | `text`、`final`、`reply`（仅 `final=true`）、`ts`、`sid` | 需适配 | 按到达顺序追加；`final=true` 只是收尾信号（并带完整 `reply`），最终正文仍以 `assistant_message` 为准。 |
| 服务端 → `thought_start` / `thought_delta` / `thought_end` | `text`、`ts`、`sid` | 需适配 | 思考链可能很长；手机默认折叠，按 30–60ms 节流刷新，不要每个 token 重排。 |
| 服务端 → `tool_activity` | `text`、`ts`、`sid` | 需适配 | “正在执行某工具”的提示条。 |
| 服务端 → `tool_done` | `event`：`name`、`label`、`summary`、`files`、`output`、`diff`、`approved`、`denied`、`reason` | 需适配 | 工具结果卡片；`output` 最长约 1500 字，`diff` 只在写文件时出现。 |
| 服务端 → `ask` | `question`、`options`、`ts`、`sid` | 需适配 | 服务端发选项卡片；手机的回答方式就是发一条普通 `chat` 文本（选选项文字），没有专用回执接口。 |
| 服务端 → `assistant_message` | `text`、`ts`、`sid`、`events`、`client`、`device` | 直接可用 | 最终消息；用它落本地缓存、触发“新回复”通知。语音元数据不在这个事件里，见 `voice_added` / `history`。 |
| 服务端 → `voice_added` | `sid`、`ts`、`voice:{url,seconds,speaker,bytes}` | 需适配 | 目前来自 QQ 语音消息，普通聊天回复不会产生；见 1.5。 |
| 服务端 → `session_changed` | `current` | 直接可用 | **全局**当前会话变化，掌机和手机会一起变。 |
| 服务端 → `system` | `text`、`ts` | 可选 | 系统提示，MVP 可以只显示一条 toast。 |
| 服务端 → `term_start` / `fs_follow` | 命令、路径等 | 本期不做 | 桌面端终端/文件浏览器联动，手机 MVP 忽略。 |
| 服务端 → `update_available` / `selfcheck_progress` / `remote_input` / `pad_order` | 各自状态 | 本期不做 | 更新、自检、远程输入、手柄顺序，均不属于聊天 MVP。 |
| `ws://host:18765/ws/input` | 鼠标/键盘注入 | 本期不做 | 远程输入面板，后续可选。 |
| `ws://host:18765/ws/terminal` | 终端流 | 本期不做 | 手机 MVP 不接。 |

关键点：**所有客户端收到同一份广播**，没有按客户端订阅。手机端必须按 `sid` 过滤，否则切会话或别人发消息时会串台。

### 1.3 会话列表与历史

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 会话列表 | `GET /api/sessions` | 直接可用 | 返回 `{current, items:[{id,title,updated,messages}]}`；没有最后一条消息预览，也没有分页。 |
| 新建会话 | `POST /api/sessions/create` | 直接可用 | 当前已经是空会话时返回 `unchanged:true`，不重复建。 |
| 切换会话 | `POST /api/sessions/switch`，body `{id}` | 需适配 | **切换是全局的**：服务端把 `current_sid` 改掉并广播 `history` + `session_changed`，掌机和其他手机会一起切走。没有“只给当前客户端看另一个会话”的能力。 |
| 删除会话 | `DELETE /api/sessions/{sid}` | 直接可用 | 删除当前会话时会新建空会话并广播。 |
| 删除单条消息 | `POST /api/messages/delete` | 直接可用 | 需要 `ts` 或索引等定位字段，按网页端现有用法实现。 |
| 读取某个会话的历史 | 无 | **需后端补充** | 只能通过 WS 的 `history`（只给当前会话）或 `switch`（全局切换）获得。建议新增只读接口 `GET /api/sessions/{sid}/history`，不改 `current_sid`，避免手机翻历史时把掌机也切走。 |
| 重试上一轮 | `POST /api/retry` | 直接可用 | MVP 可后置。 |
| 压缩上下文 | `POST /api/compact` | 直接可用 | MVP 可后置。 |
| 历史体量 | `history` 全量推送 | 需适配 | 长会话可能几 MB；手机只渲染最近 N 条（例如 50），更早的本地按需展开，并给本地缓存设上限。 |

### 1.4 发送消息与终止

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 发送消息 | WS `{type:"chat"}` | 直接可用 | 没有 HTTP 的 `/api/chat`；聊天必须走 WebSocket。 |
| 终止当前轮 | `POST /api/cancel?client=...&device=...` | 直接可用 | 终止的是服务端**唯一的** `current_task`，没有按 `turn_id` 的归属校验；如果别人刚发了一轮，手机点终止也可能把它掐掉。MVP 只在 `running` 且本轮由自己发起时显示终止按钮。 |
| 服务端消息队列 | 无 | **需后端补充/客户端兜底** | 服务端不排队；`running` 时再发 `chat` 会启动新轮次，和当前轮并行，结果会乱。客户端必须在 `running` 时本地排队，收到 `assistant_message` 后再发下一条。 |
| 运行状态 | `GET /api/status`、WS `status` | 直接可用 | `running`、`running_since` 用于冷启动恢复按钮状态。 |
| 待确认危险操作 | 下一轮普通 `chat` 文本 | 直接可用 | 后端把危险操作挂起，用户在任意已授权入口回复“确认执行”即可；手机不需要专门接口，但要在 UI 上提示“这一步需要确认”。 |

### 1.5 语音

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| STT 状态 | `GET /api/stt` | 直接可用 | 返回运行时/模型/是否启用/是否可录音/duck 状态；不可用时手机展示具体原因，不要假装能录。 |
| 语音转文字 | `POST /api/stt/transcribe`，body 是**原始 WAV 字节** | 直接可用 | 不是 multipart；要求 16kHz、单声道、16-bit PCM WAV；上限 8MB、前端建议 60 秒；返回识别文本，**不会自动发送**。 |
| 录音时压低掌机音量 | `POST /api/stt/duck?on=true/false` | 直接可用 | 录音开始/结束时调用，避免掌机 TTS 声音被录进去。 |
| 下载 STT 模型 | `POST /api/stt/download` | 需注意 | 触发的是**掌机端**下载（模型几百 MB），手机不要代替用户点；手机只展示状态和引导。 |
| TTS 状态 | `GET /api/tts` | 直接可用 | 引擎、是否就绪、错误原因。 |
| 普通回复语音播报 | 无 | **需后端补充** | 现在 `tts.speak(reply)` 是在**掌机本机**播放；没有“取这条回复音频”的 URL。`/api/voice/{name}` 只服务 QQ 语音；`/api/tts/preview` 也是掌机本机播放。 |
| QQ 历史语音 | `GET /api/voice/{name}` | 可选 | 需要 Cookie；只有带 `voice` 元数据的消息（主要是 QQ）才有。 |
| 建议方案 | 新增 `POST /api/tts/say`，body `{text}` → `audio/wav` | 需后端补充 | 复用现有 `tts.synth_wav()`，约 50 行；手机用 Media3 播放。备选：Android 本地 TextToSpeech 朗读，零后端改动但音色与掌机不一致。 |

### 1.6 图片与截图

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 上传附件 | `POST /api/upload`，multipart 字段名 `file` | 直接可用 | 图片上限 20MB，返回 `/uploads/xxx.png`；非图片落到工作区并返回 `@路径`。手机用 OkHttp `MultipartBody`。 |
| 图文消息 | 上传后把 `![图片](/uploads/xxx.png)` 拼进 `chat.text` | 直接可用 | 服务端会解析 Markdown 图片并作为图像输入给模型；不需要走 `chat.image` 的 base64 大字段。 |
| 截取掌机画面 | `POST /api/screenshot` | 需适配 | 返回 base64 JPEG，可能几百 KB，抓帧耗时约 1–3 秒；不是流式，手机要显示 loading。 |
| 历史里的截图 | `history`/`events` 中的 `/shots/xxx` 路径 | 需适配 | 直接当图片 URL 加载，注意拼接 host。 |
| 静态图片 | `/uploads/*`、`/shots/*` | 直接可用 | **不需要 Cookie**：这两个目录是未鉴权静态挂载。局域网内知道 URL 即可访问，属于现有安全弱点。 |
| 文件预览/原图 | `GET /api/preview?path=...&raw=1` | 直接可用 | 需要 Cookie；手机图片加载器要统一带上 Cookie 头。 |

### 1.7 文件浏览

| 能力 | 接口/协议 | 手机端 | 关键约束 |
| --- | --- | --- | --- |
| 列目录 | `GET /api/browse?path=...` | 直接可用 | 路径校验在后端；手机只做展示。 |
| 预览文件 | `GET /api/preview` | 直接可用 | 支持 `raw=1` 取原文件；媒体类交给 ExoPlayer/图片加载器。 |
| 文件操作 | `POST /api/fs`，`{op,path,path2}` | 直接可用但慎用 | 支持 `delete`/`mkdir`/`rename`/`move`/`copy`；手机 MVP 建议只读，避免误删掌机文件。 |
| 下载/打包/元数据 | `GET /api/files`、`GET /api/zip`、`GET /api/file_stat` | 直接可用 | 大文件下载要处理断点/进度，MVP 可只做单文件下载。 |
| 工作区文件 | `GET /files/{path}` | 直接可用 | 只允许工作区内路径。 |
| 完整文件管理器 | 上述组合 | 本期不做 | 桌面端已有；手机 MVP 只做浏览、预览、下载。 |

### 1.8 其他相关接口

| 能力 | 接口 | 手机端 | 说明 |
| --- | --- | --- | --- |
| 状态快照 | `GET /api/status` | 直接可用 | 电量、模式、`running`、上下文用量等。 |
| 服务器配置 | `GET /api/config` | 直接可用 | 含 `lan_url`，可用于“本机地址”引导。 |
| 二维码 | `GET /api/qrcode` | 需适配 | 需要掌机回环登录；QR 在掌机设置页展示，手机扫码即可拿到地址（内容只是控制台 URL，不含配对令牌）。 |
| 版本/国际化 | `GET /api/version`、`GET /api/i18n` | 直接可用 | 未登录可访问。 |
| 更新检查 | `GET /api/update/check` | 可选 | 手机不必做升级，只在关于页展示。 |
| 远程输入/终端 | `/ws/input`、`/ws/terminal` | 本期不做 | 后续可作为“远程操控掌机”的独立功能。 |

### 1.9 后端目前没有的能力

- 没有 mDNS/SSDP/广播发现，手机只能手输地址或扫码。
- 没有推送注册（FCM/UnifiedPush/ntfy 都没有）。
- 没有按会话读取历史、不改当前会话的只读接口。
- 没有把聊天回复合成音频交给手机的接口。
- 没有 HTTPS/TLS 终止。
- 没有消息分页。
- 没有按客户端独立的当前会话视图；`current_sid` 和 `current_task` 都是全局单例。
- 没有按 `turn_id` 的终止归属校验。
- 没有服务端消息队列。

---

## 2. 手机端复用会遇到的问题

### 2.1 鉴权 Cookie 与配对流程在原生端怎么处理

**结论：可以用，但不要依赖 OkHttp 的自动 Cookie 语义。**

- 配对成功后从 `Set-Cookie` 里取出令牌，放进加密存储（EncryptedSharedPreferences 或 Keystore + DataStore）。
- 所有 HTTP 请求和 WebSocket 握手都显式加 `Cookie: deckmeido_device=<token>`。不要因为“换成了 IP/主机名”就丢令牌；按“服务器配置”维度保存令牌，而不是按 OkHttp 的 host 维度。
- 后端对 Cookie 的 `max_age` 只是浏览器存储策略，服务端令牌本身不因 30 天到期而失效。App 的“记住此设备”应作为本地持久化开关；取消勾选就只在本次进程内保存。
- 401 → 清令牌、回到配对页；WS 4401 → 同样处理。不要静默重试。
- 配对必须由掌机侧批准：手机输入地址 → 发起配对 → 提示“请到 Deck 设置页确认并读取 4 位 PIN” → 轮询状态 → 输入 PIN → 确认。PIN 60 秒有效、错 3 次本次作废，UI 要给出倒计时和重新发起。
- 设备名：把 User-Agent 设为类似 `DeckMeidoAndroid/0.1 (Android)`，服务端会把来源识别成 mobile；`chat` 里再显式传 `client:"android"`，消息来源标记才稳定。
- CSRF：原生请求不带 `Origin`/`Sec-Fetch-Site` 会通过；如果以后加拦截器，**不要**给请求加异源 `Origin`，否则写操作会被 403。

### 2.2 局域网发现与地址输入

**结论：后端没有任何发现协议，首版必须做手动地址输入，扫码是加分项。**

- 输入框接受 `192.168.1.5`、`192.168.1.5:18765`、`http://host:port`，默认端口 18765；自动补 `http://`，校验后保存为“服务器配置”。
- 支持从掌机设置页展示的二维码扫码导入地址（`/api/qrcode` 生成的是控制台 URL）。二维码**不含配对令牌**，扫完仍需走配对。
- 不要依赖 `deck.local` 或 `.local` 名称：后端没有 mDNS 广播，不同 Android 版本对 `.local` 解析也不稳定。IP 最可靠。
- 多服务器：MVP 支持一个当前服务器 + 最近地址列表即可；不要一开始做多账号/多后端切换。
- 明文 HTTP：Android 9+ 默认禁止 cleartext，需要 `android:usesCleartextTraffic="true"`（或 network security config 的 base-config 放行）。如果以后支持 HTTPS，再收紧。

### 2.3 麦克风权限与录音格式

**结论：可行，但必须直接产出 16kHz 单声道 PCM16 WAV，不能把 MediaRecorder 的 m4a/webm 直接传给 `/api/stt/transcribe`。**

- 运行时权限 `RECORD_AUDIO`；首次点麦克风再申请，拒绝后给设置入口。
- 用 `AudioRecord` 直接录 `16000Hz / MONO / PCM_16BIT`；如果设备不支持 16k，按 48k 录制后做线性重采样，仍然输出 16k PCM16。
- 自己写 44 字节 RIFF/WAVE 头，上传 `audio/wav` 原始字节；上限 8MB、录音上限 60 秒，到时自动停止并提示。
- 尽量启用 `AcousticEchoCanceler` 和 `NoiseSuppressor`（设备支持时）；录音期间调用 `/api/stt/duck?on=true`，结束后 `on=false`，避免掌机 TTS 回授。
- 识别结果只填入输入框，**不自动发送**，和网页端行为一致。
- 录音前先查 `/api/stt`：模型未下载/未启用时直接提示“请先在掌机设置里启用语音识别”，不要录完才报错。

### 2.4 后台保活与重连

**结论：普通后台 WebSocket 一定会被系统挂起；MVP 必须做前台服务，否则“后台收到回复”不可靠。**

- 用一个前台服务持有 WS 连接，常驻通知显示“已连接 DeckMeido / 正在回复”。Android 14+ 要声明前台服务类型（聊天类可用 `remoteMessaging`，或保守用 `dataSync`，实现阶段按目标版本定），并申请 `POST_NOTIFICATIONS` 运行时权限。
- 服务端不主动 ping；OkHttp 设置 `pingInterval(20s)`，App 层再维护“最后 pong 时间”，40–60 秒无响应判定断线。
- 重连用指数退避 + 抖动（1s、2s、4s…上限 30s），网络切换（Wi-Fi ↔ 蜂窝）时用 `ConnectivityManager` 立即触发一次重连。
- 重连后先发 `{type:"history"}`，合并断线期间消息；如果 `history.partial` 存在，用它恢复未收尾那一轮的正文、思考链和工具事件，否则长任务断线后界面会缺一大块。
- `boot` 变化说明后端进程重启过；不要拿旧缓存硬拼新历史。
- 掌机休眠/关机时手机无法唤醒（MVP 不做 Wake-on-LAN）；UI 要区分“网络不通 / 后端没开 / 掌机休眠”，不要一律显示“连接失败”。

### 2.5 是否需要推送

**结论：MVP 不需要 FCM，也没有可用的公网推送链路；后台通知只能靠前台服务。**

- 后端没有推送注册接口，也没有公网中继；手机离开局域网后无法通过 FCM 收到“回复完成”。
- MVP 行为：前台服务活着时，收到 `assistant_message` 发一条本地通知；服务被杀后不再保证。
- 如果以后要真推送，需要新增后端能力（FCM 需要 Google 服务和公网服务端；UnifiedPush/ntfy 需要额外组件），并且后端主机必须常开。这不在本期范围。
- 注意：如果 DeckMeido 跑在会休眠的掌机上，即使有推送，主机不在线也发不出。远程可用性首先取决于后端主机是否常开。

### 2.6 Markdown 与表格渲染

**结论：协议给的是纯 Markdown，客户端自己渲染；推荐 Markwon，不推荐自己写解析器。**

- 网页端的 `renderMd` 支持代码块、行内代码、粗体、标题、列表、链接、表格（带对齐）。Android 用 **Markwon + TablesPlugin + LinkMovementMethod**，在 Compose 里用 `AndroidView` 包一个 `TextView`，兼容性和表格支持最稳。
- 流式输出不要每个 `delta` 都重新解析整段：按 30–60ms 节流，或只在 `assistant_message` 做最终渲染；流式过程中可以用纯文本 + 简单高亮，结束后切换 Markdown。
- 表格宽度超过屏幕时外层套横向滚动；代码块用等宽字体 + 复制按钮；链接用 `Intent` 打开并提示离开 App。
- 图片 Markdown（`/uploads/...`、`/shots/...`）用 Coil 加载，统一拼 `scheme://host:port`；`/api/preview` 类需要 Cookie 的 URL 用带 Cookie 的 OkHttpClient 给 Coil。
- 不要把模型返回的 HTML 直接当网页渲染；Markwon 默认不执行脚本，足够安全。

### 2.7 窄屏布局

**结论：不能照搬桌面控制台；手机端要做“聊天优先”的独立信息架构。**

- 主界面：消息流 + 底部输入条 + 顶部连接/运行状态；会话列表用抽屉或独立页，不常驻侧栏。
- 思考链和工具时间线默认折叠成一行摘要（“思考 12 秒 / 执行 3 个工具”），点开展开；否则窄屏会被过程信息淹没。
- 设置只保留手机需要的：服务器地址、配对/设备、语音开关、通知、关于；掌机专属的设置（LLM、权限模式、游戏、QQ）不要搬过来。
- 键盘弹出用 `adjustResize` + `WindowInsets.ime`，输入条跟随键盘；横屏和字体放大要能正常换行。
- 深色主题为主，沿用网页端的深色背景和强调色；状态区分不要只靠 1px 边框或透明度差异。
- 长按消息提供复制/重新发送；终止按钮只在运行时出现，避免常驻占位。

### 2.8 其他必须提前知道的约束

- **全局单会话**：`current_sid`、`current_task` 都是服务端单例。手机切会话、终止、发消息都会影响掌机和其他客户端。MVP 要么接受“大家共用一块屏幕”，要么先加只读历史接口缓解“翻历史”场景。
- **全局终止**：`/api/cancel` 没有 `turn_id` 归属校验，可能误杀别人刚发起的轮次。手机只在确认本轮由自己发起时展示终止。
- **无服务端队列**：`running` 时再发消息会并行开新轮，必须在客户端排队。
- **全量历史**：一次推全量，长会话占内存；渲染只取最近 N 条。
- **静态资源未鉴权**：`/uploads`、`/shots` 以及整个 `static` 挂载不校验 Cookie；局域网内知道 URL 就能看。手机端不要把这些 URL 当成秘密，后续可让后端加鉴权。
- **反向代理的回环旁路**：后端只按来源 IP 判断本机。若用 frp/nginx 在掌机本机把 `127.0.0.1:18765` 暴露出去，后端会把所有请求当成本机，**完全跳过鉴权**。远程访问建议用 Tailscale/WireGuard（手机拿到独立 IP），或在代理层保留真实来源并改后端判断逻辑。
- **无 TLS**：公网直接暴露 HTTP 会让设备 Cookie 明文传输；必须套隧道或 TLS。
- **`native=1` 不是给 Android 的**：网页里 `?native=1` 是 Deck 桌面客户端标记（显示麦克风按钮、改变 logo 行为）。Android 原生不要直接复用这个语义；WebView 壳如果带 `native=1`，会继承桌面端交互假设。
- **掌机休眠**：后端主机休眠时手机连不上，MVP 不做 WOL；需要用户在掌机电源设置里保持唤醒，或把后端部署到常开设备。

---

## 3. 技术选型对比

| 方案 | 开发成本 | 对现有前端资产的复用度 | 语音/后台能力 | 长期维护 | 包体积（量级估算） | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| Kotlin + Jetpack Compose 原生 | 中高：全部界面和协议客户端从零写 | 低：CSS/JS/DOM 不能直接复用；协议、文案、颜色可手工移植 | 最强：AudioRecord、前台服务、通知、Media3、权限全部原生可控 | 最好：单一语言、Android 官方栈、升级路径清晰 | 约 10–16MB（release，R8 后） | **推荐** |
| Flutter | 中：Dart + Widget，UI 迭代快 | 低：同样不能复用 DOM/JS | 中强：录音/前台服务/通知依赖插件，边界问题要写平台通道 | 中：多一套工具链和引擎版本要维护 | 约 18–25MB 通用包；按 ABI 拆分约 8–12MB | 备选：如果明确要同时做 iOS |
| React Native | 中高：JS/TS + 原生桥接 | 低到中：纯 JS 逻辑（如 Markdown 解析思路）可借鉴，但现有 UI 是 vanilla DOM，不是 React 组件 | 中：录音、前台服务、通知都要原生模块或第三方库 | 中：桥接层和依赖升级成本持续存在 | 约 20–30MB | 不推荐 |
| WebView 壳 | 最低：包一层 WebView + 权限/文件选择 | 最高：直接加载现有 `index.html`，几乎 100% 复用 | 弱：录音要在 `onPermissionRequest` 放行，后台/通知/文件下载都受限；`native=1` 语义不匹配 | 低：仍然受网页 DOM 和后端前端耦合约束，体验不像原生 | 约 2–4MB | 仅适合过渡/内部试用 |

### 推荐：Kotlin + Jetpack Compose

理由：

1. **现有前端没有可迁移的组件资产**。`server/static/index.html` 是单文件 vanilla JS + DOM + CSS，不是 React/Vue；“复用度高”的只有 WebView 壳，而 WebView 壳正是用户想摆脱的浏览器形态。
2. **MVP 的核心差异点都在原生能力上**：16k WAV 录音、前台上网服务、后台通知、Media3 播放、权限处理。Compose 方案对这些能力的控制最直接，Flutter/RN 都要经过插件或平台通道。
3. **长期维护成本最低**：只维护一套 Kotlin 代码和 Android 工具链；没有 Flutter 引擎、RN 桥接或网页 DOM 的额外升级面。
4. **包体积可接受**：release 后约 8–14MB，明显小于 Flutter/RN 通用包，也远小于“装一个浏览器壳”的心理负担。
5. 构建环境已经具备 JDK 21 + SDK 35 + Gradle 8.10.2 + AGP 8.8，不需要为选型再装一套 Node/Flutter SDK。

不选 Flutter 的原因：当前只做 Android；Flutter 的主要收益是跨平台，而这个项目没有 iOS 需求。不选 RN 的原因：不能复用现有 DOM UI，却要承担桥接和原生模块成本。WebView 壳只建议作为“先验证协议/先给自己用”的过渡，不建议作为最终形态。

---

## 4. MVP 范围与阶段划分

### 4.1 范围定义

MVP = P0–P5，加上 P6 中**前台服务保活**这一项。  
语音“播报”需要后端新增一个小接口；如果坚持零后端改动，则 MVP 只能做“语音输入 + 掌机本机播报”，手机端播报降级为 Android 本地 TTS。

工作量量级定义（按代码/界面复杂度，不按日历时间）：

- **S**：≤ 800 行，1–2 个界面，单点能力。
- **M**：约 1k–2.5k 行，3–5 个界面/状态机。
- **L**：约 2.5k–5k 行，多状态并发、断线恢复。
- **XL**：> 5k 行，跨端协议/架构改造。

### 4.2 阶段表

| 阶段 | 交付物 | 依赖/后端改动 | 工作量量级 |
| --- | --- | --- | --- |
| **P0 连接与配对** | 可安装 APK；服务器地址输入与校验；扫码导入（可选）；`/api/auth/session` 探测；配对发起/轮询/PIN 确认；令牌加密存储；WS 连接与 20s 心跳；401/4401 处理；顶部连接/运行状态；最近地址列表 | 无 | **M**（约 1.5–2.5k 行） |
| **P1 会话列表与历史** | 会话抽屉/列表（标题、消息数、更新时间、当前项）；新建/切换/删除；历史消息渲染（最近 50 条、按需展开更早）；用户/助手/系统角色；时间戳；工具与思考折叠块；消息来源标记 | 建议新增只读 `GET /api/sessions/{sid}/history`，否则先用全局 `switch` | **M**（约 1.5–2.5k 行） |
| **P2 发消息与流式回复** | 底部输入条；发送队列（running 时排队）；`delta` 流式正文；思考链节流展示；工具时间线；`assistant_message` 收尾；终止按钮；重试；断线重连后用 `history` + `partial` 恢复；冷启动恢复 running 状态 | 无（终止沿用 `/api/cancel`） | **L**（约 2.5–4.5k 行，最容易出并发/顺序 bug） |
| **P3 语音输入** | 麦克风权限；16k mono PCM16 WAV 录制；60 秒/8MB 上限；`/api/stt` 状态检查；`/api/stt/transcribe` 上传并填入输入框；录音时 `/api/stt/duck`；AEC/NS；错误提示 | 无 | **S–M**（约 0.8–1.5k 行） |
| **P3b 语音播报** | 后端 `POST /api/tts/say`（text → audio/wav，复用 `tts.synth_wav`）；手机 Media3 播放；播报开关；切走/新轮次自动停止；播放失败降级提示 | **后端约 50 行** | **S**（客户端约 0.5–1k 行） |
| **P4 Markdown 与表格** | Markwon + TablesPlugin；代码块/行内代码/标题/列表/链接；表格横向滚动；代码复制；流式节流；Markdown 图片加载（Coil + Cookie）；长文滚动性能 | 无 | **M**（约 1–2k 行） |
| **P5 附件与截图** | 相册/相机选择；`/api/upload` 上传；图片消息拼接与预览；`/api/screenshot` 查看掌机当前画面；保存/分享；只读文件浏览（可选） | 无 | **M**（约 1–2k 行） |
| **P6 后台与打磨** | 前台服务 + 常驻通知；`POST_NOTIFICATIONS` 权限；后台收到回复的本地通知；网络切换重连；设置页（服务器/设备/语音/通知/关于）；深色主题；崩溃与错误上报（本地日志）；设备移除/重新配对 | 无 | **M**（约 1.5–2.5k 行）；通知可后置 |

### 4.3 每阶段验收标准

- **P0**：冷启动 → 输入地址 → 配对成功 → 杀进程重开仍免配对；错误 PIN/过期/掌机未确认都有明确提示；断网后恢复能自动重连。
- **P1**：会话列表与网页端一致；切换会话后历史正确；不因“翻历史”把掌机切走（若加了只读接口）；长会话滚动不卡死。
- **P2**：发一条会触发长工具调用的消息，期间退出/重进/断网，最终正文和工具时间线不丢不重；终止生效；running 时连续发送不会并行开轮。
- **P3**：中英文短句识别可用；识别结果只填不发；录音 60 秒自动停；掌机语音识别未启用时给出可操作提示。
- **P3b**：点回复上的播放按钮，声音从手机出；掌机本机播报不受影响；播放可中断。
- **P4**：含表格、代码块、长列表、图片的复杂回复能正确渲染；流式过程不闪、不卡。
- **P5**：上传图片后模型能看到；截图能在手机上显示；大图不 OOM。
- **P6**：锁屏/后台 10 分钟后回来仍能收到消息（前台服务未被杀）；通知权限拒绝时降级为应用内提示。

### 4.4 建议的代码结构（实现阶段参考，不在本阶段落地）

- 单 `app` 模块：`data`（协议模型、HTTP/WS、令牌存储）、`domain`（会话/连接状态机）、`ui`（聊天、会话、配对、设置）、`service`（前台连接服务）、`media`（录音/播放/图片）。
- 网络：OkHttp + kotlinx.serialization；WebSocket 用 OkHttp；少量 HTTP 接口不必引入 Retrofit。
- 并发：Coroutines + Flow；一个 `ConnectionState` 状态机（Disconnected/Connecting/Paired/Streaming/Reconnecting）。
- 持久化：DataStore（服务器配置、令牌、草稿）；历史缓存可选 Room（MVP 可只内存 + 最近 N 条）。
- Markdown：Markwon；图片：Coil；音频：Media3；录音：AudioRecord。
- 依赖注入不必上 Hilt，手动单例即可，避免 MVP 过度设计。

---

## 5. 构建环境现状

### 5.1 已检查到的组件

| 组件 | 状态 | 位置/版本 |
| --- | --- | --- |
| JDK | 已安装可用 | Eclipse Adoptium **JDK 21.0.12**，`JAVA_HOME` 已设置 |
| Android SDK | 已安装 | `C:\Users\Escape\AppData\Local\Android\Sdk` |
| SDK Platform | 已安装 | `android-35` |
| Build-Tools | 已安装 | `35.0.0` |
| Platform-Tools / adb | 已安装 | `platform-tools\adb.exe` |
| cmdline-tools | 已安装 | `cmdline-tools\latest`（含 `sdkmanager`） |
| NDK / CMake | 已安装 | SDK 下 `ndk`、`cmake`（本项目 MVP 不需要） |
| Gradle | 未在 PATH，但发行版已缓存 | `~/.gradle/wrapper/dists/gradle-8.10.2-bin`、`9.2.0`、`8.6`；8.10.2 可直接运行 |
| Android Gradle Plugin | 已缓存 | `com.android.tools.build:gradle` 8.8.0、8.2.0 |
| 网络到 Maven | 可达，但大文件可能超时 | `dl.google.com`、Maven Central 均能返回 200；首次下载 Compose AAR 时出现过 `Read timed out`，已用阿里云镜像 + 180 秒超时解决 |
| 已连接 Android 设备 | 无 | `adb devices` 为空 |
| 模拟器/系统镜像 | 未安装 | SDK 下无 `emulator`、无 `system-images` |
| 磁盘空间 | 充足 | C 盘约 58GB 可用 |

### 5.2 实际构建验证

在 `C:\Users\Escape\AppData\Local\Temp\deckmeido_android_probe` 建了一个最小 Kotlin + Compose 工程（compileSdk 35、minSdk 26、AGP 8.8.0、Kotlin 2.0.21、Compose BOM 2024.10.01），用缓存里的 Gradle 8.10.2 执行 `assembleDebug`，验证“这台机器能不能直接出 APK”。

结果：**构建成功**，生成 `app-debug.apk`，大小约 8.3MB（包含 Compose runtime/material3/activity 等基础依赖；真实 App 加业务代码、图片加载、Media3 后，release 包预计仍在 10–16MB 量级）。

过程中遇到两个环境问题，均已有解：

1. **首次从 Google Maven 下载 3 个 Compose AAR 时 `Read timed out`**（约 9 分钟后失败）。把仓库换成阿里云 Google/Central 镜像，并把 `org.gradle.internal.http.socketTimeout` 调到 180 秒后，重试 **1 分 35 秒构建成功**。首次构建后依赖已缓存，后续不需要再走网络。
2. **Kotlin 编译守护进程无法写 `C:\Users\Escape\AppData\Local\kotlin\daemon`（Access denied）**，Gradle 自动回退到“无 daemon 编译”，仍然构建成功。这是本次 Agent 沙箱对该目录的写限制；在正常用户终端里运行不受影响。如果正式工程也遇到，可设置 `kotlin.compiler.execution.strategy=in-process` 或修复该目录权限。

结论：**可以直接构建 APK，不需要再装 JDK/SDK/Gradle。** 建议正式工程预先配置镜像和超时，避免首次构建卡在网络。需要做的只有：

1. 在正式工程里生成 Gradle Wrapper（`gradle wrapper --gradle-version 8.10.2`），或直接用缓存发行版。
2. 写 `local.properties`：`sdk.dir=C:\\Users\\Escape\\AppData\\Local\\Android\\Sdk`。
3. `gradlew assembleDebug`，产物在 `app/build/outputs/apk/debug/app-debug.apk`。
4. 用 `adb install -r` 安装到真机。

### 5.3 还缺什么（测试相关）

- **没有连接真机，也没有模拟器**。要实际测试麦克风、前台服务、通知、后台保活，建议用一台 Android 真机（USB 调试或无线调试）。
- 如果要用模拟器：需要额外安装 `emulator` 和至少一个 `system-images;android-35;google_apis;x86_64`（约 1–2GB），并且宿主机要开启虚拟化。模拟器对录音/后台测试不如真机真实，MVP 验收仍建议真机。
- `ANDROID_SDK_HOME` 当前指向 `D:\Android\SDK\.android`，这个变量语义容易让人误以为 SDK 在 D 盘；实际有效 SDK 是 C 盘路径。建议在正式工程用 `local.properties` 固定 `sdk.dir`，不要依赖这个环境变量。

---

## 6. MVP 边界（明确不做的事）

- **只连用户自建的 DeckMeido 后端**：地址由用户输入（局域网 IP、Tailscale/WireGuard 地址、内网穿透域名均可），不做官方云、不做账号体系、不做多租户。
- **不做公网云服务/中继**：不部署 FCM 服务端、不做消息中转、不做云端同步。
- **不做 iOS/桌面端**：本期只做 Android。
- **不做完整远程桌面**：`/ws/input`、`/ws/terminal`、远程键鼠、游戏串流不在 MVP。
- **不做完整文件管理器**：只做浏览/预览/下载；写操作（删除/移动/重命名）后置。
- **不做 QQ 机器人管理、LLM 配置、权限模式等掌机专属设置**：手机端只保留“看状态、聊天、语音、附件、连接设置”。
- **不做公网推送**：后台通知依赖前台服务；服务被杀后不保证。
- **不假设后端永远在线**：掌机休眠/关机时手机只提示不可达，不做 Wake-on-LAN。

安全边界：

- 默认只在可信局域网或加密隧道内使用。
- 不建议把 18765 直接暴露到公网；如必须，先解决 TLS 和反向代理“回环旁路”问题。
- 现有 `/uploads`、`/shots`、静态资源未鉴权，属于已知风险；手机端不把它当安全存储。

---

## 7. 需要用户确认的决策点

1. **会话历史读取**：接受“切换会话是全局的”，还是同意在后端加只读 `GET /api/sessions/{sid}/history`？推荐加（约 30 行，能避免手机翻历史时把掌机切走）。
2. **语音播报**：同意后端加 `POST /api/tts/say`，让手机播放掌机同款音色？还是先用 Android 本地 TTS（零后端改动、音色不同）？推荐前者。
3. **最低 Android 版本**：建议 minSdk 26（Android 8.0），targetSdk 35；如果要求前台服务/通知行为更简单，可提高到 29+。
4. **远程访问方式**：MVP 是否只保证局域网？如果要远程，建议明确用 Tailscale/WireGuard，而不是 frp 直接映射 127.0.0.1。
5. **文件浏览范围**：只读浏览/预览是否够用？是否需要手机端删除/移动文件？建议 MVP 只读。
6. **后台通知**：是否接受“前台服务 + 本地通知”，不做公网推送？建议接受。
7. **多后端**：MVP 只保存一个当前服务器 + 最近地址，是否够用？

---

## 8. 下一步（等确认后再执行）

确认本方案后，建议按 P0 → P1 → P2 的顺序落地；P0 结束就能在真机上完成“配对 + 连接 + 看状态”，P2 结束即可日常用手机聊天。语音、Markdown、附件和后台打磨在 P3–P6 增量交付，每阶段都可独立安装验证。
