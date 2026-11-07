# DeckMeido 远程终端（Remote Shell）

给已配对的手机 / 网页客户端提供**可交互的真实 shell**：多会话、常驻工作目录与环境变量、
实时流式输出、stdin 写入、随时终止。默认关闭，属于高危能力。

- 后端实现：`server/remote_terminal.py`
- 路由接入：`server/main.py`（`/api/terminal/sessions*`、`/ws/terminal/{sid}`）
- 测试：`tests/test_remote_terminal.py`

## 1. 安全边界（实现结论）

| 约束 | 实现 |
| --- | --- |
| 默认关闭 | `config.toml` 的 `[terminal] remote_shell = false`；关闭时所有远程终端接口返回 **403**，`error_code=remote_shell_disabled`，`reason` 直接可展示 |
| 鉴权 | 只认现有 `require_auth`：已配对设备的 `deckmeido_device` Cookie；回环地址仍按原逻辑免鉴权。没有新增免鉴权路径 |
| 运行身份 | 后端进程身份（Deck 上的 `deck` 用户）直接拉起 shell；不 sudo、不提权、不 setuid |
| 危险命令 | 复用 `server/policy.py` 的 `classify_remote_command`（含 `sh -c` / `bash -c` 递归检查）以及 `power_action` / `steamosctl_action`；命中不执行，返回 **428 + need_confirm**，客户端带 `confirm=true` 再提交一次才写入 shell |
| 审计 | 每次 `exec` 落一条现有审计事件，`kind=terminal`、`action=exec`，并带命令原文、来源、开始/结束时间、退出码、是否确认、是否终止 |
| 与 AI 工具链隔离 | 独立会话管理器，不复用 `run_command` 的 ContextVar/审批标记 |
| 资源回收 | 空闲超时回收、最大会话数、单会话输出上限；终止时杀整个进程组；进程退出清理 fd 并记录退出码 |
| 关掉开关 | `POST /api/config` 写入 `terminal_enabled=false` 后，立即终止全部存活会话 |

补充说明：`POST .../input` 是原始 stdin 通道（交互程序、Ctrl+C、密码输入）。
服务端会对**本次请求里带换行的每一行**做危险判定；但故意把一条命令拆成多次无换行写入
理论上可以绕过行级检查。因此客户端应把完整命令行走 `.../exec`，`input` 只用于
交互式程序的按键/控制序列。

## 2. 默认值与可配置项

写入 `config.toml`：

```toml
[terminal]
remote_shell = false          # 总开关，默认关闭
idle_timeout_seconds = 900    # 空闲回收，默认 15 分钟（允许 5–86400）
max_sessions = 3              # 并发会话上限（允许 1–10）
max_output_bytes = 1048576    # 单会话输出总量上限，默认 1 MiB（允许 1 KiB–16 MiB）
shell = ""                    # 空 = 自动：Linux 优先 /bin/bash，Windows 用 cmd.exe
cwd = ""                      # 空 = 用户家目录
```

本功能实际采用的默认值：**空闲 900 秒、最多 3 个会话、单会话输出 1 MiB**。

## 3. REST 接口契约

所有 `/api/terminal/...` 接口都需要设备 Cookie（或回环免鉴权）。
成功响应均带 `"ok": true`；失败响应的统一结构：

```json
{"ok": false, "error_code": "...", "reason": "...", ...附加字段}
```

> 例外：未配对设备由现有 `require_auth` 返回 `401 {"detail": "未配对设备：请先在设置页配对"}`。

### 3.1 设置页开关（复用现有 /api/config）

`GET /api/config` 新增只读字段：

```json
{
  "terminal_remote_shell": false,
  "terminal_idle_timeout": 900,
  "terminal_max_sessions": 3,
  "terminal_max_output_bytes": 1048576,
  "terminal_shell": "",
  "terminal_cwd": ""
}
```

`POST /api/config` 新增可写字段（都是可选项，`null`/缺省表示不改）：

```json
{
  "terminal_enabled": true,
  "terminal_idle_timeout": 60,
  "terminal_max_sessions": 2,
  "terminal_max_output_bytes": 4096,
  "terminal_shell": "/bin/bash"
}
```

`terminal_enabled=false` 会同时回收全部会话。响应里带回实际的 `terminal_*` 值。

### 3.2 会话对象

