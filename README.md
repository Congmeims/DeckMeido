<p align="center">
  <img src="assets/logo.png" width="160" alt="DeckMeido">
</p>

<h1 align="center">DeckMeido</h1>

<p align="center">
  <b>住在 Steam Deck 里的 AI 女仆</b><br>
  会聊天、查攻略、开游戏、点屏幕 —— 她替你动手，还能把回复念给你听
</p>

<p align="center">
  <a href="https://github.com/Congmeims/deckmeido/releases"><img src="https://img.shields.io/github/v/release/Congmeims/deckmeido?style=flat-square&color=1f6feb" alt="Release"></a>
  <a href="https://github.com/Congmeims/deckmeido/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-CC%20BY--NC%204.0-2ea44f?style=flat-square" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-SteamOS%20%7C%20Linux-1f6feb?style=flat-square" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.11+-3776ab?style=flat-square" alt="Python">
</p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="#能做什么">能力</a> ·
  <a href="#设计取舍">设计取舍</a> ·
  <a href="#配置">配置</a> ·
  <a href="#常见问题">FAQ</a>
</p>

---

## 这是什么

一个跑在掌机本机上的 AI Agent。它不是聊天网页——它能**真正操作这台机器**：启动 Chrome 打开网页、在输入框里打字、点击按钮、截图确认结果、翻 Steam 库、扫模拟器 ROM、跑 shell 命令。

一套界面两种打开方式：浏览器里的 Web 控制台，和 PySide6 封装的原生客户端（可最小化到托盘）；游戏模式下还能通过 Decky 插件在侧栏里直接对话。

> **Meido**（メイド）是日语「女仆」的读音。人格可改，默认设定是礼貌周全、称你为「主人」的日系女仆。

## 快速开始

```bash
git clone https://github.com/Congmeims/deckmeido.git && cd deckmeido && bash install.sh
```

GitHub 连不上时走国内镜像（同一份代码）：

```bash
curl -fsSL -o /tmp/dm.sh https://meido.meagames.cn/deckmeido/install.sh
bash /tmp/dm.sh from-mirror
```

装完脚本会打印带令牌的访问地址（默认 `http://<deck-ip>:18765/?token=meido`），首次打开自动弹设置页，填 **API 地址**和 **Key** 即可对话。也可以手机扫码直连。

> 不要用 `sudo` 执行。程序装到当前用户家目录（默认 `~/deckmeido`），root 装会落到 `/root` 下，桌面会话里起不来。

装不下来？先在 Deck 上测一下哪个 pip 源最快：

```bash
bash install.sh speedtest
```

想看菜单自己选「安装 / 升级 / 卸载」，不带参数也随时可以重跑（已装则走升级）：

```bash
bash install.sh menu
```

## 能做什么

| 类别 | 说明 |
| --- | --- |
| **桌面对话** | 流式回复 + 思维链、多会话、上下文自动压缩、长期记忆、图片上传、分享为长图 |
| **操作桌面** | 聚焦窗口、按坐标点击、打字、执行命令（超时保护 + 危险命令识别 + 三档权限） |
| **看屏幕** | 桌面模式 KWin 截图，游戏模式走 gamescope PipeWire 抓帧 |
| **Steam** | 列已装游戏、商店搜索、拉下载（危险操作，强制确认）、检查系统更新 |
| **游戏模式** | Decky 插件：QAM 面板对话（LT / RT 翻页）、截屏提问、截图翻译、虚拟手柄注入 |
| **编程 / 文件** | 工作区内的目录浏览与读写、精确替换编辑（`permission_mode` 决定能否越出工作区） |
| **自我维护** | 检查并升级自身、语音播报（TTS）、桌面模式下执行任务时阻止息屏 |
| **其它** | 联网搜索、天气、模拟器 ROM 扫描、系统信息 |

模型接任意 OpenAI 兼容后端：GPT、Claude（中转）、DeepSeek、Qwen、GLM、Kimi、本地 LM Studio 都行，默认 DeepSeek。

## 设计取舍

