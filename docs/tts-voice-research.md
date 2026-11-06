# DeckMeido 离线 TTS 选型调研（日系女仆音色 / 中英日三语 / Steam Deck 纯 CPU）

> 调研日期：2026-11-06
> 结论用途：为 `server/tts.py` 从 espeak-ng 升级到离线神经 TTS 做技术选型，不改代码。
> 证据标注约定：**[有来源]** = 有公开文档/发布页/API 元数据可查；**[推测]** = 由同类硬件数据外推；**[未验证]** = 本次没有实机或没有可信实测数据。

---

## 0. 结论速览（TL;DR）

- **首选：piper-plus + つくよみちゃん（Tsukuyomi-chan）6 语模型**。
  单个 ONNX 模型 **39.65 MB**（fp16，HF API 实测字节数），代码 MIT，模型走「つくよみちゃんコーパス」条款（**允许商用，但必须在使用者可见处署名**），音色就是年轻日系动画女声，和「稍微有点像日系女仆」的需求高度吻合。上游基准（Xeon，非 Deck）RTF 0.078、单句 P50 27 ms、常驻 RAM 约 208 MB，冷启动约 1.6 s。**[有来源 + 非 Deck 实测]**
- **备选：Kokoro-82M int8**。88 MB 模型 + 27 MB voices，Apache-2.0，音色选择多、社区成熟；但三语中的日语需要 `misaki[ja]` + **完整 UniDic（约 526 MB）**，且 sherpa-onnx 官方只给 Kokoro 做了中/英，日语得走 `kokoro-onnx`。总占用会到 ~650 MB，CPU 也明显更慢。**音质/许可优先时选它，体积/延迟优先时不选。**
- **不建议**：官方 Piper（已归档，且无日语；现维护分支 GPL-3.0）、MeloTTS（中文官方权重含 581 MB BERT，整体 ~750 MB + torch）、Supertonic 3（很快，但 **31 种语言里没有中文**）、GPT-SoVITS / gsv.cpp（权重 + 前端约 2.5 GB，还需要参考音频）、edge-tts（联网服务，非离线）。
- **落地方式**：沿用现有 `[tts]` 配置与 `TTS.speak()/stop()` 契约，新增引擎适配层；模型放 `<数据根>/models/tts/`，按需下载、懒加载、空闲卸载（和 STT 同一套模式），播放仍用本机 PipeWire，不把模型塞进插件 zip。

---

## 1. 现有实现盘点（决定候选必须满足什么）

### 1.1 当前调用链

