<p align="center">
  <img src="assets/logo.png" width="160" alt="DeckMeido">
</p>

<h1 align="center">DeckMeido</h1>

<p align="center">
  <b>住在 Steam Deck 里的 AI 女仆</b><br>
  手柄优先，QAM 与大屏两种形态，还能从 QQ 远程使唤她
</p>

<p align="center">
  <a href="https://github.com/Congmeims/DeckMeido/releases"><img src="https://img.shields.io/github/v/release/Congmeims/DeckMeido?style=flat-square&color=1f6feb" alt="Release"></a>
  <a href="https://github.com/Congmeims/DeckMeido/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-CC%20BY--NC%204.0-2ea44f?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-SteamOS%20%7C%20Linux-1f6feb?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.11+-3776ab?style=flat-square" alt="Python">
</p>

<p align="center">
  <a href="#安装">安装</a> ·
  <a href="#功能">功能</a> ·
  <a href="#首次配置">配置</a> ·
  <a href="#qq-用法">QQ</a> ·
  <a href="#故障排查">排查</a>
</p>

---

## 这是什么

跑在掌机本机上的 AI Agent。她不只会聊天——能截当前画面看图答问、按坐标点击、注入手柄、装游戏、执行命令，也能在 QQ 里接受远程指令。

**核心亮点**

- **手柄优先**：QAM 面板里 LB/RB 翻页，X 翻译、Y 截图询问、R2 按住说话；大屏页同样用手柄操作。
- **两种形态**：游戏内 QAM + 大屏路由，桌面模式有原生客户端（可驻留托盘）与网页控制台。
- **离线语音**：识别在本机跑，不联网；回复可用 Deck 内置 TTS 念出来。
- **QQ 远程**：NapCat OneBot 11 接入，管理员私聊或白名单群里 @ 她就能下指令，高危操作有独立风险档位。
- **真的能动手**：Steam 库、商店、截图、窗口聚焦、虚拟手柄、文件读写、终端都在工具层。

## 功能

### 多入口

| 入口 | 说明 |
| --- | --- |
| QAM 面板 | 三个页签「询问 / 聊天 / 设置」，窄屏纵向布局，L1/R1 翻页（页签两侧有 `‹ L1` / `R1 ›` 提示） |
| 大屏页 | 注册到 GamepadUI 路由，三个页签「询问 / 聊天 / 设置」；L1/R1 翻页，X 翻译、Y 截图询问，聊天页按住 R2 说话 |
| 桌面客户端 | PySide6 + QWebEngineView，加载与网页版同一个界面；最小化进托盘，双击桌面图标启动 |
| 网页控制台 | 浏览器打开 `http://<deck-ip>:18765/`，单文件前端，功能与客户端一致；首次打开点「配对新设备」，按掌机上的 PIN 配对一次 |

QAM 面板由 Decky 插件提供；插件以 root 运行，走本机回环免鉴权，不再需要令牌。大屏页从 QAM 直接跳转。

### 对话与画面

- **询问**：单轮问答，可带当前画面；游戏名由插件自动取到并写进提示词。
- **聊天**：多轮会话，流式输出、思维链、上下文自动压缩、长期记忆。
- **截图询问**：抓当前画面 + 游戏名，按 `[agent] ask_template` 回答「这一步怎么过」，不剧透。
- **截图翻译**：抓画面按 `[agent] translate_template` 原文照抄后逐句翻译。
- **可改提示词**：询问模板、翻译模板、状态栏短句提示词都能在页面或插件设置里改，保存即时生效。
- **模型可换**：设置页可拉取模型列表并切换，兼容任意 OpenAI 接口（DeepSeek / GPT / Claude 中转 / Qwen / LM Studio 等）。

### 语音

- **语音输入**：sherpa-onnx + SenseVoice-Small int8，纯 CPU 离线识别，中英混说带标点；模型压缩包约 1GB（只解出约 250MB），首次按键时按需下载到 `userdata/models/sensevoice/`，闲置一段时间自动释放。
- **语音播报**：默认关闭；主引擎是纯 CPU 离线的 piper-plus + つくよみちゃん 6 语模型（ja/en/zh/es/fr/pt，约 38 MB，首次使用时下载到 `userdata/models/tts/tsukuyomi/`，不进发布包），运行时装不上或模型加载失败时自动回落 `espeak-ng`；可设语言、语速与单次字数上限。

