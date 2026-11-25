<p align="center">
  <img src="assets/logo.png" width="140" alt="DeckMeido">
</p>

<h1 align="center">DeckMeido</h1>

<p align="center">
  <b>住在 Steam Deck 里的 AI 女仆</b><br>
  手柄优先，QAM、大屏、网页、桌面客户端都能用，也能从 QQ 远程使唤她
</p>

<p align="center">
  <img alt="SteamOS / Linux" src="https://img.shields.io/badge/SteamOS%20%2F%20Linux-1a1a1a?logo=steam&logoColor=white">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="CC BY-NC 4.0" src="https://img.shields.io/badge/License-CC%20BY--NC%204.0-3a9d5d">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#能力总览">能力</a> ·
  <a href="#最近新增">新增</a> ·
  <a href="#配置">配置</a> ·
  <a href="#qq-用法">QQ</a> ·
  <a href="#安全与权限">安全</a> ·
  <a href="#故障排查">排查</a>
</p>

<p align="center">
  <a href="#安装">安装</a> ·
  <a href="#数据目录">数据目录</a> ·
  <a href="#升级与卸载">升级与卸载</a> ·
  <a href="#开发">开发</a> ·
  <a href="#许可">许可</a>
</p>

---

## 这是什么

跑在掌机本机上的 AI Agent。她不只会聊天：能截当前画面看图答问、按坐标点击、装游戏、执行命令，也能在 QQ 里接受远程指令。

三种使用方式，同一套引擎：

| 场景 | 你能做什么 |
| --- | --- |
| 游戏内 QAM | <kbd>X</kbd> 翻译画面、<kbd>Y</kbd> 截图询问、按住 <kbd>R2</kbd> 说话；<kbd>LB</kbd>/<kbd>RB</kbd> 翻页 |
| 大屏 / 桌面 | 完整聊天、设置、文件与终端；手柄和键鼠都能操作 |
| QQ 远程 | 管理员下指令、群友只读聊天；高危操作有独立风险档位 |

---

## 快速开始

```bash
git clone https://github.com/Congmeims/DeckMeido.git && cd DeckMeido
bash install.sh
```

GitHub 连不上时走国内镜像：

```bash
curl -fsSL -o /tmp/dm.sh https://meido.meagames.cn/deckmeido/install.sh
bash /tmp/dm.sh from-mirror
```

装完打开网页或桌面客户端，进「设置」填模型地址与 Key 即可对话。数据默认在 `<安装目录>/userdata/`。

---

## 能力总览

| 能力 | 说明 | 边界 |
| --- | --- | --- |
| 对话与画面 | 单轮询问、多轮聊天、流式输出、思维链、上下文自动压缩、长期记忆 | 看图需要模型支持 vision，不支持时退化为纯文字 |
| 截图询问 / 翻译 | 抓当前画面 + 游戏名，按可改模板回答或逐句翻译 | 不剧透；模板可改，保存即时生效 |
| 语音 | 离线识别（sherpa-onnx + SenseVoice）、本地播报（三种发音人：机器人 / 月读酱 / 哔哔啵啵） | 模型首次按需下载，不进发布包 |
| 智能音箱 / 音乐 | 本地唤醒词、连续对话、语音播控与点歌 | 需 sherpa-onnx 与录音工具；音乐依赖 Decky Music |
| 设备操作 | 命令、截图、窗口聚焦、点击/输入、Steam 库与安装、文件与终端 | 受 `[security] permission_mode` 与远程风险档位约束 |
| QQ 远程 | NapCat OneBot 11；私聊指令、群内 @、固定命令菜单、归档 | 群白名单为空时全部不响应；群友只读 |
| 跨屏键鼠 | 浏览器接管 Deck 的鼠标键盘 | 仅已配对设备；建议只在可信局域网开 |
| 外观与壁纸 | 主题三档（浅色 / 深色 / 暗黑）、自定义壁纸、自动配色、透明度与毛玻璃 | 壁纸 ≤ 20 MB，存在 `userdata/wallpapers/`；也能联网找图换壁纸 |
| 睡眠模式 | 歌停了又长时间没人动，自动倒计时关机 | 默认关闭，`[sleep] enabled` 打开 |

### 多入口

| 入口 | 说明 |
| --- | --- |
| QAM 面板 | Decky 插件，游戏内三页签「询问 / 聊天 / 设置」，<kbd>L1</kbd>/<kbd>R1</kbd> 翻页 |
| 大屏页 | 注册到 GamepadUI 路由，功能同 QAM；<kbd>X</kbd> 翻译、<kbd>Y</kbd> 截图询问、<kbd>R2</kbd> 说话 |
| 桌面客户端 | PySide6 + QWebEngineView，加载与网页版同一界面；最小化进托盘 |
| 网页控制台 | 电脑或手机浏览器打开 `http://<deck-ip>:18765/`；首次打开用掌机 PIN 配对新设备 |
| QQ | NapCat WebSocket；Deck 主动外连，本机不开入站端口 |
| 智能音箱 | 客户端托盘进入；全屏猫脸页反馈状态，唤醒词进入一轮语音问答并自动播报 |

QAM 面板由 Decky 插件提供，以 root 运行、走本机回环免鉴权；大屏页从 QAM 直接跳转。

---

## 最近新增