```json
{
  "id": "a1b2c3d4e5f6",
  "pid": 12345,
  "state": "running",              // running | exited | terminated | timeout | shutdown | disabled
  "exit_code": null,               // 结束后为进程退出码（被强杀时可能为 -1 / 非 0）
  "created": 1762400000.0,
  "started": 1762400000.0,
  "last_activity": 1762400005.0,
  "idle_seconds": 5.0,
  "ended": null,
  "cols": 120,
  "rows": 36,
  "cwd": "/home/deck",
  "shell": "/bin/bash",
  "backend": "pty",                // pty（Linux 正式环境）| pipe（仅 Windows 开发机测试）
  "resize": true,
  "output_total": 1234,
  "output_next_seq": 3,            // 下一块的 seq，用作 cursor
  "truncated": false,
  "terminated": false,
  "completed_execs": 1,
  "last_command": {
    "token": "9f8e7d6c5b4a",
    "exit_code": 0,
    "started": 1762400010.0,
    "ended": 1762400010.2,
    "confirmed": false,
    "terminated": false
  }
}
```

### 3.3 创建会话

`POST /api/terminal/sessions`

请求：

```json
{"cwd": "/home/deck", "cols": 120, "rows": 36, "source": "mobile"}
```

- `cwd` 可选；不存在或不是目录 → `400 cwd_missing`
- `cols` 20–500、`rows` 5–300，服务端夹取
- `source` 只进审计（最多 60 字符），不参与鉴权

响应 `200`：

```json
{"ok": true, "session": { ...会话对象... }}
```

失败：

| 状态 | error_code | 场景 |
| --- | --- | --- |
| 401 | — | 未配对设备 |
| 403 | `remote_shell_disabled` | 总开关关闭 |
| 400 | `cwd_missing` | 工作目录不存在 |
| 429 | `too_many_sessions` | 存活会话达到 `max_sessions` |
| 500 | `spawn_failed` | shell 启动失败 |

### 3.4 会话列表 / 状态

`GET /api/terminal/sessions`

```json
{"ok": true, "enabled": true, "settings": {...}, "sessions": [{...}]}
```

`GET /api/terminal/sessions/{id}`

```json
{"ok": true, "session": {...}}
```

会话结束后仍可在 **300 秒**内查询状态；过期 → `404 session_not_found`。

### 3.5 执行命令

`POST /api/terminal/sessions/{id}/exec`

请求：

```json
{"command": "echo hello", "confirm": false, "source": "mobile"}
```

命令会写入同一个常驻 shell（保持 cwd/环境），并追加一个不可见退出码标记。
响应 `200` 表示**已接收**，输出通过 WS 或 `.../output` 读取：

```json
{
  "ok": true,
  "token": "9f8e7d6c5b4a",
  "confirmed": false,
  "level": "normal",
  "session": {...}
}
```

命中危险判定且 `confirm=false` → **428**：

```json
{
  "ok": false,
  "error_code": "confirm_required",
  "reason": "rm 会删除文件，需明确确认",
  "need_confirm": true,
  "level": "deny",          // deny | hard_confirm
  "rule": "rm",
  "command": "rm -rf /tmp/x"
}
```

客户端原样重发并带 `"confirm": true` 后才会真正执行，审计里 `confirmed=true`。

其它失败：`404 session_not_found`、`409 session_ended`、`400 empty_command`、
`413 command_too_long`（上限 4096 字符）、`502 write_failed`。

### 3.6 写入 stdin

`POST /api/terminal/sessions/{id}/input`

```json
{"data": "ls\r", "encoding": "utf-8", "confirm": false, "source": "mobile"}
```

- `encoding`: `utf-8`（默认，也接受 `text`/`utf8`）或 `base64`（发送控制序列/二进制）
- 单次解码后上限 16 KiB → `413 input_too_large`
- 请求文本按行做危险判定；命中返回同样的 **428 + need_confirm**
- 成功：`{"ok": true, "bytes": 16}`

错误：`400 bad_encoding`（base64 非法或 encoding 不支持）、`404`、`409`、`413`。

### 3.7 终止会话

`POST /api/terminal/sessions/{id}/terminate`

先给进程组发 SIGTERM，1 秒不退再 SIGKILL；清理 fd、标记 `state=terminated`、
给未完成的命令补审计（`terminated=true`）。

```json
{"ok": true, "already_ended": false, "session": {...}}
```

已结束会话重复调用返回 `200` 且 `already_ended=true`；不存在 → `404`。

### 3.8 读取输出（轮询/断线补历史）

`GET /api/terminal/sessions/{id}/output?cursor=0&max_bytes=262144`