### 设备操作

- **执行命令**：`manual` 全部审批、`auto` 只读放行 / 危险拦一道、`full` 不审批。
- **桌面操作**：按窗口名聚焦、按坐标点击、键盘输入；Wayland 下优先 klipper 剪贴板粘贴，其次 ydotool。
- **虚拟手柄**：后端工具层写 `/dev/uinput` 模拟 Xbox 360 手柄，游戏模式也能用。
- **截图**：桌面模式走 spectacle/KWin，游戏模式走 gamescope PipeWire 抓帧。
- **Steam**：列已装游戏、商店搜索、按 appid 安装、检查系统更新；安装属危险操作，强制确认。
- **文件与终端**：工作区内浏览、读写、精确替换；网页终端与文件预览；模拟器 ROM 扫描。

### QQ 远程

- **OneBot 11 / NapCat**：Deck 主动连 NapCat 的 WebSocket 服务器，本机不开入站端口。
- **触发方式**：私聊直接发；群里必须 @ 机器人。指令前可带昵称前缀（默认 `meido`），也支持整句口语别名。
- **权限**：`admin_qq` 里的号在私聊、任意群都可直接下指令（无需 @、不受群白名单与群开关限制，危险操作仍要确认）；普通群成员始终只读聊天（零工具、不读记忆、输出脱敏）。
- **风险三档**：`deny` 直接拒绝、`hard_confirm` 只认「确认执行」、`normal` 走普通确认。
- **归档**：可存图、存文本，图片进 `meidobot/images/<日期>/`，索引与文本分别写入 `inbox_index.jsonl`、`text_log.txt`。

### 跨屏键鼠

在电脑浏览器里点网页左下角 DeckMeido logo，进入 Pointer Lock，把鼠标键盘交给 Deck；再点 logo、按 Esc 或到时间上限即释放。

## 安装

### 前置

- SteamOS / Steam Deck（其它 Linux 桌面也能跑）。
- Python 3.11+ 与 `venv`（SteamOS 需先开启开发者模式）。
- 可选：Decky Loader。只有想在游戏模式 QAM 里用才需要。
- **不要用 sudo 运行 `install.sh`**。脚本装到当前用户家目录；控制类系统包由脚本单独调 `sudo` 安装；Decky 插件本身以 root 运行。

### 命令

```bash
git clone https://github.com/Congmeims/DeckMeido.git && cd DeckMeido
bash install.sh
```

GitHub 连不上时走国内镜像：

```bash
curl -fsSL -o /tmp/dm.sh https://meido.meagames.cn/deckmeido/install.sh
bash /tmp/dm.sh from-mirror
```

其它入口：

```bash
bash install.sh menu                 # 交互菜单：安装 / 升级 / 卸载
bash install.sh install --deps       # 顺带用 sudo 装控制类控件
bash install.sh install --gui        # 只补装桌面客户端（PySide6）
bash install.sh from-mirror          # 从镜像站拉最新版覆盖安装
bash install.sh check                # 逐个实测控制类控件
bash install.sh speedtest            # 只测 pip 源速率
```

### 首次安装做了什么

1. 预检 Python 版本、venv、`systemctl`、磁盘余量、端口占用与图形会话。
2. 把程序同步到安装目录（默认 `~/DeckMeido`，可用 `DECKMEIDO_DIR` 覆盖；旧版 `~/deckmeido` 会被识别并原地升级，可用 `bash install.sh migrate` 改名）。
3. 创建数据根 `userdata/` 及 `persona/`、`sessions/`、`uploads/`、`meidobot/`、`logs/`、`models/`，目录权限 `700`。
4. 建 `.venv` 并安装依赖；默认装 PySide6，`DECKMEIDO_SKIP_GUI=1` 可跳过。
5. 从 `server/config.example.toml` 生成 `userdata/config.toml`，权限 `600`。
6. 写桌面快捷方式与 `uninstall.sh`；**不生成、不安装任何 systemd 单元**，后端只由客户端与插件拉起。
7. 检测到 Decky Loader 时询问是否安装 QAM 插件，然后启动客户端。