| 功能 | 行为 |
| --- | --- |
| **外观与壁纸** | 设置 → 外观：主题三档（浅色 / 深色 / 暗黑，暗黑档纯黑省 OLED）、上传壁纸（PNG / JPG / WEBP / GIF / BMP，≤ 20 MB）、五种铺法（填充 / 适应 / 拉伸 / 平铺 / 居中，左上胶囊点一下换一种）、九宫格焦点、透明度 / 模糊 / 毛玻璃；配色默认取壁纸主色，也能手改强调色。她还能联网搜壁纸，对话里给 4 张候选供你挑 |
| **睡眠模式** | `[sleep] enabled` 打开后，「音乐已停」且「`loginctl` 报会话空闲」两个条件同时满 `idle_minutes`（默认 60）分钟，才进入 `countdown_sec`（默认 60 秒）倒计时；期间任何输入、唤醒或 `POST /api/sleep/cancel` 都能取消，眼睛页同步显示倒计时 |
| **女仆长** | `auto` 档碰到危险操作先交给她判：放行就直接执行，拦截会说明理由，拿不准就请你定夺（15 秒不答按请定夺处理）；升级自身和远程操作不交给她。对话里会留下「女仆长已介入」的记录 |
| **文件搬运进度** | 她跑 `mv` / `cp` / `rsync` 时，文件面板出现带进度条与「取消」的卡片；命令自己甩到后台的按 `du` 估个大概，显示成「约」 |
| **OCR 兜底** | 装了 OCR 可选组件后，纯文本模型收到图片会先用 RapidOCR（PP-OCR 模型，中英日）认字再作答；也能主动调 `ocr_image` |
| **三种发音人** | `[tts] speaker` 三选一：机器人（espeak-ng）/ 月读酱（piper-plus 6 语）/ 哔哔啵啵（纯代码合成，不需要模型）；老配置的 `engine` / `voice` 自动映射 |
| **QQ 自动连接与主动接话** | `enable` 只表示允许通道，默认「已启用 · 未连接」，点连接或 `POST /api/qq/connect` 才建连；`auto_connect` 打开才随服务自动连。`proactive_chance`（0~100）管群里没人 @ 时自己插话的概率，默认 0 |
| **QQ 敏感词** | `redact_words` 把人设与群聊里的指定词换成 `redact_mask`（默认 `***`），不分大小写与全半角；单字词忽略，免得误伤 |
| **会话与归档** | 侧栏按来源分组（电脑 / 手机 / 掌机 / 插件 / QQ / 唤醒）；QQ 会话头部有连接状态与快捷开关，归档可一键「打开图片文件夹」 |
| **离线模式与探测** | `[runtime] offline_mode` 手动开关；开启后所有出网入口（模型对话与列表、更新、余额、组件 / 语音 / 唤醒模型下载、QQ 图片、联网搜索 / 天气 / 商店）快速失败，回环与局域网算本地。`GET /api/net/probe` 判断能不能到公网，探测不通只建议、不强制切离线 |
| **记忆 scope 与预算** | 记忆按 `private` / `group:<群号>` / `person:<QQ>` 分 scope；`/memory` 查看（最多 10 条），`/memory clear [群/人/私聊/all]` 清空；单轮注入上限 2500 字，超了按「知识 → 环境经验 → 画像」丢，只影响本轮 |
| **TurnPolicy** | 主人档保持全部能力；群友档零工具、无长期记忆、400 字硬上限、强制脱敏、只给公开人设（`extra` 只给主人），上下文只带最近 20 条 × 40 字，全在代码层强制 |
| **命令菜单与脱敏** | `/help` 或「菜单 / 帮助」列出内置指令与口语别名；群友回复替换配置键、绝对路径、IP、令牌样式文本，后端日志统一打码凭据、Cookie、URL 查询值、非回环 IP 与 QQ 号 |

---

## 功能细节

### 对话与画面

- **询问**：单轮问答，可带当前画面；游戏名由插件自动取到并写进提示词。
- **聊天**：多轮会话，流式输出、思维链、上下文自动压缩、长期记忆。
- **截图询问**：抓当前画面 + 游戏名，按 `[agent] ask_template` 回答「这一步怎么过」，不剧透；模型不支持看图时，装了 OCR 组件会先认字再答。
- **截图翻译**：抓画面按 `[agent] translate_template` 原文照抄后逐句翻译。
- **可改提示词**：询问模板、翻译模板、状态栏短句提示词都能在页面或插件设置里改，保存即时生效。
- **模型可换**：设置页可拉取模型列表并切换，兼容任意 OpenAI 接口（DeepSeek / GPT / Claude 中转 / Qwen / LM Studio 等）。
- **提问卡片**：信息不够或要你在几个方案里挑时，她能弹选项卡（最多 6 项），点一下就算回答。
- **记一笔**：你说「记住……」她会用 `remember` 写进长期记忆，重复内容不会重复存。

### 语音

- **语音输入**：sherpa-onnx + SenseVoice-Small int8，纯 CPU 离线识别，中英混说带标点；按住说话期间默认静音外放（`duck_output`，避免外放灌进麦克风；缺 `wpctl` / `pactl` 时自动跳过），模型压缩包约 1GB（只解出约 250MB），首次按键时按需下载到 `userdata/models/sensevoice/`，闲置一段时间自动释放。
- **语音播报**：默认关闭。`[tts] speaker` 三选一：机器人（espeak-ng）、月读酱（piper-plus + つくよみちゃん 6 语模型，ja/en/zh/es/fr/pt，约 38 MB，首次使用时下载到 `userdata/models/tts/tsukuyomi/`，不进发布包）、哔哔啵啵（纯代码合成，只用标准库，不需要模型）。老配置里的 `engine` / `voice` 会自动映射；可设语言、语速与单次字数上限。

### 智能音箱与音乐

