import { useEffect, useRef, useState } from "react";
import { addEventListener, call, removeEventListener } from "@decky/api";
import { Navigation } from "@decky/ui";
import { applyPadOrder } from "./padOrder";

/** 大屏路由页地址：index.tsx 注册路由、这里截图后导航回来，只能有这一份 */
export const BIG_SCREEN_ROUTE = "/deckmeido";

/**
 * 从大屏页退到游戏、到抓帧之间的等待。gamescope 抓的是合成后的整屏，
 * 路由页的退场动画没走完就会被拍进去；QAM 收起那套用的是 1.2s，
 * 这里是整页路由切换（还带一层淡出），给到 1.6s。
 */
const SHOT_LEAVE_MS = 1600;

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// 后端在不在、截图能不能用、代理走哪条路，由插件后端探测
export type Status = {
  install_dir: string;
  config_ok: boolean;
  port: number;
  gamescope: boolean;
  capture_ok: boolean;
  capture_reason: string;
  uid: number;
  as_user: string;
  proxy: string;
  ip: string;
  core_ok: boolean;
  core_reason: string;
  core_mode: string;
  // 用户点过「关闭核心」且没再点「启动核心」：显示「已停止」并禁用重启核心
  core_stopped: boolean;
  workspace: string;
  // 虚拟手柄可用性由后端判断：true/false 为后端结论，null = 后端没上报（显示「未知」）
  gamepad_ok: boolean | null;
  gamepad_reason?: string;
  tts: boolean;
  // 带画面询问：后端 [agent] with_shot，面板重开后按它恢复
  with_shot: boolean;
  // 语音输入开关（后端 [stt] enable）：关掉后后端会释放识别模型
  stt: boolean;
  // 录音期间静音外放（后端 [stt] duck_output，默认开）
  stt_duck: boolean;
  // 这台机器有没有 wpctl/pactl：没有时开关照旧可存，只是不生效
  stt_duck_available: boolean;
  // 更新检测（后端缓存的 /api/version.update）：设置页显示「有更新 / 当前最新」
  update_has?: boolean;
  update_latest?: string;
  model: string;
  game_name: string;
  // 后端 persona 的昵称：状态行前缀用，缺失时前端退回默认称呼
  persona_name: string;
  // 名字来源 / 置信度 / 证据时间：只有 high 才注入提问
  game_source: string;
  game_confidence: string;
  game_ts: number;
  version: string;
  // 运行中的后端版本 / 磁盘 VERSION / 两者是否一致。
  // 安装换完文件但没重启后端时 version_match=false，面板提示「重启核心」
  running_version?: string;
  disk_version?: string;
  version_match?: boolean;
  // 读不到后端配置时的明确原因（空串表示正常）
  config_error: string;
  // 局域网访问：地址（设置页展示、复制用）
  lan_url: string;
  // lan_enable=false 时只监听 127.0.0.1，设置页据此显示「仅本机」
  lan_enable?: boolean;
  // 掌机状态行：有人尝试配对 / 连续鉴权失败时的提示，空串表示正常
  security_note?: string;
  pending_pair?: number;
};

/** 待配对会话：PIN 只在掌机侧出现，浏览器侧看不到 */
export type PairSession = {
  pair_id: string;
  pin: string;
  ip: string;
  ua: string;
  name: string;
  attempts: number;
  created_at: number;
  expires_in: number;
  state: string;
};

/** 已配对设备（不含任何凭据/hash，只给设置页展示） */
export type PairedDevice = {
  device_id: string;
  name: string;
  ip: string;
  ua: string;
  created_at: number;
  last_seen: number;
};

export type Item = {
  role: "user" | "assistant";
  text: string;
  client: string;
  ts: number;
  image?: string;
};

export type Delta =
  | { kind: "delta"; text: string }
  | { kind: "final"; text: string; cancelled?: boolean }
  | { kind: "tool"; text: string }
  | { kind: "status"; running: boolean; since?: number }
  | { kind: "pad_order"; action: "to1p" | "revert" }
  | { kind: "thread"; items: Item[] };

export type ActionKind = "" | "ask" | "translate" | "send";