**遮罩只盖会动屏幕的那一步。** 点击、打字、滚动时才显示半透明遮罩（点一下即终止），跑命令、翻文件这类后台动作不遮视线。注入鼠标的几百毫秒内遮罩自动透明穿透，否则点击会被自己吃掉。遮罩期间按音量键也能中止。

**上下文压缩而不是丢弃。** 占用接近窗口上限时，把旧对话摘要成一条 system 消息（保留目标、偏好、已完成操作、待办），再拼上最近几轮原文。长任务不会因为跨度大而「失忆」。

**为 DeepSeek 前缀缓存让步。** 历史只追加、压缩只推进游标、旧截图换成一枚 `[SHOT:文件名]` 标记不再重发 base64、记忆段拼在 system prompt 最末尾——都是为了发出去的前缀每轮一致，命中硬盘缓存按低价计费。命中率打在日志里，悬停顶栏的上下文徽标也能看到。

**长期记忆是显式教学，不是后台自学。** 只有两种来源：对话里说「记住」时模型调 `remember` 工具，或在设置页手动写。没有后台定时总结——那会让上下文塞满噪声。

**游戏模式抓帧只有 2 帧、12 秒上限。** 早期取 6 帧、超时 25 秒，遇上 gamescope 不推帧就要卡满半分钟。现在超时就重新探节点再试一次，两次都失败会把 `pw-dump` 里的节点状态一并回给前端，而不是只吐一句「抓帧失败」。

**Web 与客户端同源。** 客户端不复制界面，直接用 QWebEngineView 加载 `127.0.0.1:18765`。改一处 HTML 两端同步。

## 配置

网页「设置」里改，保存即时生效，一般不用手改文件。

| 文件 | 内容 |
| --- | --- |
| `server/config.toml` | 端口、令牌、模型与 API、上下文压缩、权限模式、语音、更新源 |
| `server/meidosoul.json` | 人设：名字、性格、称呼、语气、禁用词 |
| `server/memory.json` | 长期记忆：用户画像 / 环境经验 / 知识条目 |

感兴趣的几个键：`[llm] reasoning_effort`（思考深度，`high` / `max`）、`[agent] context_window`（`0` = 按模型名推断）、`[update] source`（更新源，`mirror` / `github` / `mirror-only` / `github-only`）、`[security] permission_mode`（`manual` / `auto` / `full`）、`[workspace] dir`（文件工具的工作区，留空用 `~/deckmeido_workspace`）。完整表格见 [config.example.toml](server/config.example.toml)。

## 常用命令

```bash
bash install.sh                     # 不带参数：没装就装，已装就升级
bash install.sh menu                # 交互菜单：安装 / 升级 / 卸载
bash install.sh upgrade             # 升级：同步代码 + 依赖，保留配置与会话
bash install.sh upgrade --deps      # 升级后顺带检查控制类插件
bash install.sh install --gui       # 只补装桌面客户端（PySide6）
bash install.sh from-mirror         # 从镜像站拉最新版覆盖安装
bash install.sh speedtest           # 只测各 pip 源速率，不安装
bash install.sh check               # 自检：逐个实测控制类控件是否真能用
bash install.sh uninstall           # 卸载（保留配置与会话，加 --purge 全删）
bash install.sh service             # 生成 systemd 用户单元（默认不装，由客户端拉起后端）
```

升级也可以在网页「设置 → 关于」里点，会先备份、校验 sha256，失败整份回滚，配置和会话不受影响。更新源可以在同一页切换镜像站 / GitHub。

环境变量：`DECKMEIDO_DIR`（安装目录）、`DECKMEIDO_PORT`（端口）、`DECKMEIDO_MIRROR`（镜像站）、`DECKMEIDO_PIP_INDEX`（指定 pip 源，留空则自动测速）、`DECKMEIDO_SKIP_GUI=1`（不装 PySide6）、`DECKMEIDO_UPDATE_SOURCE`（安装时写进配置的更新源）、`DECKMEIDO_WORKSPACE`（工作区目录）。

## 目录结构