- **唤醒**：默认关闭。打开后后端用 sherpa-onnx KWS + silero VAD 常驻监听，默认唤醒词「你好梅朵」（`[wake] wake_words` 可改）。唤醒、VAD 与录音都在本机完成，音频只在内存里停留，不落盘、不上传；只有识别出的文字会进入现有对话链路。
- **一轮流程**：命中原词后先播预合成的「我在」，把音乐音量压到原来的 25%，再进入「听 → 想 → 说」；回答沿用当前人设与模型，并用 TTS 念出来。说完保留 4 秒追问窗口，没人接话就回待机；单轮录音上限 15 秒、最短 600 毫秒，唤醒后 8 秒没人说话自动退回。
- **三道闸**：KWS 置信度阈值、VAD 确认真人声、最短语音时长，任一不过就不转写、不发送。
- **本地口令**：`[wake] local_intent` 默认关闭。打开后「放稻香」「下一首」「大点声」这类短口令由本地正则直接执行，不调模型、不花额度；本地没认出来的句子照常进对话，长句点播交给模型调 `music_play`。
- **音乐播控**：暂停/继续/停止/上一首/下一首/音量加减/静音/取消静音，以及「随机播放队列里的一首」。播控走 Decky Music 注册的 MPRIS2；点歌走 Decky Loader 的本地 RPC，只允许搜索、读播放状态/队列、插队、播放这几个白名单方法。
- **眼睛页（猫脸）**：客户端托盘提供「智能音箱模式 / 退出」，进入后加载 `/eyes.html` 全屏猫脸页，跟着播放状态眨眼、露出专辑封面、显示睡眠倒计时；退出恢复主界面。状态也可从 `/api/wake/state` 读取。
- **电源**：模式期间持有「唤醒模式」电源锁，避免掌机在监听时休眠。

**依赖**：

- sherpa-onnx 运行时与 `requirements-stt.txt`（KWS、silero VAD、SenseVoice 识别共用）。
- 系统录音工具 `parec` 或 `pw-record`；音乐播控需要 `busctl`。
- 首次启用会下载约 38 MB 的 KWS 模型到 `userdata/models/wake/`，并调用 `sherpa-onnx-cli` 生成 keywords 文件。
- 音乐功能需要安装 Decky Loader 插件 **Decky Music**（MIT，支持 QQ 音乐 / 网易云音乐），并在插件里登录音乐服务；没装或没登录时播控/点歌会回一句中文原因，不影响问答。
- 回答语音按上一节的 TTS 配置；TTS 不可用时只显示文字状态。

**限制**：

- 半双工：TTS 播放期间丢弃采集帧，不能打断她说话（没有 barge-in）。
- 一次只处理一轮；唤醒词识别是本地小模型，环境噪声大或说话太轻时会漏唤醒。
- 点歌只搜 Decky Music 曲库；曲库未登录、无版权或搜不到时会明确说放不了。点歌新建 Loader WebSocket 时 QAM 面板可能闪一下（Loader 会关掉前端原有连接，前端随后自动重连）。
- `eyes_display` 目前只作为状态字段，客户端固定使用当前屏幕，暂不区分内屏/外屏。
- 对话仍走已配置的模型：没配本地模型时，唤醒与识别在本地，回答仍需要联网；离线模式下只有本地端点可用。

### 睡眠模式

- **开关**：`[sleep] enabled`，默认关闭；设置页有独立卡片。
- **判据**：两个条件**同时**成立并持续满 `idle_minutes`（默认 60）分钟才触发——音乐已停（MPRIS 不在播放），且 `loginctl` 报会话空闲（键鼠、手柄、玩游戏都会清零）。只停音乐或只看空闲都不关，避免掐掉正在听的歌或毁存档。
- **倒计时**：`countdown_sec`（默认 60 秒）内任何输入、唤醒或 `POST /api/sleep/cancel` 都能取消；眼睛页同步显示倒计时。
- **关机**：倒计时结束执行 `systemctl poweroff`，交给系统授权，不碰 sudo 密码。

### 设备操作

- **执行命令**：`manual` 全部审批、`auto` 只读放行 / 危险先交女仆长、`full` 不审批。
- **桌面操作**：按窗口名聚焦、按坐标点击、键盘输入；Wayland 下优先 klipper 剪贴板粘贴，其次 ydotool。
- **虚拟手柄**：已移除。软件不再创建或使用虚拟手柄，AI 在游戏模式下只做只读操作与截图，需要按键时由用户自己用手柄操作。
- **截图**：桌面模式走 spectacle/KWin，游戏模式走 gamescope PipeWire 抓帧。
- **Steam**：列已装游戏、商店搜索、按 appid 安装、检查系统更新；安装属危险操作，强制确认。
- **文件与终端**：工作区内浏览、读写、精确替换；网页终端与文件预览；模拟器 ROM 扫描；删除走系统回收站；`mv` / `cp` / `rsync` 搬家时文件面板显示进度条与「取消」（后台命令按 `du` 估算）。
- **自我更新**：`self_update` 可以查新版本，升级必须经你确认，会拉新代码、补依赖并重启后端（当前对话会断）。
- **壁纸与搜图**：`set_wallpaper` 换壁纸，`search_wallpaper` / `search_image` 联网找图并给出候选，选中的图也能存进工作区。
- **OCR**：`ocr_image` 提取图片里的文字（中英日），需要装 OCR 可选组件。
- **操作遮罩**：执行工具时全屏半透明遮罩显示当前动作；点一下遮罩或按音量键立即取消当前任务（遮罩期间音量键改为强制停止）。

### 设置与数据