export type PendingPoke = { text: string; brief: string; action: ActionKind };

/** 大屏唯一的日志来源：设置页「维护」里只显示最近一条，点开看最近 3 条 */
export type LogEntry = { code: string; ts: number; detail: string };

// 跨挂载保留最近一次动作：从游戏画面回到大屏页时 busy 由后端事件补推，
// 但“哪个按钮在跑”只存在前端，放模块级变量才不会因为组件重挂而丢。
let activeAction: ActionKind = "";

// 日志同理：截图会短暂离开大屏页，组件重挂后仍要保留最近事件
let logStore: LogEntry[] = [];

export const SHOT_ASK_BRIEF = "看看这个，我该怎么做？";
export const SHOT_TRANSLATE_BRIEF = "翻译画面上的文字";

export const DEFAULT_ASK_TPL =
  "看我卡在哪一步，说说现在这个画面里我该做什么。" +
  "已经能从上图判断就不要搜索；最多搜 2 次，得到一个答案就停。\n" +
  "再总结成几条可以直接照做的步骤，200 字以内。\n" +
  "不要剧透：只讲眼前这一步怎么过，不要提前说后面的剧情、结局或其他关卡的解法。";

export const DEFAULT_TRANSLATE_TPL =
  "把画面上的外文原文先照抄出来，再逐句翻译成简体中文。" +
  "已经是中文就原样说明，不要翻译。\n" +
  "只翻译、不解释剧情、不剧透，保持原文的换行和顺序。";

/**
 * 大屏路由用的会话状态。逻辑与 QAM 的 Content 保持一致，
 * 但不依赖 QAM 的收起/恢复流程；所有调用都走插件后端。
 */
