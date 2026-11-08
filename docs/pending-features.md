# DeckMeido 待实现清单

> 基线：版本 0.7.5，提交 `2e73ee7fae8ffa44910a445da925295539ee1f9b`，测试 329 项通过。
> 状态列按拉取后的代码（`8d904c02f70341d78031bcf79a42b6bb7af03009`）逐项核对，不照抄计划描述。
> 状态取值：`已实现` / `部分` / `未实现`。

| 编号 | 功能点 | 要点 | 前置依赖 | 优先级 | 预估代价 | 验证方式 | 状态 |
|---|---|---|---|---|---|---|---|
| A1 | OneBot 固定命令菜单 | `server/menu.py` 已有命令表（help/status/shot/cancel/save_image/save_text/deps/memo_clear）、斜杠+口语别名匹配、`authorize` 权限、危险动作两级确认；`onebot.py` 已接入路由与动作回调。按设计不提供「安装依赖」，聊天窗口不能改设备环境 | — | P0 | 已投入 | `tests/test_menu.py`、`tests/test_onebot.py` 菜单/确认用例 | 已实现 |
| A2 | 记录清洗 | `onebot.py` 分发入口调用 `memory.clean_text(..., names=(昵称, 发送者))`，@机器人 与「昵称：」标签在进群缓存/长期记忆前被剥掉 | — | P0 | 小 | 单测：含 @ 与昵称前缀的文本落记忆后无残留 | 已实现 |
| A3 | 轻量条目记忆 + scope | `memory.py` 仍是单文件全局条目（add/update/remove/clear/block/stats），无 private/group/person 维度；`onebot.py` 未按会话 scope 读写 | 记录清洗（已完成） | P1 | 中 | 单测：私聊/群/人三类 scope 隔离与清理 | 未实现 |
| A4 | 群常识 | `server/groupmemo.py`：内存 TTL（默认 300s）、按群隔离、只读提示块、管理员 `/memo` 清除 | — | P1 | 已投入 | `tests/test_group_memo.py` | 已实现 |
| A5 | 群聊限频与分段 | `onebot.py`：`rate_per_minute=6`（每人/分钟）、`group_rate_per_minute=10`（每群/分钟）、`reply_chunk_chars=300`、`max_reply_chunks=3`，白名单外群先拦再限流 | — | P1 | 已投入 | `tests/test_qq_turn_gate.py`、限流/分段用例 | 已实现 |
| A6 | 可配置输出敏感词 | `redact_words` / `redact_mask` 写入配置，`redact.Redactor` 替换输出；网页设置页有多值控件 | — | P1 | 已投入 | `server/redact.py` 相关用例、设置保存校验用例 | 已实现 |
| A7 | 每会话排队 1 条 | 当前是「忙就拒」：`_busy()` 为真时直接回「上一件事还在处理」，没有队列缓冲 | — | P2 | 中 | 并发两条消息的排队顺序与超时用例 | 未实现 |
| A8 | 昵称触发 | 群里非管理员必须 @ 机器人才处理，没有「提到昵称即触发」模式与开关 | — | P2 | 小 | 关闭/开启两态的消息路由用例 | 未实现 |
| A9 | `/memory` 管理命令 | 菜单只有 `memo_clear`（清本群缓存），没有按 scope 查看/清空记忆的指令 | A3 scope | P2 | 小 | 指令解析 + 清理生效用例 | 未实现 |
| B1 | 语音模型三层兜底 | 已有手动补下载（`/api/tts/download`、自检卡 `tts_model` 组件调用 `tts.start_download`），状态区分引擎缺失（runtime）与模型缺失（model）；缺安装期自动下载、缺启动时自愈（无 `auto_download` 配置，`install.sh` 只校验不下载）、缺独立「本地模型」卡片 | — | P0 | 中 | Deck 上断网安装、删除模型目录后重启、网页手动补下载 | 部分 |
| C1 | 离线 / 受限模式 | 已实现第一版：`[runtime] offline_mode`（默认 false，设置页可切换、保存即生效）；`server/netmode.py` 统一快速失败模型对话、在线模型列表、更新检查、余额、联网搜索/天气、TTS/STT/唤醒模型下载；顶部离线横幅与状态接口 `offline_mode` 已接入；回环/局域网地址放行，本地模型仅留可选插座，不做硬依赖；本地命令、文件、日志、已装好的 STT/TTS 不受影响 | — | P1 | 中 | 关/开开关各跑一轮对话、模型列表、更新、余额与下载；断网后确认本地功能可用 | 已实现（第一版） |
| D1 | OneBot 语音消息 | `_send_voice_file` 发 `record` 段 `base64://`；默认 WAV 交给 NapCat 转码，预留 `_voice_convert` 钩子但未接入 graiax-silkcoder（仓库无该依赖）；`voice_reply=false`、`voice_group_members=true`（群友默认允许，与原计划的「群友不给」相反）；真机 6 项（record 接受、base64、WAV/SILK、60s、群私差异、限速/`can_send_record`）未验证 | NapCat 真机 | P3 | 中 | 真机逐项记录（群聊 + 私聊） | 部分 |
| E1 | TTS 中英混排实机验证 | `[tts] ja_terms` 词表与句子级主语言路由已实现（`tts.py` 整词匹配、引号区间独立判定）；英文段真实音频、中日英混排听感、技术文档切碎后听感未实机验收 | Deck + `--tts` | P1 | 小 | 逐句试听并记录（英文段、混排句、长技术文档） | 部分 |
| F1 | Deck 上机自检与发布 | `install.sh --tts` 与 `verify_tts` 真实合成自检已实现；0.7.5 未打 tag、未创建 Release、未上传镜像站；`winres/deckmeido/` 已有 7 项（主包、插件包及各自 sha256、install.sh、latest.json、SHA256SUMS），`dist/` 另有 0.7.x 历史产物 | Deck 真机 | P1 | 小 | 真机跑 `bash install.sh install --tts`；上传后逐项核对 sha256 与 latest.json | 部分 |
| G1 | GitHub 改名旧链接 | remote 已指向 `Congmeims/DeckMeido`，仓库内无处理旧链接的代码；旧地址返回 200 无 301 是站外行为，需在 GitHub 端确认 | — | P2 | 小 | 直接访问旧 URL 看状态码 | 未实现 |
| G2 | Linux 路径大小写 / 旧目录 | `install.sh` 已识别旧目录 `~/deckmeido`（带安装标记或 VERSION），默认原地升级不装两份，提供 `bash install.sh migrate`；旧工作区仅提示迁移命令 | — | P1 | 已投入 | 旧目录机器上跑安装脚本三分支 | 已实现 |
| G3 | 资源占用结论 | 结论为全模块全模型约 1.62 GB 磁盘、常驻约 1.2–1.7 GB 内存；`/api/status` 只有主机内存/磁盘百分比，无按模块的模型占用统计 | — | P2 | 小 | 目标机复测并与结论对齐 | 部分 |

## 备注

- 已由近期拉取的改动完成、无需再做：A1、A2、A4、A5、A6、G2。
- 仍需按代码补齐的重点：B1（安装期下载 + 启动自愈 + 独立模型卡）、A3/A9（记忆 scope 与 `/memory`）、A7（会话排队）、A8（昵称触发）。
- D1 的默认值与原计划有出入：`voice_group_members` 当前默认 true（群友可触发语音），如要「群友不给」需改默认值并同步设置页文案。