- 配置：[`server/config.example.toml`](../server/config.example.toml#L149-L161) 的 `[tts]`，字段 `enable / engine / voice / speed / max_chars`。默认 `engine = "espeak-ng"`、`voice = "cmn"`、`speed = 170`（词/分钟）、`max_chars = 300`，默认关闭。
- 实例化与热更新：[`server/main.py`](../server/main.py#L685-L691)、[`server/main.py`](../server/main.py#L731-L742)。设置页改开关走 `save_config` → 热更新，不重启。
- 播报入口：[`server/main.py`](../server/main.py#L3957-L3960)，只播最终回复，`asyncio.create_task(tts.speak(reply))`，**QQ 通道不播**。
- 实现：[`server/tts.py`](../server/tts.py#L17-L61)。`speak()` 清洗 Markdown → `stop()` 打断上一条 → `asyncio.create_subprocess_exec` 拉起 `espeak-ng`；`stop()` 先 terminate、2 秒后 kill 并再次 wait，避免僵尸进程。
- 系统依赖：`install.sh` 把 `espeak-ng` 作为可选系统包（[`install.sh`](../install.sh#L674)、[`install.sh`](../install.sh#L790)）；`server/depcheck.py` 有对应探测。
- 语音输入（可复用的工程模式）：[`server/stt.py`](../server/stt.py#L1-L20)，sherpa-onnx + SenseVoice-Small int8，模型放 `<数据根>/models/sensevoice`，**按需下载、镜像优先、懒加载、空闲 600 s 卸载、2 线程**；工具依赖单独放 `requirements-stt.txt`，装不上不影响主程序。

### 1.2 由此推出的硬性设计约束

| 约束 | 含义 |
|---|---|
| 在本机出声 | 不是把音频发给前端播放，后端进程直接调音频设备；因此必须能拿到 PipeWire/Pulse 会话（`XDG_RUNTIME_DIR`） |
| 可打断 | 新回复必须能立刻掐掉上一句；进程内推理不能“杀线程”，只能按句切分 + 取消后续句 |
| 不阻塞事件循环 | 推理必须 `asyncio.to_thread` / 线程池，且用锁串行化 |
| 内存可回收 | Deck 15 W 预算，必须能空闲卸载，不能像游戏一样常驻 500 MB |
| 模型不进插件包 | 插件 zip 只允许 `main.py / plugin.json / package.json / dist/`（项目既有约定），模型只能放 `userdata/` 按需下载 |
| 文本要清洗 | 现有 `_clean()` 会去掉 `* _ \` # > [ ]` 和 URL；神经 TTS 对残留符号更敏感，选型时要测 |
| 配置向后兼容 | `speed = 170` 是 espeak 的词/分钟语义；神经 TTS 是倍率语义，不能直接复用同一个字段 |

### 1.3 关于仓库内的 gsv.cpp 调研结论

本次全仓检索 `gsv` / `GPT-SoVITS` / `sovits` **没有命中任何文档**，无法引用旧结论；下文按 gsv.cpp 官方仓库与 HF 元数据独立评估。

---

## 2. 候选对比表

### 2.1 事实表（体积 / 延迟 / 内存 / 语种 / 许可 / 部署）

| 方案 | 模型体积 | 量化后 | CPU 延迟（来源） | 内存 | 语种 | 音色与女仆感 | 许可 | 部署 | 流式 |
|---|---|---|---|---|---|---|---|---|---|
| **piper-plus + つくよみちゃん 6lang** | fp16 ONNX **39.65 MB** [有来源] | 已有 fp16；int8 未提供 | 上游 Xeon E5-2650v4：RTF **0.078**、P50 **27 ms**、冷启动 1.6 s [有来源，非 Deck]；Deck 未验证 | 上游约 **208 MB** [有来源，Xeon] | **ja/en/zh/es/fr/pt**（单模型，6 语已训练） | つくよみちゃん = 年轻日系动画女声，**最贴近需求**；可调语速，不能换音色 | 代码 **MIT**；模型 **tsukuyomi-chan-corpus**（商用 OK，强制署名，条款继承） | `pip install piper-plus[cpu]`，纯 Python + onnxruntime，内置 G2P，不依赖 espeak | C++/Python 支持流式 |
| **Kokoro-82M int8** | int8 **88 MB** + voices **27 MB**；fp32 310–330 MB [有来源] | int8 88 MB | sherpa-onnx 官方 RTF（RPi4，2 线程，多语 v1.1）**4.470** [有来源]；M2 Max RTF 0.12 [有来源]；Deck [推测] ~1.0–1.5 | 约 **450 MB**（M2 Max 基准）[有来源] | en/ja/zh + es/fr/hi/it/pt；**ja 需 misaki[ja] + UniDic 526 MB** [有来源] | 女声多（`af_heart`、`jf_alpha`、`zf_xiaobei` 等），偏年轻但不一定“女仆” | 权重 **Apache-2.0**，推理代码 MIT；G2P 依赖 espeak-ng（GPL，项目已作为系统包） | `kokoro-onnx` + `misaki[ja/zh]`；或 sherpa-onnx（**仅中/英**） | 句级回调，非真流式 |
| **sherpa-onnx 自带 VITS/Matcha（中/英）** | aishell3 **30 MB**；matcha-zh-baker **73 MB**；en lessac-medium **61 MB**；zh fanchen-C 116 MB [有来源] | 部分模型有 int8 | aishell3 RTF **0.220**、matcha-zh **0.536**、en-lessac **0.482**（RPi4，2 线程）[有来源] | 约 200–300 MB [推测] | **zh/en**；无日语（日语只有 Supertonic 样例）[有来源] | aishell3 音色偏旧；matcha-baker 单女声但风格中性 | 框架 Apache-2.0；各模型许可需逐个确认（icefall/Piper 来源不同） | **已在本项目依赖里**（STT 装了 sherpa-onnx），零新依赖 | 支持 callback 分块 |
| **MeloTTS** | 官方中文：BERT wrapper **581 MB** + decoder 55.5 + encoder 30.5 + flow 76.9 + … [有来源]；sherpa 版 `vits-melo-tts-zh_en` **163 MB** | 无成熟 int8 | sherpa 版 RTF **3.877**（RPi4，2 线程）[有来源]；官方称 CPU 实时 [有来源] | 官方 torch 路径 1 GB 级 [推测] | en/es/fr/zh/ja/ko；中文支持中英混说 [有来源] | 单女声，偏中性播音，女仆感弱 | **MIT** [有来源] | 官方要 torch；sherpa 版无日语 | 句级 |
| **官方 Piper** | en/zh medium 约 **61–63 MB** [有来源] | 无官方 int8 包 | Piper 本家 RTF **0.066**、P50 35 ms（Xeon）[有来源]；sherpa VITS-Piper 0.482（RPi4） | 约 **185 MB** [有来源，Xeon] | 官方 voices **无 ja**；zh/en 齐全 [有来源 + 由 piper-plus 说明反推] | 女声有，但多为播音/中性；日系感靠特定 voice | 原仓库 MIT 但**已归档**；现维护 `piper1-gpl` 为 **GPL-3.0** [有来源] | 需要 espeak-ng 做 G2P；pip 包即 GPL | C++ 支持流式 |
| **Supertonic 3** | 99M 参数；int8 约 **145 MB** [有来源，第三方估算] | int8 145 MB | 官方宣称极快、CPU 实时；无独立 Deck 数据 [未验证] | 未验证 | **31 语含 ja/en，但没有 zh** [有来源] | 10 个预设音色 F1–F5/M1–M5，偏通用；女仆感一般 | 代码 MIT；权重 **OpenRAIL-M**（有使用限制，非 OSI） | `pip install supertonic` 或 sherpa-onnx（已收录） | 支持 |
| **GPT-SoVITS / gsv.cpp** | AR q8 **87 MB** / f16 158 MB；cond 96 MB；DiT **682 MB**；vocoder 64 MB；BERT **694 MB**；HuBERT **208 MB** [有来源] | 仅 AR 有 q8；DiT/BERT 仍 f16 | 官方 v2ProPlus RTF **0.526**（M4 CPU）；Deck / v5 未验证 | 1.5 GB 级 [推测] | zh/en/ja/ko/yue，支持混说 [有来源] | **可克隆**，女仆音色上限最高；需要 3–10 s 参考音频 | **MIT**（代码与权重转换）[有来源] | C++ 二进制 + Python sidecar + 前端 g2pw 635 MB + lid 131 MB，最重 | HTTP `/tts` 支持流式 |
| **edge-tts** | 无本地模型 | — | 网络往返决定延迟 | 很小 | 多语含中英日 | 微软在线音色，可挑年轻女声 | 服务条款/自动化风险，**非离线** [有来源] | 仅需网络 | 流式 |

> 备注：sherpa-onnx 的 RTF 表是 **Raspberry Pi 4 Model B Rev 1.5** 上的数字，RTF > 1 表示比实时慢。Steam Deck 的 Zen 2 单核通常比 RPi4 的 Cortex-A72 快 2–3 倍，但这是 **[推测]**，不能当作实测。

### 2.2 打分表（5 分最好，按本项目权重：体积/延迟 > 语种 > 许可 > 音色 > 部署）

| 方案 | 体积 | 延迟 | 三语覆盖 | 许可 | 女仆音色 | 部署难度 | 流式 | 加权总分 | 排序 |
|---|---|---|---|---|---|---|---|---|---|
| piper-plus + つくよみちゃん | 5 | 5* | 5 | 3 | 5 | 4 | 4 | **31** | **1** |
| Kokoro-82M int8 | 3 | 2* | 4 | 5 | 4 | 3 | 3 | **24** | 2 |
| sherpa-onnx VITS/Matcha | 4 | 5* | 2 | 4 | 3 | 5 | 4 | **27** | 语种不合格 |
| MeloTTS | 2 | 3* | 4 | 5 | 3 | 2 | 3 | **22** | 3 |
| 官方 Piper | 5 | 5* | 2 | 2 | 4 | 4 | 4 | **26** | 语种/许可不合格 |
| Supertonic 3 | 4 | 5* | 2 | 2 | 3 | 4 | 3 | **23** | 无中文 |
| GPT-SoVITS / gsv.cpp | 1 | 2* | 5 | 5 | 5 | 1 | 4 | **23** | 体积/部署不合格 |
| edge-tts | — | — | 5 | 1 | 4 | 5 | 5 | — | **非离线，排除** |

\* 延迟分数基于“同类模型在更弱硬件上的公开数据 + Deck 推测”，标记为 **[推测]**；实机必须按第 7 节复测。

---

## 3. 候选详评

### 3.1 piper-plus + つくよみちゃん 6lang（首选）

**是什么**：`ayutaz/piper-plus` 是原 `rhasspy/piper`（2025-10 归档）的 MIT 兼容分支，自带 8 语言 G2P（JA/EN/ZH/KO/ES/FR/PT/SV），不依赖 espeak-ng；6 语已训练模型包含 `ja/en/zh/es/fr/pt`。`ayousanz/piper-plus-tsukuyomi-chan` 是用「つくよみちゃんコーパス」微调出的单说话人 6 语模型。

**体积**：`tsukuyomi-chan-6lang-fp16.onnx` = **39,652,717 字节（约 37.8 MiB）**，HF API 元数据 [有来源]；配套 `config.json` 6.9 KB。相比 Kokoro 的 88 MB + 27 MB、MeloTTS 的 750 MB，这是数量级优势。

**速度/内存**：上游 README 的基准（Intel Xeon E5-2650 v4 @2.20 GHz，ONNX Runtime 1.24，warmup 5 / 30 次）给出：RTF **0.078**、P50 **27 ms**、RAM **208 MB**、冷启动 **1633 ms**、19.6M 参数 [有来源]。同表里 Kokoro 是 RTF 0.12 / 320 MB / 450 MB RAM（M2 Max 旧测），Piper 本家是 0.066 / 60 MB / 185 MB。**这些都不是 Deck 数据**，只能说明“同级别里最快”。
Deck 推算：按单核慢 3–5 倍算，单句 27 ms → 约 80–140 ms；30 字中文回复合成大概 0.2–0.5 s **[推测]**，完全在可接受范围。

**女仆感**：つくよみちゃん是公开的免费角色音源，声音是偏年轻、偏软的动画女声，符合“稍微有点像日系女仆”；它不是夸张角色扮演音，但辨识度足够。HF 仓库带 `samples/sample_ja.wav`、`sample_zh.wav`、`sample_en.wav` 可直接试听 [有来源]。

**许可（关键，2026-11-06 按原文核对）**：
- 代码：piper-plus 本体 MIT [有来源]。
- 模型：HF 标注 `license: other` / `license_name: tsukuyomi-chan-corpus`，声质适用つくよみちゃんコーパス条款；旧 root 版 ONNX（`tsukuyomi-chan-6lang-fp16.onnx`）的基座模型许可是 **CC BY 4.0**，但基座许可不改变声质条款 [有来源]。
- 本项目属于条款第③类「声質を使用した音声合成ソフト等を公開する（音声つき会話AIソフトもここ）」，允许公开；有偿/无偿、广告、个人/法人均不限制 [有来源]。
- **署名（必须，原文照抄）**：
  - 模型卡「クレジット表記について（必須）」：`■Piper Plus:つくよみちゃん`
  - 语料模板：`本ソフトウェアの音声合成には、フリー素材キャラクター「つくよみちゃん」（© Rei Yumesaki）が無料公開している音声データを使用しています。`
  - 语料署名：`■つくよみちゃんコーパス（CV.夢前黎）` <https://tyc.rei-yumesaki.net/material/corpus/>
  - 模型卡另要求：在软件里写明使用模型「Piper Plus つくよみちゃん」并链接模型页 <https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan>
- **模板与禁止事项**：条款要求把「公式クレジット表記テンプレート」和禁止事项放在“使用者必ず目にする場所”。官方对话 AI 模板分「公开录屏 OK / NG」两版（[OK](https://tyc.rei-yumesaki.net/wp-content/uploads/tsukuyomi-chan_credit_template4.html) / [NG](https://tyc.rei-yumesaki.net/wp-content/uploads/tsukuyomi-chan_credit_template5.html)）。
- **商用**：モデル「個人・法人問わず、商用利用に制限はありません」；本项目自身仍按 CC BY-NC 4.0 分发，两者互不替代。
- **禁止事项（原文）**：人を批判・攻撃すること；特定の政治的立場・宗教・思想への賛同または反対を呼びかけること；刺激の強い表現をゾーニングなしで公開すること；他者に対して二次利用（素材としての利用）を許可する形で公開すること。视频/直播/演讲公开生成语音时须标注 `Piper Plus:つくよみちゃん`。
- 语料本体再分发原则上禁止；基于该声质的模型再分发需**继承条款**。因此 **DeckMeido 不应把模型打进发布包**，应运行时从 HF 下载，并在设置页/关于页展示署名。
- 不得把该声质未经沟通用于“つくよみちゃん以外角色”的 TTS 软件；本项目是女仆角色，需在文案上避免与该角色官方作品混淆，必要时按条款联系作者确认。
- 结论：**可用于本项目，但许可分只能给 3/5**——商用没问题，代价是署名义务 + 条款继承 + 不建议内置权重。本仓库已在设置页与 README 按原文落地署名。

**部署**：`pip install "piper-plus[cpu]>=2.0.2"`，纯 Python + onnxruntime；不需要外部二进制，不需要 espeak-ng（这点比官方 Piper 干净）。Python 3.9+；SteamOS 的 venv 需要实机确认 wheel 可用 [未验证]。

**流式**：C++/Python 均支持流式与句级切分 [有来源]；首版可以先用“整句合成→播放”，后续再上流式。

**风险**：
1. 这是 2026 年才活跃的新分支，2.0.0 有 breaking change（import 改名 `piper_plus`），社区规模远小于 Kokoro。
2. 单模型 6 语是“一个日语语料微调的模型兼顾中英”，**中文/英文的自然度没有独立第三方评测**，必须试听 [未验证]。
3. 语速/韵律控制比 Kokoro 粗；情感标签没有。
4. 许可条款需要产品侧真的把 credit 做进 UI，不能只在 README 写。

### 3.2 Kokoro-82M int8（备选）

**体积**：int8 ONNX **88 MB** + `voices-v1.0.bin` **27 MB**；fp16 169 MB、fp32 310–330 MB [有来源]。sherpa-onnx 的多语 v1.1 包是 **311 MB（fp32）**，int8 包存在但体积未核实 [未验证]。

**速度**：sherpa-onnx 官方 RTF（RPi4，2 线程）**4.470**，比 MeloTTS 还慢；M2 Max 上 RTF 0.12 [有来源]。换算到 Deck，**很可能接近或超过实时** [推测]。这意味着 30 字回复要 1–2 s 才出声，对话体验会明显落后于 piper-plus。

**三语**：Kokoro v1.0 权重支持 en/ja/zh（以及 es/fr/hi/it/pt），ja 有 4 个女声、zh 有 8 个 [有来源，社区整理]。但：
- sherpa-onnx 官方页面明确写 **“只加了中/英支持”**，不要把 sherpa 路径当成能说日语 [有来源]。
- 日语必须走 `kokoro-onnx` + `misaki[ja]`，而 `misaki[ja]` 需要**完整 UniDic（约 526 MB）**，`unidic-lite` 不够 [有来源]。加上 espeak-ng（G2P 依赖，GPL）后，总占用 **约 650 MB**，直接违背“体积小”的第一硬约束。
- 中文 G2P 走 `misaki[zh]`（jieba/pypinyin 一类依赖），比 piper-plus 的内置 G2P 重。

**音色**：女声选择多，`af_heart` 等偏温柔；`jf_alpha` 是日系年轻女声，但整体是“通用 TTS 音色库”，不是专门的 maids 音色。可通过 reference style 微调，但需要额外实验。

**许可**：权重 Apache-2.0，推理代码 MIT；espeak-ng 是 GPL-3.0，但本项目本来就把它当系统命令调用，不构成对 Python 代码的传染 [有来源 + 项目现状]。

**结论**：如果 piper-plus 的中英质量试听不过关，或者许可团队不接受“署名 + 条款继承”，Kokoro 是最稳的替代；代价是 **~650 MB 占用 + 更慢的 CPU**。只做中英（放弃日语）时，Kokoro int8 约 115 MB，是很好的中英方案。

### 3.3 sherpa-onnx 自带 TTS（工程复用最优，但缺日语）

**已有依赖**：`requirements-stt.txt` 已经装 `sherpa-onnx`，如果 TTS 也走它，**不需要新增任何 pip 包**，还能直接复用 `stt.py` 的下载/校验/懒加载/空闲卸载模式。

**可用模型与实测（RPi4，2 线程）** [有来源]：
- 中文 `vits-icefall-zh-aishell3`：30 MB，RTF **0.220**，174 个说话人（可挑女声，但数据集偏旧）。
- 中文 `matcha-icefall-zh-baker`：73 MB，RTF **0.536**，单女声，音质比 aishell3 稳。
- 中文 `vits-zh-hf-fanchen-C`：116 MB，RTF **2.451**，187 个说话人。
- 英文 `vits-piper-en_US-lessac-medium`：61 MB，RTF **0.482**。
- 中英 `vits-melo-tts-zh_en`：163 MB，RTF **3.877**。

**致命缺口**：sherpa-onnx 的官方预训练列表里，**日语只有 Supertonic 3 的样例**，没有独立的日语 VITS/Matcha 模型；而 Supertonic 没有中文。也就是说：
- 纯 sherpa 路线**无法用单模型覆盖中英日**。
- 组合路线可以：中文 VITS（30–73 MB）+ Supertonic（ja/en，145 MB），总计 175–218 MB，两个模型按语言路由；但**音色不一致**（中文一个女声、日英另一个女声），女仆感会被割裂，且要维护两套模型和语言判定。
- 作为“只做中英、日语继续用 espeak”的保守方案，它是最省事的。

**许可**：框架 Apache-2.0；具体模型许可要逐个看（icefall 多为 Apache-2.0，Piper 来源多为 MIT/CC）。不能笼统说“全部 Apache-2.0” [未验证]。

### 3.4 MeloTTS（不推荐）

**优点**：MIT（代码与权重都可商用）[有来源]；官方支持 en/es/fr/zh/ja/ko，中文支持中英混说；官方宣称 CPU 实时 [有来源]。

**为什么不选**：
1. **中文官方权重太重**：Qualcomm AI Hub 的拆解是 `bert_wrapper 581 MB + decoder 55.5 MB + encoder 30.5 MB + flow 76.9 MB + …`，合计 **约 750 MB**，还没算 torch [有来源]。BERT 是用来做中文韵律/多音字的，砍不掉。
2. sherpa-onnx 的 `vits-melo-tts-zh_en` 只有 **163 MB**，但**没有日语**，而且 RTF 3.877（RPi4/2 线程）在 Deck 上大概 0.7–1.2，偏慢 [推测]。
3. 音色是中性女声，女仆感弱；没有日系角色音可选。
4. 走官方包就要装 torch（CPU wheel 几百 MB），与“轻量、纯 Python 小依赖”的目标冲突。

### 3.5 官方 Piper（不推荐，许可 + 语种双问题）

- 原 `rhasspy/piper` 已于 2025-10 归档，原许可 MIT [有来源]。
- 现在活跃维护的是 `OHF-Voice/piper1-gpl`，**GPL-3.0-or-later**；PyPI 的 `piper-tts` 也是 GPL-3.0 [有来源]。把 GPL 推理库直接 import 进 DeckMeido 的 Python 进程，会让分发承担源码披露义务；即便只是子进程调用，也要谨慎。**这是本项目应避开的许可风险**。
- 官方 voice 列表里**没有日语**（piper-plus 的定位就是“唯一 MIT 兼容、补上日语的 Piper 分支”）[有来源]。中英 voice 体积很好（medium 约 61–63 MB），但满足不了三语。
- 结论：如果只用中英、且能接受 GPL，它是很快的；在本项目的三语 + 宽松许可目标下不合格。

### 3.6 Supertonic 3（快，但没有中文）

- 99M 参数，ONNX Runtime CPU，44.1 kHz，10 个预设音色（F1–F5/M1–M5），支持 31 种语言 [有来源]。
- int8 包约 **145 MB**（第三方引擎文档的下载体积；官方 tar 包精确大小未核实）[有来源/未验证]。
- **31 语列表里没有 `zh`**：官方 README 的语言列表是 ar/bg/hr/cs/da/nl/en/et/fi/fr/de/el/hi/hu/id/it/ja/ko/lv/lt/pl/pt/ro/ru/sk/sl/es/sv/tr/uk/vi [有来源]。对“中英日三语”直接不合格。
- 仓库已归档（2026 年停止开发），sherpa-onnx 虽已收录，但后续维护风险高；权重是 OpenRAIL-M（有使用限制，不是 OSI 许可）。
- 结论：作为“日英双语音色”很强，但缺中文，排除。

### 3.7 GPT-SoVITS / gsv.cpp（上限最高，体积/复杂度不合格）

- gsv.cpp 是 GPT-SoVITS V5 的 C++/ggml 推理，MIT，支持 `v5turbo`（4 步）与 `v5dev`（32 步），HTTP `/tts` 支持流式，前端 G2P 覆盖 zh/en/ja/ko/yue [有来源]。
- **实际权重体积**（HF API 精确字节数）[有来源]：
  - AR q8 87.1 MB / f16 158.4 MB
  - cond 95.6 MB
  - DiT 682.1 MB
  - vocoder 63.8 MB
  - BERT 693.9 MB
  - HuBERT 207.6 MB
  - 前端 g2pw 635.2 MB + 语言检测 lid 131.3 MB
  - 合计 **约 2.5–2.7 GB**，还没算 Python sidecar 和二进制。
- 速度：官方 GPT-SoVITS v2ProPlus 在 M4 CPU 上 RTF **0.526** [有来源]；v5turbo 4 步在 Deck 上**未验证**，但 682 MB DiT + 694 MB BERT 的内存和带宽压力明显不适合 15 W 掌机。
- 还需要 **3–10 秒参考音频** 和参考文本；每次说话都要带 reference，部署/使用复杂度远高于固定音色模型。
- 结论：如果未来要做“每个用户自定义女仆声线/克隆”，它是唯一值得考虑的路线；当前“轻量、离线、三语、固定女仆音色”的目标下，排除。

### 3.8 edge-tts（非离线，排除）

`edge-tts` 走微软 Edge 的在线朗读服务，**必须联网**，不需要 API key 但属于对浏览器功能的自动化调用 [有来源]。本项目卖点是“离线语音”，且微软文档明确不把 Edge Read Aloud 的自动化抓取视为已授权分发 [有来源]。**直接排除**，不作为降级选项（降级应保留 espeak-ng）。

---

## 4. 推荐方案

### 4.1 首选：piper-plus + つくよみちゃん 6lang（单模型 40 MB）

理由，按硬约束逐条对：
1. **体积**：40 MB，是唯一一个“单模型覆盖三语且低于 100 MB”的方案；RAM 约 200 MB 级，和现有 STT 的 250 MB 模型同量级。
2. **三语**：ja/en/zh 都在同一个模型里，且支持 es/fr/pt；不需要按语言切模型，不存在音色割裂。
3. **女仆感**：つくよみちゃん本身就是年轻日系女声，比 Kokoro 的通用音色库更贴题；官方有 ja/zh/en 试听样本。
4. **离线/接入**：纯 Python + onnxruntime，不依赖外部二进制，不需要 espeak-ng；可以完全复用 STT 的懒加载/空闲卸载/镜像下载模式。
5. **许可**：MIT 代码 + 允许商用的语料条款；代价是署名与条款继承（可做进 UI）。

### 4.2 备选：Kokoro-82M int8（音质优先 / 许可最省心）

如果试听后发现 piper-plus 的中英质量不够，或者法务不接受“语料条款 + 强制署名”，选 Kokoro：
- 只做中英：`kokoro-int8` + sherpa-onnx，约 115 MB，Apache-2.0，不需要新依赖。
- 要做日语：`kokoro-onnx` + `misaki[ja]` + 完整 UniDic，总约 650 MB，CPU 更慢；这是明确的代价。

### 4.3 工程兜底

保留 `espeak-ng` 引擎不动，`engine` 做成枚举（`espeak-ng` / `piper-plus` / `sherpa-onnx`）。神经模型下载失败、加载失败、或用户关掉语音时，自动回落到 espeak-ng，并在设置页显示原因——这和 STT “装不上只影响语音”的现有策略一致。

---

## 5. 被排除方案与原因

| 方案 | 排除原因（一句话） |
|---|---|
| 官方 Piper | 无日语；现维护分支 GPL-3.0，分发有源码披露风险 |
| MeloTTS | 中文官方权重 ~750 MB（BERT 占 581 MB）+ torch；女仆感弱 |
| Supertonic 3 | 31 语里没有中文，三语硬约束不满足；仓库已归档 |
| GPT-SoVITS / gsv.cpp | 权重+前端约 2.5 GB，需参考音频，Deck CPU 延迟未验证 |
| edge-tts | 联网服务，非离线；自动化使用有条款风险 |
| ChatTTS / XTTS-v2 / F5-TTS / CosyVoice | 体积 1 GB 级起，且 ChatTTS（CC BY-NC 4.0）、XTTS-v2（CPML）为非商用许可；本次未逐条取证，但都不满足体积/许可双约束 |
| sherpa-onnx 单模型三语 | 官方无日语 VITS/Matcha；Kokoro 在 sherpa 里只做了中英 |

---

## 6. 首选方案接入本项目的具体步骤（不改代码，仅方案）

### 6.1 依赖与安装

1. 新增 `requirements-tts.txt`（与 `requirements-stt.txt` 同级、同样可选）：
   ```text
   # 语音播报（可选）：神经 TTS，缺了自动回落到 espeak-ng
   piper-plus[cpu]>=2.0.2
   ```
   不要写进 `requirements.txt`；`install.sh` 里像 STT 一样单独询问，失败不阻断安装。
2. 不改 `plugin/` 与 `client/`：模型与推理都在后端，插件只通过现有 HTTP 配置接口开关。

### 6.2 数据目录

在 [`server/paths.py`](../server/paths.py#L116-L119) 旁边增加：

```python
def tts_model_dir() -> Path:
    """语音合成模型目录：<data>/models/tts/<voice>（首次下载时才创建）"""
    return data_dir() / "models" / "tts"
```

目录约定：
```text
userdata/
└── models/
    ├── sensevoice/                 # 现有 STT
    └── tts/
        └── tsukuyomi/
            ├── tsukuyomi-chan-6lang-fp16.onnx    # 39.65 MB
            ├── config.json
            └── SHA256SUMS
```

### 6.3 配置项（保持向后兼容）

建议扩展 `[tts]`，不改旧键语义：

```toml
[tts]
enable = false
engine = "espeak-ng"          # espeak-ng | piper-plus（后续可加 sherpa-onnx）
voice = "cmn"                 # espeak 语言名；piper-plus 时是模型名，如 tsukuyomi
speed = 170                   # espeak 专用，保持词/分钟
speed_scale = 1.0             # 神经 TTS 专用，0.8–1.3
max_chars = 300
model_source = "auto"         # auto（hf-mirror → huggingface.co）/ mirror / hf / 自定义 URL
idle_unload_sec = 600         # 空闲卸载，和 STT 一致
```

要点：**不要**把 `speed = 170` 直接塞给神经引擎；新增 `speed_scale`，避免热更新时把语速变成 170 倍。

### 6.4 `server/tts.py` 改造（引擎适配层）

保持对外契约 `TTS.speak(text)` / `TTS.stop()` 不变，内部拆成：

```text
TTS
├── _EspeakEngine      # 现有逻辑原样保留（子进程，terminate/kill）
└── _PiperPlusEngine   # 懒加载 + 线程锁 + 句级合成 + 播放子进程
```

关键实现点：
1. **懒加载 + 空闲卸载**：第一次 `speak()` 才加载 ONNX；`IDLE_UNLOAD_SEC` 后释放，`_last_used` 的判定与卸载都放在锁里（照抄 `stt.py` 的 `_maybe_unload` 模式）。
2. **不阻塞事件循环**：`await asyncio.to_thread(self._synth_wav, chunk)`；推理和 recognizer 一样**不是线程安全**，用 `threading.Lock` 串行化。
3. **打断**：`speak()` 先 `await stop()`；`stop()` 取消当前 task、terminate 播放进程（`pw-play`），并设置 cancel 标志，让句级循环在下一句前退出。**不要**试图杀推理线程——ONNX 不支持。
4. **按句切分**：先按 `。！？\n` 切句，再按 `max_chars` 截断；每句合成后立刻播放，能显著降低“首声延迟”，也让打断粒度更细。
5. **播放**：合成到临时 WAV（`userdata/tmp/` 或 `tempfile`），用 `pw-play` 播放；没有 `pw-play` 时回落 `paplay` / `aplay`。环境变量要继承 `XDG_RUNTIME_DIR`、`PULSE_SERVER`（STT 的 `pw-record` 已经踩过这个坑，见 `server/stt.py` 顶部说明）。
6. **文本清洗**：复用 `_clean()`，但神经 TTS 还要额外处理：表情/颜文字、连续标点、URL、代码块；建议在切句前做一次“可朗读化”处理。
7. **语言路由**：piper-plus 6lang 需要显式语言（或自动判定）。建议按脚本切段：
   - 含平假名/片假名 → `ja`
   - 纯 CJK 且无假名 → `zh`
   - 纯拉丁字母 → `en`
   - 混排按连续字符段切开，分别合成；每段都独立可打断。
   - 这是本项目最容易翻车的地方：把整段中文喂给日语 G2P 会读出怪音。
8. **状态接口**：`/api/status` 增加 `tts_runtime` / `tts_model` / `tts_attribution` / `tts_error`，设置页据此显示“未安装 / 未下载 / 已就绪 / 加载失败”，不要让用户猜。

### 6.5 模型下载

复用 `stt.py` 的探测/续传/`.part`/校验模式，新增一个 TTS 下载器（或把通用部分抽到 `server/downloads.py`）：
- 源顺序：`hf-mirror.com/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/...` → `huggingface.co/...`。
- 文件：`tsukuyomi-chan-6lang-fp16.onnx`（39.65 MB）+ `config.json`。
- 校验：大小下限（例如 ≥ 30 MB）+ 可选 SHA256；下载后**试加载一次**（只建对象就丢），确认 onnxruntime 能读。
- 失败分类沿用 STT 的 `network / disk / permission / cancelled`，前端给不同提示。
- 模型**不进插件 zip、不进 release 资产**：运行时下载，既符合项目打包约定，也符合つくよみちゃん条款（不内置再分发）。

### 6.6 设置页 / 授权署名

- 设置页 `[tts]` 区块增加：启用开关（已有）、引擎下拉、音色（模型）下拉、语速倍率、试听按钮、模型下载进度、错误行。
- **许可要求的署名**必须做进 UI（已落地）：设置页「女仆人格 → 语音回复」卡片下方常驻，照抄官方的两行
  `本ソフトウェアの音声合成には、フリー素材キャラクター「つくよみちゃん」（© Rei Yumesaki）が無料公開している音声データを使用しています。`
  `■つくよみちゃんコーパス（CV.夢前黎）` / `■Piper Plus:つくよみちゃん`，并分别链接语料条款页与模型页；这是使用该模型的硬性条件，不是可选项。
  条款还要求把禁止事项放在使用者能看到的位置，README 的「第三方组件与语音署名」一节已原文列出。
- 大屏/QAM/网页三处设置要保持同一份配置（项目既有约定），但**报表不涉及具体 UI 代码改动**。

### 6.7 测试（新增，不依赖真实模型）

- 文本切句与语言路由的纯函数测试（中/日/英/混排/emoji/URL）。
- 配置解析：`engine` 非法值回落 espeak；`speed_scale` 范围校验。
- 下载器：mock 源，验证 `.part` 续传、大小校验、失败分类。
- `TTS.stop()`：mock 播放进程，验证 terminate → kill → wait 的顺序，不产生僵尸。
- 引擎不可用：`piper-plus` 未安装时 `speak()` 不抛到主循环，回落 espeak 并记录状态。

---

## 7. 实机验证方法（Steam Deck 上可执行）

### 7.1 环境基线（一条命令）

```bash
lscpu | grep -E 'Model name|CPU\(s\)|MHz' ; free -m ; nproc
```

记录：CPU 型号/频率、可用内存、核心数。Deck 典型是 4 核 Zen 2、15 W TDP；后台跑游戏时余量更小。

### 7.2 模型体积与冷启动

```bash
du -sh ~/DeckMeido/userdata/models/tts/tsukuyomi
/usr/bin/time -f 'cold %e s, maxrss %M KB' \
  piper-plus --model ~/DeckMeido/userdata/models/tts/tsukuyomi/tsukuyomi-chan-6lang-fp16.onnx \
             --config ~/DeckMeido/userdata/models/tts/tsukuyomi/config.json \
             --output_file /tmp/tts.wav "おかえりなさい、ご主人様"
```

> `piper-plus` 的确切 CLI 参数以 2.0.2 文档为准（未在本机验证）；也可直接跑它仓库的 `scripts/benchmark.py`（上游基准就是这么测的）。

### 7.3 RTF / 延迟 / 内存脚本（通用骨架）

```python
# deck_tts_bench.py —— 在 Deck 上测“合成一句需要多久、占多少内存”
import resource, sys, time, wave, os

def audio_seconds(path):
    with wave.open(path) as f:
        return f.getnframes() / f.getframerate()

def synth_once(fn, text):
    t0 = time.perf_counter()
    out = fn(text)                      # 你的引擎调用：返回 wav 路径
    dt = time.perf_counter() - t0
    dur = audio_seconds(out)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # Linux: KB
    return dt, dur, rss

if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "おかえりなさい、ご主人様。今日は何をしますか？"
    # 这里替换成 piper-plus 或 sherpa-onnx 的实际调用
    dt, dur, rss = synth_once(lambda s: "/tmp/tts.wav", text)
    print(f"wall={dt:.3f}s audio={dur:.3f}s RTF={dt/dur:.3f} peakRSS={rss/1024:.0f}MB")
```

- **RTF < 0.3**：优秀，可做流式对话。
- **0.3–0.6**：可接受，短句体验尚可。
- **> 1.0**：比实时慢，30 字回复要等 1 s 以上，不建议用于闲聊。

### 7.4 首声延迟（用户真正感知的指标）

```bash
START=$(date +%s%N)
pw-play /tmp/tts.wav
END=$(date +%s%N)
echo "playback $(( (END-START)/1000000 )) ms"
```

再减去音频时长，就是“从文本到第一声”的纯等待。建议用 20–30 字的中文、日文、英文各 3 句测。

### 7.5 打断与内存回收

1. 连续触发两次 `speak()`（第二次在第一次播放中），确认第一段立刻停止、没有两段叠音。
2. 播完 10 句后 `ps -o rss= -p $(pgrep -f deckmeido)`，确认内存没有线性增长。
3. 静置 10 分钟后再次查看 RSS，确认空闲卸载生效（应从 200 MB 级回落到 100 MB 以下）。**本项目现有策略是 600 s，和 STT 保持一致。**

### 7.6 试听矩阵（决定“女仆感”）

至少 12 句，覆盖：
- 中文：`おかえりなさい` 之外的日常句、带数字/英文缩写、带标点。
- 日文：`ご主人様、おかえりなさいませ`、疑问句、长短句。
- 英文：`Welcome back, master.`、混入中文的句子。
- 混排：`今天辛苦啦，おつかれさま；要不要一起看会儿动画？`
- 边界：纯 emoji、URL、Markdown 残留、超长 300 字。

每句打分：自然度 / 女仆感 / 语种正确性 / 是否有怪停顿。**这项只能人耳判断，任何表格都替代不了。**

### 7.7 建议的验收阈值

| 指标 | 建议门槛 |
|---|---|
| 模型磁盘 | ≤ 60 MB（首选方案 40 MB，留余量） |
| 常驻 RSS | ≤ 300 MB（空闲 10 min 后 ≤ 100 MB） |
| RTF（30 字中文） | ≤ 0.6 |
| 首声延迟 | ≤ 800 ms |
| 打断 | 新句 100 ms 内停旧句 |
| 三语正确率 | 试听 12 句无“读错语言” |
| 回落 | 模型缺失/加载失败时 espeak 仍可用，且状态可解释 |

---

## 8. 未验证项与需要实机确认的点

### 8.1 本次调研没有验证的（必须实机/试听）

1. **piper-plus 的中文、英文自然度**：模型是日语语料微调的 6 语模型，中文/英文没有独立第三方评测；HF 只有样本，必须自己听。
2. **Deck 上的真实 RTF / 首声延迟 / RSS**：所有延迟数字来自 Xeon、RPi4、M2 Max，不是 Deck。
3. **SteamOS 上 `piper-plus[cpu]>=2.0.2` 的 wheel 可用性**：Python 版本、onnxruntime 的 AVX 支持、venv 安装体积都要确认。
4. **6lang 模型的语言切换行为**：显式 `language_id` 是否可靠；混排句子是否需要自己切段。
5. **PipeWire 播放环境**：后端从插件拉起时能否拿到 `XDG_RUNTIME_DIR` / 音频设备；`pw-play` 是否存在，`paplay`/`aplay` 回落是否可用。
6. **模型下载源可用性**：hf-mirror 对 `ayousanz/piper-plus-tsukuyomi-chan` 的覆盖、断点续传、国内网络下的实际速度。
7. **许可条款的落地**：2026-11-06 已按模型卡与语料条款原文核对，并在设置页/README 落地署名与禁止事项；若未来改成收费发行或允许用户把生成语音作为素材再分发，需重新对照第③类条款（必要时按条款页邮箱联系作者确认）。
8. **Kokoro 日语路径的真实占用**：`misaki[ja]` + UniDic 526 MB 的实际安装体积、加载时间、内存；以及 `kokoro-onnx` 在 Deck 上的 RTF。
9. **Supertonic 的精确 int8 包体积**：145 MB 来自第三方引擎文档，官方 tar 包未实测。
10. **gsv.cpp v5turbo 在 Deck 上的延迟**：官方只给了 M4 CPU 的 v2ProPlus 数据，v5 未验证。

### 8.2 明确不要做的事

- 不要把 `pip install piper-tts`（GPL-3.0）作为依赖装进本项目。
- 不要把 `tsukuyomi-chan-6lang-fp16.onnx` 打进插件 zip 或 release 资产。
- 不要把 `speed = 170` 直接传给神经引擎（语义不同）。
- 不要假设 sherpa-onnx 的 Kokoro 能说日语（官方只做中英）。
- 不要把 Supertonic 当作三语方案（无中文）。
- 不要在 `speak()` 里同步跑 ONNX（会卡住整个 FastAPI 事件循环）。

---

## 9. 参考来源

**仓库内**
- [`server/tts.py`](../server/tts.py)、[`server/main.py`](../server/main.py)、[`server/config.example.toml`](../server/config.example.toml)、[`server/stt.py`](../server/stt.py)、[`server/paths.py`](../server/paths.py)、[`requirements-stt.txt`](../requirements-stt.txt)、[`install.sh`](../install.sh)、[`README.md`](../README.md)

**外部**
- sherpa-onnx TTS 预训练模型与 RTF（Raspberry Pi 4）：https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/rtf.html
- sherpa-onnx Kokoro 页面（仅中/英、int8 包）：https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html
- sherpa-onnx Supertonic 文档：https://k2-fsa.github.io/sherpa/onnx/tts/supertonic.html
- Supertonic 3 官网：https://supertonic3.github.io/
- Supertonic 3 语言列表与归档说明：https://github.com/supertone-oss-archive/supertonic
- piper-plus README（MIT、6 语、基准）：https://github.com/ayutaz/piper-plus
- piper-plus つくよみちゃん模型（含精确体积）：https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan
- つくよみちゃんコーパス利用規約：https://tyc.rei-yumesaki.net/material/corpus/
- Piper 许可变更（GPL-3.0）：https://pypi.org/project/piper-tts/1.6.1/
- Piper 归档/GPL 说明：https://www.promptquorum.com/power-local-llm/piper-tts-review
- MeloTTS 官方仓库（MIT、语种、CPU 实时）：https://github.com/myshell-ai/MeloTTS
- MeloTTS 中文权重拆解（BERT 581 MB）：https://aihub.qualcomm.com/models/melotts_zh
- Kokoro-82M 模型卡（Apache-2.0、82M）：https://hf.co/hexgrad/Kokoro-82M
- Kokoro int8/fp16 文件体积：https://crates.io/crates/tts-rs/2026.2.0
- Kokoro 各语言音色数量整理（ja 4 / zh 8）：https://hivebook.wiki/wiki/kokoro-tts-82m-open-weight-multilingual-text-to-speech
- Kokoro 日语需要完整 UniDic（526 MB）：https://raw.githubusercontent.com/remsky/Kokoro-FastAPI/HEAD/AGENTS.md
- gsv.cpp 仓库与模型说明：https://github.com/KakaruHayate/gsv.cpp
- gsv.cpp 权重精确体积：https://huggingface.co/KakaruHayate/GSV.CPP
- GPT-SoVITS 官方 RTF：https://everylocalai.com/tool/gpt-sovits
- edge-tts 需要联网：https://pypi.org/project/audiomaker/1.0.0/ ；微软对 Edge Read Aloud 自动化的说明：https://learn.microsoft.com/en-au/answers/questions/6053926/where-can-i-obtain-written-licensing-clarification

---

## 10. 可直接试听的链接（2026-11-06 实际请求验证）

> 验证方式：对每个 URL 发送 HTTP HEAD（跟随重定向），记录状态码、`Content-Type`、`Content-Length`。下表只列 **返回 200 且 `Content-Type` 为音频** 的链接。链接来自模型作者在 HuggingFace 模型卡中给出的 Sample Audio 表，以及つくよみちゃん官网试听页。

### 10.1 模型合成音（最适合判断“TTS 出来是什么声”）

页面：<https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan> （HTTP 200，模型卡内含下列音频的播放器）
生成参数：`--length-scale 1.5 --noise-scale 0.667`（模型卡注明；本地复现时建议带上，否则默认语速会更快）

| 语言 | 直接播放 URL | 实测结果 | 音频内容（模型卡原文） |
|---|---|---|---|
| 日 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_ja.wav | 200 / audio/wave / 101,932 B | 「こんにちは、つくよみちゃんです。」 |
| 英 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_en.wav | 200 / audio/wave / 51,244 B | 「Hello, how are you today?」 |
| 中 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_zh.wav | 200 / audio/wave / 81,452 B | 「你好，今天天气很好。」 |
| 西 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_es.wav | 200 / audio/wave / 48,172 B | 「Hola, como estas hoy?」 |
| 法 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_fr.wav | 200 / audio/wave / 60,972 B | 「Bonjour, comment allez-vous?」 |
| 葡 | https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/samples/sample_pt.wav | 200 / audio/wave / 73,772 B | 「Ola, como voce esta hoje?」 |

**结论：中英日都有合成音样本，不需要只靠日语判断音色。**

同仓库还有一组旧 zero-shot 微调版样本（与当前 6lang 模型不是同一导出，仅供参考）：
- 日 https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/releases/zs-v1/samples/ja-1-ft.wav （200 / audio/wave / 78,892 B）
- 中 https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/releases/zs-v1/samples/zh-ft.wav （200 / audio/wave / 82,476 B）
- 英 https://huggingface.co/ayousanz/piper-plus-tsukuyomi-chan/resolve/main/releases/zs-v1/samples/en-ft.wav （200 / audio/wave / 71,724 B）

### 10.2 官方音源（判断“声线本身像不像日系女仆”）

页面：<https://tyc.rei-yumesaki.net/material/voice/sample-voice/> （HTTP 200，页面内有 `<audio>` 播放器）
说明：以下全部是 **真人声优录音（48 kHz / 16-bit / 单声道）**，不是 piper-plus 合成音；只有日语。适合判断音色气质，不能代表 TTS 的中英发音质量。

| 内容 | 直接播放 URL | 实测结果 |
|---|---|---|
| 【01】タイトルコール | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-1-titlecall.wav | 200 / audio/x-wav / 161,530 B |
| 【02】平静（名乗りのみ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-2-heisei.wav | 200 / audio/x-wav / 254,400 B |
| 【02】平静（名乗り＆セリフ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-2-heisei-full.wav | 200 / audio/x-wav / 724,708 B |
| 【03】喜び（名乗り＆セリフ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-3-yorokobi.wav | 200 / audio/x-wav / 837,104 B |
| 【04】悲しみ（名乗り＆セリフ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-4-kanashimi.wav | 200 / audio/x-wav / 1,025,500 B |
| 【05】泣き（名乗り＆セリフ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-5-naki.wav | 200 / audio/x-wav / 1,009,014 B |
| 【06】夢前黎（ご挨拶のみ） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-6-reiyumesaki.wav | 200 / audio/x-wav / 524,330 B |
| 【06】夢前黎（フルバージョン） | https://tyc.rei-yumesaki.net/files/voice/samplevoice/tyc-samplevoice-6-reiyumesaki-full.wav | 200 / audio/x-wav / 813,868 B |

> 官网提示：这些文件已切掉首尾静音，部分播放器会造成开头/结尾听起来被截断，音频本身没问题。

### 10.3 不能当作“つくよみちゃん音色”证据的链接

- piper-plus HF Space：<https://huggingface.co/spaces/ayousanz/piper-plus-demo> （HTTP 200）。但它的 demo 模型是 `models/multilingual-test-medium.onnx`，大小 **39,414,515 B**，与本模型的 **39,652,717 B** 不同；Space 的 README/app.py 也没有提到 tsukuyomi。**这是 piper-plus 的通用 6 语 demo，不是つくよみちゃん音色。**
- WASM demo：<https://ayutaz.github.io/piper-plus/> （HTTP 200），页面中未出现 `tsukuyomi` 关键字，同样不能确认是该音色。

### 10.4 本地自己合成中英日（可选）

如果想把试听句换成自己的台词：

```bash
# 1) 安装（版本需 >= 2.0.2；模型卡注明新 MB-iSTFT 架构需要 PR #320 之后的运行时）
pip install "piper-plus[cpu]>=2.0.2"

# 2) 下载つくよみちゃん模型（CLI 自带模型注册表）
python -m piper_plus --download-model tsukuyomi

# 3) 合成（语速参数与官方样本对齐）
python -m piper_plus --model tsukuyomi --length-scale 1.5 --noise-scale 0.667 \
  -f ja.wav "おかえりなさい、ご主人様"
python -m piper_plus --model tsukuyomi --language zh --length-scale 1.5 \
  -f zh.wav "你好，今天天气很好。"
python -m piper_plus --model tsukuyomi --language en --length-scale 1.5 \
  -f en.wav "Hello, how are you today?"

# 4) 播放（SteamOS / Deck）
pw-play ja.wav
```

> `--language` 的可用性以 `python -m piper_plus --help` 为准（上游 README 的 `infer_onnx` 示例里使用 `--language en`；包内 CLI 的参数名可能略有差异，本次未在 Deck 实机验证）。如果不想装依赖，也可以直接下载 10.1 的 6 个 WAV 来听。