```
deckmeido/
├── install.sh          一键安装 / 升级 / 卸载 / 镜像安装 / 自检 / 测速
├── server/
│   ├── main.py         FastAPI 服务、agent 循环、权限审批、遮罩调度
│   ├── tools.py        工具定义与执行、工作区边界
│   ├── llm.py          OpenAI 兼容流式客户端
│   ├── session_store.py 会话落盘（单线程写、原子替换、压缩游标）
│   ├── memory.py       长期记忆
│   ├── persona.py      人设加载与回落
│   ├── prompts/        系统提示词（system / modes / runtime）
│   ├── overlay.py      半透明操作遮罩（PySide6）
│   ├── depcheck.py     控制类控件自检（ydotool / kdotool / wtype / espeak-ng）
│   ├── power.py        桌面模式下阻止息屏休眠
│   ├── singleton.py    后端单实例保护
│   ├── tts.py          语音播报（espeak-ng）
│   ├── updater.py      自升级：查版本、拉包、校验 sha256、回滚
│   └── static/         单文件 Web 控制台（index.html）+ 终端组件
├── client/             PySide6 原生客户端（托盘、单实例、崩溃守护）
├── plugin/             Decky 插件（QAM 面板）
├── assets/             图标与来源图（icon-src/）
├── deploy/             桌面快捷方式模板、systemd 单元、离线依赖（按需生成）
├── website/            官网静态页
└── tools/              发版打包、部署、证书与依赖抓取等仓库脚本（不进发布包）
```

发布包是 `dist/deckmeido-<版本>.tar.gz`，插件单独打成 `dist/deckmeido-plugin-<版本>.zip`（顶层 `DeckMeido/` 文件夹，Decky 商店 / 本地安装都能直接用）。

## 常见问题

**控制类插件是什么？** `ydotool`（模拟点击和打字）、`kdotool`（按窗口名聚焦）、`espeak-ng`（念出回复）。装它们需要 root，`install.sh` 结尾会问一次；跳过也能用，只是少了「动手」和「开口」的能力。随时补装：`bash install.sh install --deps`。

**SteamOS 更新后点击失灵？** 换镜像会冲掉 `/usr` 下 pacman 装的包，重跑 `bash install.sh install --deps` 即可。`bash install.sh check` 能逐个实测哪些真的没了。

**升级后聊天记录丢了？** 不应该发生——会话在 `server/sessions/`，升级不碰。如果确实丢了，多半是有两份后端抢同一端口，跑一次 `bash install.sh upgrade` 会清理残留进程。

**游戏模式下网页打不开？** 正常，SteamOS 游戏模式的浏览器不在。用 Decky 插件的 QAM 面板，或切回桌面模式。

**插件装完面板里什么都没有？** 多半是 zip 结构不对——Decky 要求顶层是 `DeckMeido/` 文件夹，文件直接摆在 zip 根目录会报 `No plugin.json found in plugin ZIP`。直接用 Release 里附带的 `deckmeido-plugin-*.zip`。

## 已知限制

- `desktop` 输入：优先走 klipper 剪贴板 + `Ctrl+V`，klipper 不可用时退回 `ydotool type`（仅英文数字符号）
- 文件工具默认只能碰工作区（`manual` / `auto` 模式）；`full` 模式下不设边界
- 部分 Wayland 原生窗口无法直接截取，桌面模式依赖 `spectacle`
- 会话只存本机，没有云端同步

## 待办

- **远程控制** — 让局域网设备注入按键与触摸，实现「远程下一句，agent 代为操作」。前置是网络鉴权（`:18765` 若不带令牌就监听 `0.0.0.0`，等于把执行命令的入口开放给整个局域网）
- **QAM 全局热键 / 独立截图按钮** — 会和 Steam 输入争抢，暂缓

## 许可

本项目采用 [CC BY-NC 4.0](LICENSE)（署名—非商业性使用 4.0 国际）协议发布：
可自由复制、修改、分发，但须署名，且不得用于商业用途。

---

<p align="center"><sub>DeckMeido · 为你守着这台掌机的女仆</sub></p>