export function useSession() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [items, setItems] = useState<Item[]>([]);
  const [streaming, setStreaming] = useState("");
  const [busy, setBusy] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [activity, setActivity] = useState("");
  const [withShot, setWithShot] = useState(true);
  const [stt, setStt] = useState(true);
  const [sttDuck, setSttDuck] = useState(true);
  const [sttDuckAvailable, setSttDuckAvailable] = useState(false);
  const [tts, setTts] = useState(false);
  const [model, setModel] = useState("");
  const [modelList, setModelList] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [askTpl, setAskTpl] = useState("");
  const [askTplEdit, setAskTplEdit] = useState("");
  const [askTplDefault, setAskTplDefault] = useState(DEFAULT_ASK_TPL);
  // 翻译提示词存在后端 [agent] translate_template 里，这里只是镜像；
  // 真正发翻译请求时用 translateTpl（见 shotTranslate）
  const [translateTpl, setTranslateTpl] = useState(DEFAULT_TRANSLATE_TPL);
  const [translateTplEdit, setTranslateTplEdit] = useState(DEFAULT_TRANSLATE_TPL);
  const [translateTplDefault, setTranslateTplDefault] = useState(DEFAULT_TRANSLATE_TPL);
  const [probing, setProbing] = useState(true);
  const [restarting, setRestarting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [killing, setKilling] = useState(false);
  const [newing, setNewing] = useState(false);
  const [pendingPoke, setPendingPoke] = useState<PendingPoke | null>(null);
  const [runningAction, setRunningActionState] = useState<ActionKind>(activeAction);
  const [logs, setLogs] = useState<LogEntry[]>(logStore);
  // 设备配对：待配对会话（含 PIN/倒计时）与已配对设备列表
  const [pairSessions, setPairSessions] = useState<PairSession[]>([]);
  const [pairDevices, setPairDevices] = useState<PairedDevice[]>([]);
  const [pairError, setPairError] = useState("");

  const curRef = useRef("");
  const busyRef = useRef(false);
  // 最近一次「用户主动终止」的时间（秒）。用来过滤后端迟到的旧 running=true
  const cancelAtRef = useRef(0);

  const setRunningAction = (a: ActionKind) => {
    activeAction = a;
    setRunningActionState(a);
  };

  const pushLog = (code: string, detail = "") => {
    const entry: LogEntry = { code, ts: Math.floor(Date.now() / 1000), detail };
    logStore = [entry, ...logStore].slice(0, 10);
    setLogs(logStore);
  };

  // 失败统一走这里：界面只在「维护」显示结论码 + 时间，正文留给展开后的明细
  const fail = (fallback: string, text: string) => {
    const code = /策略拒绝|denied|policy/i.test(text) ? "POLICY_DENIED" : fallback;
    setError(text);
    pushLog(code, text);
  };

  const setBusyBoth = (v: boolean) => {
    busyRef.current = v;
    setBusy(v);
  };

  const flushAssistant = () => {
    setStreaming(curRef.current);
  };

  const finishTurn = (cancelled?: boolean) => {
    setStreaming("");
    setBusyBoth(false);
    setRunningAction("");
    setActivity("");
    if (cancelled) setError("这一轮已中断");
  };

  const loadTemplates = async () => {
    try {
      const r = (await call<[], { ok: boolean; text?: string; default?: string }>(
        "get_ask_template"
      )) as { ok: boolean; text?: string; default?: string };
      if (r.ok && r.text) {
        setAskTpl(r.text);
        setAskTplEdit(r.text);
      }
      if (r.ok && r.default) setAskTplDefault(r.default);
    } catch {
      /* 取不到就用后端默认模板 */
    }
    try {
      const r = (await call<[], { ok: boolean; text?: string; default?: string }>(
        "get_translate_template"
      )) as { ok: boolean; text?: string; default?: string };
      if (r.ok && r.text) {
        setTranslateTpl(r.text);
        setTranslateTplEdit(r.text);
      }
      if (r.ok && r.default) setTranslateTplDefault(r.default);
    } catch {
      /* 取不到就用本地兜底模板 */
    }
  };

  // ---------- 设备配对（掌机侧展示 PIN、允许/拒绝、设备列表） ----------
  const loadPairInfo = async () => {
    try {
      const r = (await call<[], {
        ok: boolean;
        sessions?: PairSession[];
        devices?: PairedDevice[];
        error?: string;
      }>("pair_info")) as {
        ok: boolean;
        sessions?: PairSession[];
        devices?: PairedDevice[];
        error?: string;
      };
      setPairSessions(r.sessions || []);
      setPairDevices(r.devices || []);
      setPairError(r.ok ? "" : r.error || "读取配对状态失败");
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
  };

  const pairRefresh = async (pairId: string) => {
    try {
      const r = (await call<[string], { ok: boolean; error?: string }>(
        "pair_refresh", pairId
      )) as { ok: boolean; error?: string };
      if (!r.ok) setPairError(r.error || "刷新 PIN 失败");
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
    await loadPairInfo();
  };

  const pairDecide = async (pairId: string, allow: boolean) => {
    try {
      const r = (await call<[string, boolean], { ok: boolean; error?: string }>(
        "pair_decide", pairId, allow
      )) as { ok: boolean; error?: string };
      if (!r.ok) setPairError(r.error || "操作失败");
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
    await loadPairInfo();
  };

  const pairRemove = async (deviceId: string) => {
    try {
      await call<[string], { ok: boolean }>("pair_remove", deviceId);
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
    await loadPairInfo();
  };

  const pairRename = async (deviceId: string, name: string) => {
    try {
      await call<[string, string], { ok: boolean }>("pair_rename", deviceId, name);
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
    await loadPairInfo();
  };

  const pairRemoveAll = async () => {
    try {
      await call<[], { ok: boolean }>("pair_remove_all");
    } catch (e: any) {
      setPairError(String(e?.message ?? e));
    }
    await loadPairInfo();
  };

  const refreshStatus = async () => {
    setProbing(true);
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      setTts(!!st.tts);
      setWithShot(!!st.with_shot);
      setStt(!!st.stt);
      setSttDuck(st.stt_duck !== false);
      setSttDuckAvailable(!!st.stt_duck_available);
      if (st.model) setModel(st.model);
      void loadTemplates();
      return st;
    } catch (e: any) {
      fail("BACKEND_DOWN", `插件后端未就绪：${String(e?.message ?? e)}`);
      return null;
    } finally {
      setProbing(false);
    }
  };

  const startBackend = async () => {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const r = (await call<[], { ok: boolean; error?: string; already?: boolean }>(
        "start_backend"
      )) as { ok: boolean; error?: string; already?: boolean };
      if (!r.ok) {
        fail("CORE_START_FAILED", r.error ?? "拉起后端失败");
        return;
      }
      await refreshStatus();
      await call<[], { ok: boolean; error?: string }>("session_open");
      await call<[], { ok: boolean }>("ready");
      pushLog("BACKEND_STARTED", "插件后端已拉起");
    } catch (e: any) {
      fail("CORE_START_FAILED", String(e?.message ?? e));
    } finally {
      setStarting(false);
    }
  };

  const runTurn = async (text: string, grab: boolean, brief = "", action: ActionKind = "") => {
    if (busyRef.current) return;
    setError("");
    setActivity("");
    setPendingPoke(null);
    curRef.current = "";
    setStreaming("");
    setRunningAction(action);
    setBusyBoth(true);

    // 抓帧期间人已经退回游戏了：这一轮不管走成什么样，最后都要回到大屏页
    let offRoute = false;
    const backToBigScreen = () => {
      if (!offRoute) return;
      offRoute = false;
      try {
        Navigation.Navigate(BIG_SCREEN_ROUTE);
      } catch {
        /* 导航回去失败就只能靠 B 键返回，不能在这里再抛一个错把原始错误盖掉 */
      }
    };

    try {
      const st = await refreshStatus();
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);

      let shotPath: string | null = null;
      let game: {
        appid?: string;
        name?: string;
        source?: string;
        confidence?: string;
        ts?: number;
      } | null = null;
      if (grab && !st.capture_ok) {
        // 抓不到就别退页白等了：capture_reason 已经是人话（桌面模式没装
        // spectacle / 核心没起来 / 缺 gst），这轮照常只发文字
        fail(
          "SHOT_FAILED",
          `当前模式暂时截不到画面：${st.capture_reason || "原因未知"}，这轮只发文字`
        );
      } else if (grab) {
        // gamescope 抓的是合成后的整屏，/deckmeido 这页本身会一起被拍进去。
        // 所以先退回游戏、等路由退场动画走完再抓，抓完立刻导航回来。
        try {
          Navigation.NavigateBack();
          offRoute = true;
        } catch (e: any) {
          // 退不出去就别抓了：抓到的只会是插件页
          throw new Error(`无法暂时退出大屏页：${String(e?.message ?? e)}`);
        }
        await wait(SHOT_LEAVE_MS);
        const snap = (await call<
          [boolean],
          {
            ok: boolean;
            shot?: string;
            thumb?: string;
            error?: string;
            needPoke?: boolean;
            game?: {
              appid?: string;
              name?: string;
              source?: string;
              confidence?: string;
              ts?: number;
            };
          }
        >("screenshot", false).catch((e: any) => ({
          ok: false,
          error: String(e?.message ?? e),
        }))) as {
          ok: boolean;
          shot?: string;
          thumb?: string;
          error?: string;
          needPoke?: boolean;
          game?: { appid?: string; name?: string };
        };
        if (snap.needPoke) {
          setPendingPoke({ text, brief, action });
          setBusyBoth(false);
          setRunningAction("");
          backToBigScreen();
          return;
        }
        if (snap.ok && snap.shot) {
          shotPath = snap.shot;
        } else {
          fail("SHOT_FAILED", `截图失败，这轮只发文字：${snap.error ?? "未知原因"}`);
        }
        game = snap.game ?? null;
      }

      let body = text;
      // 只在可信来源时注入游戏名；否则完全不提，避免模型硬套旧名字搜攻略
      const gname = game?.confidence === "high" ? (game?.name ?? "") : "";
      if (gname) {
        const aid = game?.appid ? `（appid ${game.appid}）` : "";
        body = `当前游戏：《${gname}》${aid}。\n${text}`;
      }

      // 入口标记：聊天页发起的一轮放开多轮推进；询问/翻译保持一问一答。
      // client=bigscreen 让后端区分「大屏」和「QAM 面板」，避免 QAM 也被放开
      const surface = action === "send" ? "chat" : action || "";
      const res = (await call<[string, string | null, string, string, string], { ok: boolean; error?: string }>(
        "start_turn",
        body,
        shotPath,
        brief,
        surface,
        "bigscreen"
      )) as { ok: boolean; error?: string };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

      // 这一轮已经在后端跑起来了，马上回大屏页；答复靠事件流补上，
      // 挂载后的 ready 会补推一次 running 状态，界面不会漏掉这一轮
      backToBigScreen();

      const fin = (await call<[number], { ok: boolean; text?: string; error?: string }>(
        "wait_final",
        900
      )) as { ok: boolean; text?: string; error?: string };
      if (busyRef.current) {
        if (fin.ok && fin.text) {
          curRef.current = fin.text;
          finishTurn(false);
        } else {
          throw new Error(fin.error ?? "没有收到答复");
        }
      }
    } catch (e: any) {
      fail("TURN_FAILED", String(e?.message ?? e));
      setBusyBoth(false);
      setRunningAction("");
      setActivity("");
      setStreaming("");
    } finally {
      // 任何提前返回/异常都不能把人留在游戏画面里
      backToBigScreen();
    }
  };

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || busyRef.current) return;
    await runTurn(t, withShot, "", "send");
  };

  const shotAsk = async () => {
    if (busyRef.current) return;
    await runTurn(askTpl || DEFAULT_ASK_TPL, true, SHOT_ASK_BRIEF, "ask");
  };

  const shotTranslate = async () => {
    if (busyRef.current) return;
    // 用后端持久化的翻译提示词；还没读到就用兜底常量
    await runTurn(translateTpl || DEFAULT_TRANSLATE_TPL, true, SHOT_TRANSLATE_BRIEF, "translate");
  };

  const retryPoke = async () => {
    if (busyRef.current || !pendingPoke) return;
    const p = pendingPoke;
    setPendingPoke(null);
    await runTurn(p.text, true, p.brief, p.action);
  };

  const cancel = async () => {
    cancelAtRef.current = Date.now() / 1000;
    setBusyBoth(false);
    setRunningAction("");
    setActivity("");
    try {
      await call<[], { ok: boolean; error?: string }>("cancel");
    } catch {
      /* 断开本身已经够用 */
    }
    fail("CANCELLED", "这一轮已中断");
    setPendingPoke(null);
  };

  const newSession = async () => {
    if (newing) return;
    setNewing(true);
    setError("");
    try {
      const r = (await call<[], { ok: boolean; error?: string; unchanged?: boolean }>(
        "session_new"
      )) as { ok: boolean; error?: string; unchanged?: boolean };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受");
      if (r.unchanged) {
        setError("当前已经是新会话，可以开始聊天了~");
        return;
      }
      setItems([]);
      setStreaming("");
      setActivity("");
      curRef.current = "";
      pushLog("SESSION_NEW", "已开始新会话");
    } catch (e: any) {
      fail("SESSION_NEW_FAILED", `新建会话失败：${String(e?.message ?? e)}`);
    } finally {
      setNewing(false);
    }
  };

  const loadModels = async () => {
    if (loadingModels) return;
    setLoadingModels(true);
    try {
      const r = (await call<[], { ok: boolean; model?: string; error?: string }>(
        "get_models"
      )) as { ok: boolean; model?: string; error?: string };
      if (r.ok && r.model) setModel(r.model);
    } catch {
      /* 当前模型拿不到就保持原样 */
    }
    try {
      const r = (await call<[], { ok: boolean; models?: string[]; error?: string }>(
        "refresh_models"
      )) as { ok: boolean; models?: string[]; error?: string };
      if (r.ok) setModelList(r.models || []);
    } catch {
      /* 列表拉不到就只留当前模型 */
    } finally {
      setLoadingModels(false);
    }
  };

  const changeModel = async (name: string) => {
    const prev = model;
    setModel(name);
    try {
      const r = (await call<[string], { ok: boolean; model?: string; error?: string }>(
        "set_model",
        name
      )) as { ok: boolean; model?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setModel(r.model ?? name);
      setError("");
    } catch (e: any) {
      setModel(prev);
      fail("MODEL_CHANGE_FAILED", `切换模型失败：${String(e?.message ?? e)}`);
    }
  };

  const toggleStt = async (v: boolean) => {
    setStt(v);
    try {
      const r = (await call<[boolean], { ok: boolean; stt?: boolean; error?: string }>(
        "set_stt",
        v
      )) as { ok: boolean; stt?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setStt(!!r.stt);
    } catch (e: any) {
      setStt(!v);
      fail("STT_TOGGLE_FAILED", `语音输入切换失败：${String(e?.message ?? e)}`);
    }
  };

  const toggleSttDuck = async (v: boolean) => {
    setSttDuck(v);
    try {
      const r = (await call<[boolean], { ok: boolean; stt_duck?: boolean; error?: string }>(
        "set_stt_duck",
        v
      )) as { ok: boolean; stt_duck?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setSttDuck(r.stt_duck !== false);
    } catch (e: any) {
      setSttDuck(!v);
      fail("STT_DUCK_TOGGLE_FAILED", `静音外放开关切换失败：${String(e?.message ?? e)}`);
    }
  };

  const toggleWithShot = async (v: boolean) => {
    setWithShot(v);
    try {
      const r = (await call<[boolean], { ok: boolean; with_shot?: boolean; error?: string }>(
        "set_with_shot",
        v
      )) as { ok: boolean; with_shot?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setWithShot(!!r.with_shot);
    } catch (e: any) {
      setWithShot(!v);
      fail("SHOT_TOGGLE_FAILED", `带画面开关切换失败：${String(e?.message ?? e)}`);
    }
  };

  const saveAskTpl = async () => {
    try {
      const r = (await call<[string], { ok: boolean; text?: string; error?: string }>(
        "set_ask_template",
        askTplEdit
      )) as { ok: boolean; text?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setAskTpl(r.text ?? "");
      setAskTplEdit(r.text ?? "");
      setError("");
    } catch (e: any) {
      fail("ASK_TPL_SAVE_FAILED", `提示词保存失败：${String(e?.message ?? e)}`);
    }
  };

  const saveTranslateTpl = async () => {
    try {
      const r = (await call<[string], { ok: boolean; text?: string; error?: string }>(
        "set_translate_template",
        translateTplEdit
      )) as { ok: boolean; text?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setTranslateTpl(r.text ?? "");
      setTranslateTplEdit(r.text ?? "");
      setError("");
    } catch (e: any) {
      fail("TRANSLATE_TPL_SAVE_FAILED", `翻译提示词保存失败：${String(e?.message ?? e)}`);
    }
  };

  /** 保存后端端口：QAM / 大屏共用，越界由插件后端拒绝并回传原因。 */
  const savePort = async (port: number) => {
    try {
      const r = (await call<
        [number],
        { ok: boolean; port?: number; restart_required?: boolean; error?: string }
      >("set_port", port)) as {
        ok: boolean;
        port?: number;
        restart_required?: boolean;
        error?: string;
      };
      if (!r.ok) throw new Error(r.error ?? "端口保存失败");
      await refreshStatus();
      return {
        ok: true as const,
        port: r.port ?? port,
        restartRequired: r.restart_required ?? true,
      };
    } catch (e: any) {
      return { ok: false as const, error: String(e?.message ?? e) };
    }
  };

  const coreStart = async () => {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const r = (await call<[], { ok: boolean; version?: string; error?: string }>(
        "core_start"
      )) as { ok: boolean; version?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "核心没有起来");
      await refreshStatus();
      await call<[], { ok: boolean; error?: string }>("session_open");
      await call<[], { ok: boolean }>("ready");
      pushLog("CORE_STARTED", "核心已启动");
    } catch (e: any) {
      fail("CORE_START_FAILED", `启动核心失败：${String(e?.message ?? e)}`);
    } finally {
      setStarting(false);
    }
  };

  const coreRestart = async () => {
    if (restarting) return;
    setRestarting(true);
    setError("");
    try {
      const r = (await call<[], { ok: boolean; version?: string; error?: string }>(
        "core_restart"
      )) as { ok: boolean; version?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "核心没有回来");
      await refreshStatus();
      pushLog("CORE_RESTARTED", "核心已重启");
    } catch (e: any) {
      fail("CORE_RESTART_FAILED", `重启核心失败：${String(e?.message ?? e)}`);
    } finally {
      setRestarting(false);
    }
  };

  const coreKill = async () => {
    if (killing) return;
    setKilling(true);
    setError("");
    try {
      const r = (await call<[], { ok: boolean; error?: string }>(
        "core_kill"
      )) as { ok: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "核心没有停下来");
      // 标记已落盘：后端不会再自动拉起，只有「启动核心」能恢复
      await refreshStatus();
      pushLog("CORE_CLOSED", "核心已关闭，不会自动拉起");
    } catch (e: any) {
      fail("CORE_CLOSE_FAILED", `关闭核心失败：${String(e?.message ?? e)}`);
    } finally {
      setKilling(false);
    }
  };

  useEffect(() => {
    void refreshStatus();
  }, []);

  useEffect(() => {
    (async () => {
      try {
        await call<[], { ok: boolean; error?: string }>("session_open");
        await call<[], { ok: boolean }>("ready");
      } catch {
        /* 连不上会在 get_status 里体现 */
      }
    })();
  }, []);

  useEffect(() => {
    const onDelta = (msg: Delta) => {
      if (msg.kind === "thread") {
        setItems(msg.items);
        return;
      }
      if (msg.kind === "pad_order") {
        void applyPadOrder(msg.action);
        return;
      }
      if (msg.kind === "status") {
        // since 是后端记的「这一轮什么时候开始的」时间戳（秒）。
        // 用户刚点了终止时，后端可能还排着一条旧的 running=true 广播；
        // 只要它的开始时间不晚于我们终止的时间，就属于已经掐掉的那一轮，
        // 不能把思考指示重新点亮。
        const since = Number(msg.since || 0);
        if (msg.running && !busyRef.current) {
          if (since > 0 && since <= cancelAtRef.current) return;
          // 可能是别的端已经发起了一轮；大屏页打开后要接着显示这轮的状态
          setBusyBoth(true);
        } else if (!msg.running && busyRef.current) {
          // 后端明确说停了：不管是不是我们点的终止，指示都必须灭
          setBusyBoth(false);
          setRunningAction("");
          if (!cancelAtRef.current) fail("BACKEND_DOWN", "这一轮没有收到答复，可能是后端断开了");
        }
        return;
      }
      if (!busyRef.current) return;
      if (msg.kind === "tool") {
        setActivity(msg.text);
        return;
      }
      if (msg.kind === "delta") {
        curRef.current += msg.text;
        flushAssistant();
      } else {
        curRef.current = msg.text;
        finishTurn(msg.cancelled);
      }
    };
    addEventListener<[Delta]>("deckmeido_delta", onDelta);
    return () => removeEventListener("deckmeido_delta", onDelta);
  }, []);

  useEffect(() => {
    if (!busy) return;
    const t0 = Date.now();
    setElapsed(0);
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - t0) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [busy]);

  return {
    status,
    error,
    items,
    streaming,
    busy,
    runningAction,
    elapsed,
    activity,
    logs,
    withShot,
    stt,
    sttDuck,
    sttDuckAvailable,
    toggleSttDuck,
    toggleWithShot,
    tts,
    model,
    modelList,
    loadingModels,
    askTpl,
    askTplEdit,
    setAskTplEdit,
    askTplDefault,
    translateTpl,
    translateTplEdit,
    setTranslateTplEdit,
    translateTplDefault,
    probing,
    restarting,
    starting,
    killing,
    newing,
    pendingPoke,
    pairSessions,
    pairDevices,
    pairError,
    loadPairInfo,
    pairRefresh,
    pairDecide,
    pairRemove,
    pairRename,
    pairRemoveAll,
    refreshStatus,
    startBackend,
    send,
    shotAsk,
    shotTranslate,
    retryPoke,
    cancel,
    newSession,
    loadModels,
    changeModel,
    toggleStt,
    saveAskTpl,
    saveTranslateTpl,
    savePort,
    coreStart,
    coreRestart,
    coreKill,
    setError,
  };
}