- **人设**：「女仆人设」页可改名字、性格、称呼、语气、句尾、禁词与 `extra` 提示词；「导出备份 / 导入备份」把人设与长期记忆打成一个 JSON，换机或重装后整体恢复。
- **外观**：设置 → 外观可改主题（浅色 / 深色 / 暗黑）、上传壁纸、挑配色、调透明度 / 模糊 / 毛玻璃；壁纸存 `userdata/wallpapers/`，AI 找的图默认存进工作区。
- **会话**：新建、切换、删除会话，删除单条消息；长会话可手动压缩，平时按 `compact_ratio` 自动压缩；侧栏按来源分组（电脑 / 手机 / 掌机 / 插件 / QQ / 唤醒）。
- **游戏预设**：插件检测到游戏启动时按 appid 套用该游戏的提示词预设，退出回默认；设置页可新增、编辑、改名、删除。
- **多组 API**：`[llm]` 最多保存 20 组「地址 + Key」，设置页切换当前使用的一组；用量统计按稳定 id 归属，改名不拆历史。旧版单组 `base_url` / `api_key` 会自动迁移。
- **用量与余额**：设置页按北京时间自然日、按模型统计 token 用量，并按高峰/闲时分桶估算费用；DeepSeek 官方接口可查余额；支持导出 JSON 与清空统计。
- **界面语言**：`[ui] language` 支持 `zh` / `en`。英文是机翻，未收录的句子保持中文；插件面板暂未翻译。
- **开发者模式**：`[runtime] dev_mode` 打开后额外记录 HTTP/WebSocket、工具调用、LLM 请求与流式分片、STT/TTS 等调试日志，写到 `userdata/logs/debug-<日期>.log`（单文件 5 MB，保留 2 份）；设置页可清空调试日志。凭据、Cookie、URL 查询值等始终打码，与开关无关。
- **设备与审计**：局域网设备通过 PIN 配对（配对成功后设备保存令牌，请求时放在 `Authorization` 请求头里，有效期 `[security] device_days`，默认 30 天）；设置页可重命名/移除已配对设备，并查看最近的鉴权与配对审计事件（不记录 PIN 与设备令牌）。
- **状态栏短句**：QAM 与大屏页顶部显示一句话（待机 / 忙碌 / 出错 + 高置信度游戏名），由 `/api/status/line` 生成；模型失败时自动回落本地模板，状态栏不会空白。
- **核心开关**：插件 QAM 与大屏页可「启动 / 重启 / 关闭核心」；关闭后保持停止，客户端不会自动重拉，直到再点启动。
- **sudo 复用**：一次密码校验通过后，`[security] sudo_ttl`（默认 600 秒）内后续 sudo 命令走 `SUDO_ASKPASS` 复用；密码只存内存，不落盘。

### 跨屏键鼠

1. 网页设置里打开 `[remote_input] enabled`（会先弹一次安全确认）。
2. 在电脑浏览器点网页左下角 DeckMeido logo 进入接管；指针锁定后移动与按键都发往 Deck。
3. 再点 logo、按 <kbd>Esc</kbd>，或到达 `max_minutes` 自动释放。

注入优先 `ydotool`（由 `ydotoold` 用户服务托管），不可用时直写 `/dev/uinput`——这要求后端有 root 或 `input` 组权限。只有已配对设备能接管。

---

## QQ 用法

触发前缀就是软件里配置的昵称，默认 `meido`。私聊管理员直接发；群里默认必须 @ 机器人，管理员免 @。指令前可带昵称前缀，也支持整句口语别名。

`enable` 只表示允许这条通道：装好默认是「已启用 · 未连接」，点设置页 / QQ 会话面板的「连接」（或 `POST /api/qq/connect`）才建连；`auto_connect = true` 才会随服务自动连。

| 操作 | 斜杠指令 | 口语别名示例 |
| --- | --- | --- |
| 查询状态 | `/status` | `状态`、`在吗`、`怎么样了` |
| 截屏 | `/shot` | `截个屏`、`看看屏幕`、`拍张照` |
| 取消当前任务 | `/cancel` | `停`、`别做了`、`算了` |
| 存图 | `/save_image` | `存图`、`保存这张` |
| 存文本 | `/save_text` | `保存这段`、`存这段文字` |
| 检查依赖 | `/deps` | `检查依赖`、`查依赖`、`依赖状态` |
| 查看记忆 | `/memory` | `记忆`、`查看记忆`、`群记忆` |
| 清空记忆 | `/memory clear [scope]` | `清空记忆`、`清空群记忆`（仅管理员） |
| 清群常识缓存 | `/memo` | `清除本群缓存`（仅管理员） |
| 指令菜单 | `/help` | `菜单`、`帮助`、`有什么指令` |

固定指令命中后直接执行既有动作，不经过 AI。`/deps` 只检测并回报依赖状态，不会安装任何东西、不改设备环境。其余文本按聊天处理，可让她执行设备操作。优先级：斜杠指令 > 取消 > 状态 > 截图 > 聊天。

**归档**：可存图、存文本，图片进 `meidobot/images/<日期>/`，索引与文本分别写入 `inbox_index.jsonl`、`text_log.txt`；QQ 会话面板上有「打开图片文件夹」，直接调起掌机文件管理器。

**其它能力**：

- **语音条**：`voice_reply` 打开后，回复可附带一条 NapCat `record`。私聊只对 `admin_qq` 生效，群里是否放开给群成员由 `voice_group_members` 决定。一轮最多额外发一条，默认上限 60 秒 / 4 MB；`voice_only` 打开时语音失败会改发文字，不会吞掉回复。需要 TTS 模型可用。
- **图片**：`allow_image` 打开后下载并识别 QQ 图片，可作视觉输入并归档；单张上限 `max_image_mb`（默认 8 MB），最近 `image_recent_minutes`（默认 2 分钟）内的图会跟着聊天轮次带上。下载只放行公网 http(s)，挡住 `file://`、内网地址、异常端口和重定向绕道。
- **主动接话**：`proactive_chance`（0~100）控制群里没人 @、也没带前缀时自己插话的概率，默认 0（不插话）。
- **敏感词**：`redact_words` 命中的词替换成 `redact_mask`（默认 `***`），不分大小写与全半角；单字词自动忽略，免得误伤。
- **群常识缓存**：`group_memo` 打开且群在白名单时，每群保留最近 20 条、每条压到 80 字、5 分钟后过期，整段最多 400 字，最多同时缓存 200 个群，仅供理解语境；纯内存、不落盘、不写长期记忆，`/memo` 可清空本群缓存。
- **安装后自动启动**：安装类命令成功后按 `auto_launch_after_install` 自动拉起刚装好的程序，等 `auto_launch_wait_seconds` 秒再截图回一条图文。

---

## 安全与权限