### Decky 插件

安装器发现 Decky 会询问，同意后把 `deploy/deckmeido-plugin.zip` 解到 `~/homebrew/plugins/DeckMeido`，并打印「插件已安装 v…」。到 **设置 → Decky Loader → 重新载入** 即可在 QAM 看到。没装 Decky 会整段跳过；之后想装可先装 Decky 再执行 `bash install.sh upgrade`，或运行 `bash ~/DeckMeido/plugin/deploy.sh`。

### 桌面客户端

装完会自动打开一次客户端，同时拉起后端。之后双击桌面上的 DeckMeido 图标即可；窗口最小化驻留托盘，右键菜单可退出。

## 首次配置

网页或客户端打开后进「设置」，填模型地址与 Key 就能对话。也可以直接改 `userdata/config.toml`（改完重启后端生效，网页里改则立即生效）。

| 配置段 | 关键项 | 说明 |
| --- | --- | --- |
| `[server]` | `lan_enable` / `port` | 默认 `false` / `18765`；局域网鉴权用设备配对（PIN + HttpOnly Cookie），本机回环免验证；旧配置里的 `token` 键会被忽略 |
| `[llm]` | `base_url` / `api_key` / `model` | OpenAI 兼容地址与密钥；换服务商时模型名要一起改 |
| `[llm]` | `reasoning_effort` / `timeout` / `max_continuations` | 思维链强度、请求超时、撞上限自动续写轮数 |
| `[agent]` | `ask_template` / `translate_template` / `status_prompt` | 截图询问、截图翻译、状态栏短句的提示词 |
| `[agent]` | `context_window` / `compact_ratio` | 上下文窗口（0 = 按模型名推断）与压缩触发比例 |
| `[security]` | `permission_mode` | `manual` / `auto` / `full` |
| `[workspace]` | `dir` | 文件工具工作区；默认 `~/MeidoSpace` |
| `[tts]` | `enable` / `engine` / `voice` / `speed` | 语音播报开关与音色 |
| `[power]` | `keep_awake` | 后端运行期间抑制系统休眠 |
| `[update]` | `github_enable` / `mirror` / `github_repo` | 双源更新：默认镜像与 GitHub 都探测，取更高版本 |
| `[remote_input]` | `enabled` / `sensitivity` / `max_minutes` | 跨屏键鼠总开关、灵敏度、单次最长分钟数 |
| `[qq]` | 见下 | QQ 远程通道 |

### 截图与 vision

截图询问、截图翻译和「带画面发送」都要求当前模型支持看图。模型能力可在设置页的校验入口查看；不支持时会退化成纯文字回答。

### 语音开关

语音输入默认开启，可在 QAM / 大屏 / 网页设置里切换（写入 `[stt] enable`）；首次使用会下载识别模型，失败只影响语音，不影响其它功能。语音播报在 `[tts] enable` 或设置里打开，依赖 `espeak-ng`。

### `[qq]` 段

| 键 | 说明 |
| --- | --- |
| `enable` | 总开关，默认关闭 |
| `ws_url` | NapCat WebSocket 地址；本机调试 `ws://127.0.0.1:3001`，公网用 `wss://` |
| `access_token` | 必填，必须与 NapCat 侧一致；留空拒绝启动 |
| `admin_qq` | 管理员 QQ 号列表，只有这些人能下指令 |
| `allow_groups` | 允许普通群成员聊天的群号；空 = 不处理群友消息，主人 QQ 不受此限 |
| `guest_prompt` | 群友专用提示词，可写 `{name}`；留空用内置受限版 |
| `allow_image` | 是否处理 QQ 图片（下载、看图、存档） |

其余限流、拆条、确认超时、重连退避等键见 [config.example.toml](server/config.example.toml)。

## 数据目录

所有可修改数据统一在数据根下，默认 `<安装目录>/userdata`，可用环境变量 `DECKMEIDO_DATA_DIR` 覆盖；服务端找不到数据根会直接报错，不会静默换目录。