- `cursor`: 上次返回的 `next_cursor`，从 0 开始
- `max_bytes`: 单页上限 1 KiB–1 MiB，默认 256 KiB

```json
{
  "ok": true,
  "session": {...},
  "chunks": [
    {"seq": 0, "encoding": "base64", "data": "aGVsbG8K"}
  ],
  "next_cursor": 1,
  "dropped": false,
  "truncated": false
}
```

- 输出块一律 base64，任意字节（含非法 UTF-8）都不会让接口/连接报错
- `dropped=true` 表示请求的 cursor 太旧，服务端环形缓冲已滚掉前面的块
- 超过 `max_output_bytes` 后不再保存新输出并置 `truncated=true`，进程继续运行

## 4. WebSocket 事件契约

`GET /ws/terminal/{id}?cursor=N`（升级为 WebSocket）

- 鉴权：设备 Cookie（或回环）。失败：accept 后关闭码 **4401**
- 开关关闭：先发 `error` 事件再关闭 **4403**
- 会话不存在：先发 `error` 再关闭 **4404**
- `cursor` 用于断线重连补历史；连接后自动订阅该会话后续输出
- 客户端应定期发 `ping`（同时刷新空闲计时），否则 15 分钟无活动会被回收

服务端 → 客户端：

```json
{"type":"hello","session":{...},"cursor":0}
{"type":"cursor","next_cursor":1,"dropped":false}
{"type":"output","seq":0,"encoding":"base64","data":"..."}
{"type":"truncated","total_bytes":1048576,"limit":1048576}
{"type":"confirm_required","level":"deny","rule":"rm","command":"rm -rf /x","reason":"..."}
{"type":"exec_started","token":"9f8e7d6c5b4a","confirmed":true}
{"type":"exited","state":"exited|terminated|timeout|shutdown|disabled","exit_code":0}
{"type":"pong","ts":1762400010.0}
{"type":"error","code":"...","reason":"..."}
```

客户端 → 服务端：

```json
{"type":"exec","command":"echo hi","confirm":false,"source":"mobile"}
{"type":"input","data":"<base64>","confirm":false,"source":"mobile"}
{"type":"resize","cols":120,"rows":36}
{"type":"ping"}
{"type":"stop"}
```

- `exec` 被接收 → 服务端回 `exec_started`；命中危险判定 → 回 `confirm_required`
- `input` 成功不单独回执；命中危险判定 → 回 `confirm_required`
- `stop` 会终止会话，服务端补发 `exited` 后关闭连接
- 进程自行退出时服务端发 `exited` 并关闭连接

## 5. 审计字段

写入现有 `server/audit.py` 缓冲区（`GET /api/audit` 可读），事件示例：

```json
{
  "ts": 1762400010.2,
  "kind": "terminal",
  "action": "exec",
  "result": "ok",                  // ok | fail
  "ip": "192.168.1.70",
  "reason": "exit_code",
  "detail": "echo hello",          // 命令原文
  "session_id": "a1b2c3d4e5f6",
  "source": "mobile",              // 来源标识
  "started": 1762400010.0,
  "ended": 1762400010.2,
  "exit_code": 0,
  "confirmed": false,
  "terminated": false
}
```

另有 `action=open`（创建会话）与 `action=close`（显式终止）两条辅助事件。

## 6. 实现说明与测试

- **PTY**：Linux/SteamOS 上使用 `ptyprocess.PtyProcess.spawn`（`forkpty`，即设置控制终端）
  拉起真 PTY；依赖在 `requirements.txt` 声明为 `ptyprocess>=0.7,<0.8`，缺失时退回
  `pty.openpty + subprocess.Popen(start_new_session=True)`。两者都不可用时，创建会话
  返回 501 `pty_unavailable` 并附安装命令，服务本身照常启动（依赖为惰性导入）。
  输出按 4 KiB 块读取，经 `run_in_executor` 交给事件循环，不阻塞请求。
- **退出码**：每条 `exec` 注入随机 token 的标记行，解析后得到该命令退出码；标记行
  从客户端可见输出中剔除（兼容 Windows cmd 的提示符前缀）。
- **Windows 开发机**：`pty` 不可用，测试走 `cmd.exe` + 管道后端（`backend="pipe"`）。
  这只是让 `python -m unittest discover -s tests` 能在 Windows 跑通；正式目标机始终是 PTY。
- **测试**：`python -m unittest discover -s tests`（749 个用例全绿）；
  单独跑：`python -m unittest tests.test_remote_terminal -v`。