| 层 | 规则 |
| --- | --- |
| 本机权限 | `manual` 全部审批；`auto` 只读放行、危险操作先交女仆长打理；`full` 不审批 |
| 女仆长 | `auto` 档的危险操作由她判定：放行直接执行、拦截会说明原因、拿不准就请你定夺（15 秒不答按请定夺）；升级自身与远程操作不交给她 |
| 远程风险三档 | `deny` 直接拒绝；`hard_confirm` 只认「确认执行」；`normal` 走普通确认 |
| 群白名单 | 只响应 `allow_groups` 里的群；白名单为空时全部不响应，主人身份也不构成豁免 |
| 管理员 | `admin_qq` 里的号可下指令并触发设备操作；危险操作仍要确认 |
| 群友 | 只读聊天：零工具、无长期记忆、400 字上限、输出脱敏，上下文只带最近 20 条 × 40 字；`TurnPolicy` 在代码层强制，不靠提示词 |
| 局域网 | `[server] lan_enable` 默认关闭；开启后用 PIN 配对（设备令牌放在请求头，不用 Cookie），可重命名/移除设备并查看审计；本机来源免验证 |
| 日志 | 落盘前统一脱敏：凭据、Cookie、URL 查询值、非回环 IP、QQ 号 |
| 关机 / 重启 | 走 sudo，要密码；一次校验通过后在 `sudo_ttl` 内复用（睡眠模式的 `systemctl poweroff` 另走系统授权） |

**风险命令**：

- `deny`：`rm`、`rmdir -r`、`shred`、`dd`、`mkfs`、分区写入、写 `/dev` 块设备、fork bomb、写系统关键文件等，远程一律拒绝。
- `hard_confirm`：`sudo`、下载后管道给 shell、`/etc` 或 `/usr` 写入、包管理器安装卸载、`crontab` 修改、非电源类 `systemctl restart` 等，必须回复「确认执行」。
- `normal`：其余设备操作，按普通确认流程走。

内置指令之外的设备操作，先按 `[security] permission_mode` 判断本机权限，再叠加远程风险档位。

---

## 配置

改 `userdata/config.toml` 后重启后端生效；网页里改大多立即生效，端口、局域网监听等需要重启后端（设置页会提示）。

| 配置段 | 关键项 | 说明 |
| --- | --- | --- |
| `[server]` | `lan_enable` / `port` | 默认 `false` / `18765`；旧配置里的 `token` 键会被忽略 |
| `[runtime]` | `offline_mode` / `dev_mode` | 离线模式手动开关；开发者模式额外写调试日志。默认均为 `false`；离线探测只提示，不自动切换 |
| `[llm]` | `apis` / `current_api` / `model` | OpenAI 兼容地址与密钥，最多 20 组；旧版单组 `base_url` / `api_key` 自动迁移，换服务商时模型名要一起改 |
| `[llm]` | `reasoning_effort` / `timeout` / `max_continuations` | 思维链强度、请求超时、撞上限自动续写轮数 |
| `[agent]` | `ask_template` / `translate_template` / `status_prompt` | 截图询问、截图翻译、状态栏短句的提示词 |
| `[agent]` | `context_window` / `compact_ratio` | 上下文窗口（0 = 按模型名推断）与压缩触发比例 |
| `[agent]` | `max_tool_rounds` / `with_shot` / `chat_voice` | 单轮工具轮数上限（默认 50）；聊天自动带当前画面；「对话与语音」整组总开关，关掉后截图带画面与语音输入一起停用 |
| `[security]` | `permission_mode` / `sudo_ttl` | 本机权限档位；sudo 密码复用秒数（默认 600） |
| `[security]` | `device_days` | 配对设备令牌有效期（默认 30 天），过期需重新配对 |
| `[workspace]` | `dir` | 文件工具工作区；默认 `~/MeidoSpace` |
| `[stt]` | `enable` / `model_source` | 离线语音识别，默认开启；模型约 250 MB，按需下载 |
| `[tts]` | `enable` / `speaker` / `speed` | 语音播报；`speaker` 三选一（`robot` / `maid` / `bibibo`），旧 `engine` / `voice` 自动映射 |
| `[appearance]` | `theme` / `wallpaper` / `palette` / `dim` / `blur` / `glass` / `wall_fit` / `wall_pos` | 界面主题、壁纸、配色与透明度；壁纸文件放 `userdata/wallpapers/` |
| `[sleep]` | `enabled` / `idle_minutes` / `countdown_sec` | 歌停 + 无人操作自动关机；默认关闭、60 分钟、60 秒倒计时 |
| `[wake]` | `enable` / `wake_words` / `followup_sec` / `local_intent` | 智能音箱唤醒词、追问窗口与本地口令直连 |
| `[ui]` | `language` | `zh` / `en`；英文为机翻，插件面板仍为中文 |
| `[power]` | `keep_awake` | 桌面模式下执行命令或显示操作遮罩时抑制息屏与休眠（游戏模式不管） |
| `[update]` | `github_enable` / `mirror` / `github_repo` | 双源更新：默认镜像与 GitHub 都探测，取更高版本 |
| `[remote_input]` | `enabled` / `sensitivity` / `max_minutes` | 跨屏键鼠总开关、灵敏度、单次最长分钟数 |
| `[qq]` | 见下 | QQ 远程通道 |

### `[qq]` 段