```
userdata/
├── config.toml              600
├── persona/meidosoul.json   人设：名字、性格、称呼、语气
├── memory.json              长期记忆
├── sessions/                会话记录
├── uploads/                 用户上传与工作区文件
├── models/sensevoice/       语音识别模型（首次使用时按需下载）
├── logs/
│   ├── backend.log          后端日志（滚动保留 backend.log.1）
│   ├── overlay.log          操作遮罩日志
│   └── client-crash.log     客户端崩溃栈
├── core.stopped             插件「关闭核心」标记，与客户端共享
└── meidobot/                QQ 归档
    ├── images/<日期>/       存图
    ├── inbox_index.jsonl    图片索引
    └── text_log.txt         引用保存的文本
```

- 目录权限 `700`、`config.toml` 权限 `600`（安装器创建时收紧）。
- 安装器只维护这一套标准布局；数据根位置由 `DECKMEIDO_DATA_DIR`（默认 `<安装目录>/userdata`）决定。

## 卸载

用安装目录里的卸载脚本：

```bash
bash ~/DeckMeido/uninstall.sh            # 按清单删安装器文件，保留 userdata/
bash ~/DeckMeido/uninstall.sh --purge    # 额外删安装器创建的配置备份（人设/记忆备份）
```

- 卸载按 `.deckmeido-manifest` 逐项删除，清单外的文件与目录都不动；`userdata/` 整目录保留，最后打印归档路径。
- 只有目录为空（只剩 `userdata/` 也算非空）才会 `rmdir`，否则打印「保留目录：…（含未识别文件）」。
- **`install.sh uninstall` 会被守卫拒绝**：脚本拒绝删除源码工作区（含 `.git`）、脚本所在目录及其上级，也拒绝没有 `.deckmeido-install` 标记的目录。不要对着 clone 下来的仓库执行卸载；要卸载就装完后的安装目录脚本。
- `--purge` 只删除安装器创建、且带 `.deckmeido-config` 标记的配置目录（逐项）；工作区、`$HOME`、`/` 一律拒绝，`userdata/` 永不删除。
- 需要强删无标记目录时才设 `DECKMEIDO_ALLOW_UNSAFE_UNINSTALL=1`，它也只允许删清单文件，不会 `rmdir`。

## 升级

- **软件内**：设置 → 关于 → 检查更新 → 升级。下载后校验 sha256，失败整份回滚；配置、会话、工作区不受影响，完成后自动重启。
- **双源**：默认同时探测国内镜像与 GitHub Release，取版本更高者，同版本优先镜像；`[update] github_enable = false` 则只用镜像。
- **命令行**：`bash ~/DeckMeido/install.sh upgrade`，会先停客户端与后端，再同步代码与依赖；`--deps` 顺带检查控制类控件。

## QQ 用法

触发前缀就是软件里配置的昵称，默认 `meido`。私聊管理员直接发；群里必须 @ 机器人。

| 操作 | 斜杠指令 | 口语别名示例 |
| --- | --- | --- |
| 查询状态 | `/status` | `状态`、`在吗`、`怎么样了` |
| 截屏 | `/shot` | `截个屏`、`看看屏幕`、`拍张照` |
| 取消当前任务 | `/cancel` | `停`、`别做了`、`算了` |
| 存图 | `/save_image` | `存图`、`保存这张` |
| 存文本 | `/save_text` | `保存这段`、`存这段文字` |
| 检查依赖 | `/deps` | `检查依赖`、`查依赖`、`依赖状态` |
| 指令菜单 | `/help` | `菜单`、`帮助`、`有什么指令` |

固定指令命中后直接执行既有动作，不经过 AI。`/deps` 只检测并回报依赖状态，不会安装任何东西、不改设备环境。其余文本按聊天处理，可让她执行设备操作。优先级：斜杠指令 > 取消 > 状态 > 截图 > 聊天。

**管理员与群友**：管理员在私聊与任意群都能下指令并触发设备操作（无需 @，危险操作仍要确认）；普通群成员只读聊天，这一轮零工具、不读主人记忆、不带截图，输出还会做脱敏。

**风险控制**：

- `deny`：`rm`、`rmdir -r`、`shred`、`dd`、`mkfs`、分区写入、写 `/dev` 块设备、fork bomb、写系统关键文件等，远程一律拒绝。
- `hard_confirm`：`sudo`、下载后管道给 shell、`/etc` 或 `/usr` 写入、包管理器安装卸载、`crontab` 修改、非电源类 `systemctl restart` 等，必须回复「确认执行」。
- `normal`：其余设备操作，按普通确认流程走。

内置指令之外的设备操作，先按 `[security] permission_mode` 判断本机权限，再叠加远程风险档位。

## 跨屏键鼠

1. 网页设置里打开 `[remote_input] enabled`（会先弹一次安全确认）。
2. 在电脑浏览器点网页左下角 logo 进入接管；指针锁定后移动与按键都发往 Deck。
3. 再点 logo、按 Esc（浏览器会强制释放 Pointer Lock）或到达 `max_minutes` 自动释放。

注入优先 `ydotool`（由 `ydotoold` 用户服务托管），不可用时直写 `/dev/uinput`——这要求后端有 root 或 `input` 组权限。只有已配对设备能接管，且建议只在可信局域网开启。

## 故障排查

| 现象 | 排查 |
| --- | --- |
| 插件版本没变 | 看安装日志里是否有「插件已安装 v…」；到 设置 → Decky Loader 点「重新载入」，或运行 `bash ~/DeckMeido/plugin/deploy.sh` |
| 后端没起来 | 看 `~/DeckMeido/userdata/logs/backend.log`（轮转备份在 `backend.log.1`）；确认 `18765` 没被别的进程占用 |
| 配置写坏了 | 删除 `userdata/config.toml` 后重启，会按模板重新生成（自定义项会丢）；人设 JSON 坏了会自动回落默认人设 |
| QQ 连不上 | 网页 QQ 状态页看 `detail`；确认 `access_token` 非空且与 NapCat 一致、`ws_url` 可达、机器人已登录 |
| 截图 / 点击不灵 | 跑 `bash install.sh check`；`ydotoold` 没运行会让 ydotool 报 socket 错误 |
| 升级后打不开 | 多半是旧后端占着端口；再跑一次 `bash install.sh upgrade` 会清理残留进程 |

日志位置：后端 `userdata/logs/backend.log`、遮罩 `userdata/logs/overlay.log`、客户端崩溃栈 `userdata/logs/client-crash.log`；QQ 事件可在状态页查看，也可调 `/api/qq/logs`；依赖安装日志 `pip-install.log` 在安装目录。

## 开发

```
DeckMeido/
├── install.sh        安装 / 升级 / 卸载 / 镜像安装 / 自检 / 测速
├── server/           FastAPI 后端：agent 循环、工具、LLM、会话、记忆、QQ、STT/TTS、静态前端
├── client/           PySide6 客户端（托盘、单实例、后端守护）
├── plugin/           Decky 插件（QAM + 大屏页）
├── deploy/           服务单元、桌面项、插件包
├── tests/            unittest 用例
├── tools/            发版与维护脚本
└── website/          官网静态页
```

```bash
py -3 -m unittest discover -s tests     # 当前 197 项
cd plugin && npm run build              # 构建插件前端到 plugin/dist
python tools/release.py 0.7.1           # 构建 dist/：tar.gz + 插件 zip + latest.json
```

发版：`release.py` 只写 `dist/` 并自动同步到 `winres/deckmeido/`；该目录**只保留最新版本产物**，同步时会清掉其它版本的 tar/zip 与 `.sha256`，`install.sh`、`latest.json`、`SHA256SUMS` 常驻保留。提交后打 tag，用 `gh release create` 上传 tar.gz、插件 zip、sha256、`latest.json` 与 `install.sh`。

## 许可

本项目采用 [CC BY-NC 4.0](LICENSE)（署名—非商业性使用 4.0 国际）协议发布：可自由复制、修改、分发，但须署名，且不得用于商业用途。

### 第三方组件与语音署名

- **语音合成模型**：[Piper Plus つくよみちゃん](https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan)（`tsukuyomi-chan-6lang-fp16.onnx`，ja/en/zh/es/fr/pt 六语）。模型不随发布包分发，首次使用时下载到 `userdata/models/tts/tsukuyomi/`；旧 root 版基座模型许可为 CC BY 4.0。
- **推理程序**：[ayutaz/piper-plus](https://github.com/ayutaz/piper-plus)，MIT License。
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