| 键 | 说明 |
| --- | --- |
| `enable` | 总开关，默认关闭。打开后是「已启用 · 未连接」，要手动点「连接」才建连 |
| `auto_connect` | 后端启动时自动建连；默认关闭，避免 NapCat 没开时空转 |
| `ws_url` | NapCat WebSocket 地址；本机调试 `ws://127.0.0.1:3001`，公网用 `wss://` |
| `access_token` | 必填，必须与 NapCat 侧一致；留空时 QQ 通道不建立连接（后端与其它入口照常运行） |
| `admin_qq` | 管理员 QQ 号列表，只有这些人能下指令 |
| `allow_groups` | 允许响应的群号；空 = 所有群一律不响应，主人 QQ 也不能绕过，私聊不受影响 |
| `proactive_chance` | 群里没人 @ 时自己插话的概率（0~100），默认 0 |
| `redact_words` / `redact_mask` | 敏感词替换，默认掩码 `***`；不分大小写与全半角，单字词忽略 |
| `guest_prompt` | 群友专用提示词，可写 `{name}`；留空用内置受限版（公开人设） |
| `allow_image` / `max_image_mb` / `image_recent_minutes` | 是否处理 QQ 图片；单张上限默认 8 MB；最近 2 分钟内的图跟着聊天轮次带上。下载只放行公网 http(s)，挡住内网地址与重定向绕道 |
| `voice_reply` / `voice_group_members` / `voice_only` | 回复附语音条：私聊只对管理员生效，群聊可放开给群成员；`voice_only` 发送失败时改发文字 |
| `group_memo` | 群常识只读缓存；默认关闭，且要求群在白名单里 |
| `auto_launch_after_install` / `auto_launch_wait_seconds` | 安装类命令成功后自动拉起程序，等待后截图回报 |

其余限流、拆条、确认超时、重连退避等键见 [config.example.toml](server/config.example.toml)。

### 截图与 vision

截图询问、截图翻译和「带画面发送」都要求当前模型支持看图。模型能力可在设置页的校验入口查看；不支持时会退化成纯文字回答。

### 语音开关

语音输入默认开启，可在 QAM / 大屏 / 网页设置里切换（写入 `[stt] enable`）；首次使用会下载识别模型，失败只影响语音，不影响其它功能。语音播报在 `[tts] enable` 或设置里打开，`[tts] speaker` 选发音人：机器人（espeak-ng，需系统包）、月读酱（piper-plus，需模型）、哔哔啵啵（纯代码合成，不需要任何外部依赖）。

---

## 安装

### 前置

- SteamOS / Steam Deck（其它 Linux 桌面也能跑）。
- Python 3.11+ 与 `venv`（SteamOS 需先开启开发者模式）。
- 可选：Decky Loader。只有想在游戏模式 QAM 里用、或用智能音箱点歌才需要；音乐还需要额外安装 Decky Music 插件。
- **不要用 sudo 运行 `install.sh`**。脚本装到当前用户家目录；控制类系统包由脚本单独调 `sudo` 安装；Decky 插件本身以 root 运行。

### 其它入口

```bash
bash install.sh menu                 # 交互菜单：安装 / 升级 / 卸载
bash install.sh install --deps       # 顺带用 sudo 装控制类控件（默认就装，可省略）
bash install.sh install --no-deps    # 跳过控制类系统包
bash install.sh upgrade --gui        # 升级代码后只补装桌面客户端（PySide6）
bash install.sh from-mirror          # 从镜像站拉最新版覆盖安装
bash install.sh check                # 逐个实测控制类控件
bash install.sh check --fix          # 自检并修复（含 ydotoold 用户服务）
bash install.sh speedtest            # 只测 pip 源速率
bash install.sh uninstall --dry-run  # 预览卸载会动哪些路径（不实际执行）
```

安装器没有单独的 `--stt` / `--tts` 开关，重跑 `bash install.sh` 会自动补齐缺失的运行时；OCR 组件在安装时按提示选择下载。常用环境变量：`DECKMEIDO_DIR`（安装目录）、`DECKMEIDO_WORKSPACE`、`DECKMEIDO_PORT`、`DECKMEIDO_MIRROR`、`DECKMEIDO_PIP_INDEX`、`DECKMEIDO_SKIP_GUI`、`DECKMEIDO_NO_DEPS`、`DECKMEIDO_NO_SPEAKER`。

### 首次安装做了什么

1. 预检 Python 版本（3.11+）、venv、`systemctl`、磁盘余量、端口占用与图形会话。
2. 停掉旧的后端 / 客户端进程，把程序同步到安装目录（默认 `~/DeckMeido`，可用 `DECKMEIDO_DIR` 覆盖）。
3. 创建数据根 `userdata/` 及 `persona/`、`sessions/`、`uploads/`、`meidobot/`、`logs/`、`models/`，目录权限 `700`。
4. 检测控制类依赖（spectacle、ydotool、parec 等），用 `sudo` 补装；`--no-deps` 可跳过。
5. 建 `.venv` 并安装 Python 依赖；默认装 PySide6（`DECKMEIDO_SKIP_GUI=1` 跳过），语音运行时按需补齐；OCR 组件按提示选择下载。
6. 从 `server/config.example.toml` 生成 `userdata/config.toml`，权限 `600`；随后做功能自检。
7. 写桌面快捷方式与 `uninstall.sh`；除 `ydotool` 包自带的用户级 `ydotoold` 服务外，**不生成、不安装任何 systemd 单元**，后端只由客户端与插件拉起。
8. 检测到 Decky Loader 时询问是否安装 QAM 插件，然后启动客户端。没装 Decky Loader 时这一步自动跳过，插件 zip 留在工作区。

### Decky 插件与桌面客户端

安装器发现 Decky 会询问，同意后把 `deploy/deckmeido-plugin.zip` 解到 `~/homebrew/plugins/DeckMeido`，并打印「插件已安装 v…」。到 **设置 → Decky Loader → 重新载入** 即可在 QAM 看到。没装 Decky 会整段跳过，QAM 里没有入口属预期；之后想装可先装 Decky 再执行 `bash install.sh upgrade`，或运行 `bash ~/DeckMeido/plugin/deploy.sh`。

装完会自动打开一次客户端，同时拉起后端。之后双击桌面上的 DeckMeido 图标即可；窗口最小化驻留托盘，右键菜单可退出。

---

## 数据目录

所有可修改数据统一在数据根下，默认 `<安装目录>/userdata`，可用环境变量 `DECKMEIDO_DATA_DIR` 覆盖；服务端找不到数据根会直接报错，不会静默换目录。

```text
userdata/
├── config.toml              600
├── persona/meidosoul.json   人设：名字、性格、称呼、语气
├── presets/presets.json     游戏提示词预设（带 schema 版本号）
├── memory.json              长期记忆（条目自带 scope）
├── sessions/                会话记录
├── trash/                   被删会话的软删除副本（保留最近 20 份，可撤销）
├── uploads/                 网页 / QQ 上传的图片与文件
├── wallpapers/              自定义壁纸（挂到网页 /wallpapers）
├── voice/                   QQ 语音条的本地副本，供网页 / 客户端回放
├── models/sensevoice/       语音识别模型（首次使用时按需下载）
├── models/tts/tsukuyomi/    语音合成模型（首次使用时按需下载）
├── models/wake/             唤醒词模型（首次启用按需下载）
├── usage/<日期>.json        按北京时间自然日聚合的 token 用量
├── logs/
│   ├── backend.log          后端日志（由后端轮转，保留 backend.log.1/.2）
│   ├── overlay.log          操作遮罩日志
│   ├── client-crash.log     客户端崩溃栈
│   └── debug-<日期>.log     开发者模式日志（5 MB × 2）
├── core.stopped             插件「关闭核心」标记，与客户端共享
└── meidobot/                QQ 归档
    ├── images/<日期>/       存图
    ├── inbox_index.jsonl    图片索引
    └── text_log.txt         引用保存的文本
```

- 目录权限 `700`、`config.toml` 权限 `600`（安装器创建时收紧）。
- 安装器只维护这一套标准布局；数据根位置由 `DECKMEIDO_DATA_DIR`（默认 `<安装目录>/userdata`）决定。
- 卸载时默认把 `userdata/` 整体搬到 `~/MeidoSpace/userdata-<时间>`，校验文件数与字节数通过后才删代码；`--purge` 才会一并清掉。

---

## 升级与卸载

**升级**

- **软件内**：设置 → 关于更新 → 检查更新 → 立即升级到最新版（AI 也能用 `self_update`）。检查完没有新版本会写「已是最新版本」，有更新则逐条列出改动摘要。下载后校验 sha256，失败整份回滚；配置、会话、工作区不受影响，完成后自动重启。
- **双源**：默认同时探测国内镜像与 GitHub Release，取版本更高者，同版本优先镜像；`[update] github_enable = false` 则只用镜像。
- **命令行**：`bash ~/DeckMeido/install.sh upgrade`，会先停客户端与后端，再同步代码与依赖；`--deps` 顺带检查控制类控件，`--gui` 只补装桌面客户端。

**卸载**

```bash
bash ~/DeckMeido/uninstall.sh            # 卸载；userdata 先搬到 ~/MeidoSpace/userdata-<时间>
bash ~/DeckMeido/uninstall.sh --dry-run  # 只打印会动哪些路径，不做修改
bash ~/DeckMeido/uninstall.sh --purge    # 连 userdata、工作区与配置一起清掉
```

- 卸载**只移入回收站**（`gio trash`），不做永久删除，误删可从回收站找回。
- 默认先把 `userdata/`（人设、会话、记忆、QQ 归档、模型）搬到 `~/MeidoSpace/userdata-<时间>`，校验数量与字节后才删代码；`--purge` 才把 `userdata/`、`[workspace] dir` 与安装器创建的配置目录一并进回收站。
- 会询问是否一起卸载安装器装过的系统包（回车 = 卸载，`n` = 保留）；随后清理 Decky 插件、桌面快捷方式与缓存目录。插件按「停止 → 等残留 → 启动」重载，不会硬 `restart`。
- `bash install.sh uninstall` 只允许对着带 `.deckmeido-install` 标记的安装目录执行；源码工作区（含 `.git`）会被守卫拒绝，避免误删 clone 下来的仓库。

---

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 插件版本没变 | 看安装日志里是否有「插件已安装 v…」；到 设置 → Decky Loader 点「重新载入」，或运行 `bash ~/DeckMeido/plugin/deploy.sh` |
| QAM 里没有入口 | 先确认装没装 Decky Loader：没装时插件整段跳过、QAM 不会有入口，属预期；装好 Decky 后运行 `bash ~/DeckMeido/plugin/deploy.sh`（或 `bash ~/DeckMeido/install.sh upgrade`），再到 设置 → Decky Loader 点「重新载入」 |
| 后端没起来 | 看 `~/DeckMeido/userdata/logs/backend.log`（轮转备份 `backend.log.1/.2`，只在后端运行时产生）；确认 `18765` 没被别的进程占用 |
| 配置写坏了 | 删除 `userdata/config.toml` 后重启，会按模板重新生成（自定义项会丢）；人设 JSON 坏了会自动回落默认人设 |
| 模型连不上 | 先看离线模式是否打开；本地端点连接超时约 2 秒，读超时按配置。`/api/net/probe` 可判断当前只通局域网还是能到公网 |
| QQ 连不上 | 网页 QQ 状态页看 `detail`；确认 `access_token` 非空且与 NapCat 一致、`ws_url` 可达、机器人已登录 |
| 截图 / 点击不灵 | 跑 `bash install.sh check`；`ydotoold` 没运行会让 ydotool 报 socket 错误 |
| 升级后打不开 | 多半是旧后端占着端口；再跑一次 `bash install.sh upgrade` 会清理残留进程 |
| 唤醒没反应 | 确认 `[wake] enable`、模型已下载完、`parec` / `pw-record` 能录到声音；环境噪声大或唤醒词说得太轻会漏，可换更清楚的唤醒词或调低阈值 |
| 点歌没声音 | 确认 Decky Music 已安装并登录、播放器已在播放；`busctl` 能列出 MPRIS、Loader 的 127.0.0.1:1337 RPC 可达。没装插件时只会回一句原因，不会崩 |
| 语音输入 / 播报不工作 | 看设置页语音状态和 `backend.log`；识别需要 sherpa-onnx 与 SenseVoice 模型，播报按 `[tts] speaker` 要对应运行时（robot 要 `espeak-ng`、maid 要 piper-plus 与模型、bibibo 不依赖任何东西） |
| 壁纸不显示 | 确认文件 ≤ 20 MB 且是 PNG / JPG / WEBP / GIF / BMP；设置 → 外观 里重新选一次；看 `userdata/wallpapers/` 是否可写 |
| 睡眠模式没触发 | 确认 `[sleep] enabled`；只认「音乐已停 + 会话空闲」同时满足，玩游戏或放歌时不会触发；倒计时期间有输入就会取消 |
| OCR 没生效 | OCR 是可选组件，没装时纯文本模型看图会退化；重跑 `bash install.sh` 选择安装，看 `backend.log` 里的导入报错 |

日志位置：后端 `userdata/logs/backend.log`、遮罩 `userdata/logs/overlay.log`、客户端崩溃栈 `userdata/logs/client-crash.log`；开发者模式另有 `userdata/logs/debug-*.log`；QQ 事件可在状态页查看，也可调 `/api/qq/logs`；依赖安装日志 `pip-install.log` 在安装目录。

---

## 开发

```text
DeckMeido/
├── install.sh        安装 / 升级 / 卸载 / 镜像安装 / 自检 / 测速
├── server/           FastAPI 后端：agent 循环、工具、LLM、会话、记忆、QQ、STT/TTS、外观、静态前端
├── client/           PySide6 客户端（托盘、单实例、后端守护）
├── plugin/           Decky 插件（QAM + 大屏页）
├── deploy/           桌面项、插件安装包
├── docs/             设计与需求笔记
├── tests/            unittest 用例
├── tools/            发版与维护脚本
├── winres/           发版产物同步目录（只保留最新一版）
└── website/          官网静态页
```

```bash
python -m unittest discover -s tests   # 当前 885 项
cd plugin && npm run build              # 构建插件前端到 plugin/dist
python tools/release.py 0.8.23          # 构建 dist/：tar.gz + 插件 zip + latest.json
```

依赖分四份：`requirements-gui.txt`（桌面客户端）、`requirements-stt.txt`（识别 / 唤醒）、`requirements-tts.txt`（合成）、`requirements-ocr.txt`（OCR 可选组件，模型单独下载，不随发布包）。

发版：`release.py` 只写 `dist/` 并自动同步到 `winres/deckmeido/`；该目录**只保留最新版本产物**，同步时会清掉其它版本的 tar/zip 与 `.sha256`，`install.sh`、`latest.json`、`SHA256SUMS` 常驻保留。提交后打 tag，用 `gh release create` 上传 tar.gz、插件 zip、sha256、`latest.json` 与 `install.sh`。

---

## 许可

本项目采用 [CC BY-NC 4.0](LICENSE)（署名—非商业性使用 4.0 国际）协议发布：可自由复制、修改、分发，但须署名，且不得用于商业用途。

### 第三方组件与语音署名

- **语音合成模型**：[Piper Plus つくよみちゃん](https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan)（`tsukuyomi-chan-6lang-fp16.onnx`，ja/en/zh/es/fr/pt 六语）。模型不随发布包分发，首次使用时下载到 `userdata/models/tts/tsukuyomi/`；旧 root 版基座模型许可为 CC BY 4.0。
- **推理程序**：[ayutaz/piper-plus](https://github.com/ayutaz/piper-plus)，MIT License。
- **哔哔啵啵音色**：参考 路墨小剧场 的纯代码合成实现（MIT License），不依赖任何模型文件。
- **OCR**：[RapidOCR](https://github.com/RapidAI/RapidOCR)（Apache License 2.0）与 PP-OCR 模型；依赖在 `requirements-ocr.txt`，模型随组件下载，不内置进仓库与发布包。
- **语音 / 唤醒推理运行时**：[k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)，Apache License 2.0；`requirements-stt.txt` 安装，模型按需下载。
- **音乐播控**：[Decky Music](https://github.com/jinzhongjia/decky-music)，MIT License（Copyright © 2026 jinzhongjia），支持 QQ 音乐与网易云音乐。本项目不打包它，只在运行时通过它注册的 MPRIS2 和 Decky Loader 本地 RPC 控制播放；需要用户自行安装并登录音乐服务。
- **语音库署名（必须保留，原文照抄）**：
  - 本ソフトウェアの音声合成には、フリー素材キャラクター「つくよみちゃん」（© Rei Yumesaki）が無料公開している音声データを使用しています。
  - ■つくよみちゃんコーパス（CV.夢前黎） <https://tyc.rei-yumesaki.net/material/corpus/>
  - ■Piper Plus:つくよみちゃん <https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan>
- **生成语音的使用条件**（[つくよみちゃんコーパス利用規約 ③](https://tyc.rei-yumesaki.net/material/corpus/#terms3)）：
  - 商用限制：个人/法人、营利/非营利均可，**模型与语音本身无商用限制**；这与本项目自身的 CC BY-NC 4.0 是两件事，发布包仍按非商用授权。
  - 公开生成语音（视频/直播/演讲等）时，须标注「Piper Plus:つくよみちゃん」。
  - 禁止事项（原文）：
    - 人を批判・攻撃すること。
    - 特定の政治的立場・宗教・思想への賛同または反対を呼びかけること。
    - 刺激の強い表現をゾーニングなしで公開すること。
    - 他者に対して二次利用（素材としての利用）を許可する形で公開すること。
  - 语料本体与模型权重不得再分发；本项目只在运行时下载，不内置进插件 zip / release 资产；派生分发须继承同一条款。

---

<p align="center"><sub>DeckMeido · 为你守着这台掌机的女仆</sub></p>
