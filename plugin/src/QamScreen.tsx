import { useEffect, useRef, useState } from "react";
import { addEventListener, call, removeEventListener } from "@decky/api";
import {
  DialogButton,
  DropdownItem,
  Focusable,
  Navigation,
  PanelSection,
  PanelSectionRow,
  TextField,
  ToggleField,
} from "@decky/ui";
import { ACCENT, DIM, ERR_COLOR, ImeTextArea, Md, TEXT, copyText } from "./ui";
import { statusLineFor } from "./statusLine";
import { bumpOnce, stepTab } from "./tabNav";
import { applyPadOrder, steamClient } from "./padOrder";
import { useVoiceInput } from "./useVoiceInput";
import {
  BIG_SCREEN_ROUTE,
  DEFAULT_ASK_TPL,
  DEFAULT_TRANSLATE_TPL,
  Delta,
  Item,
  LogEntry,
  PairedDevice,
  PairSession,
  SHOT_ASK_BRIEF,
  SHOT_TRANSLATE_BRIEF,
  Status,
} from "./useSession";

declare const __PLUGIN_VERSION__: string;
const PLUGIN_VERSION =
  typeof __PLUGIN_VERSION__ === "string" ? __PLUGIN_VERSION__ : "—";

// QAM 面板只有 300~400px 宽，按窄屏重排：整页固定高度，消息区吃满剩余空间，
// 减少一长串纵向滚动。设置页内部允许滚动，但一屏最多露出三张卡。
//
// 高度不能写死 460：QAM 的可用高度随分辨率和侧栏状态变化，写死会比可视区高，
// 面板整体被外层滚动裁掉一截 —— 手柄焦点滚到底就成了「一块空白」。
// 这里取「460 与可视区高度」的较小值，短屏自动收缩，长屏仍按原比例。
const PAGE_H = "min(460px, calc(100vh - 90px))";
const TAB_H = 26;
const ACTION_H = 34;
const INPUT_H = 32;
const GAP = 8;

const TABS = ["询问", "聊天", "设置"] as const;

const TITLE = "#eaf2fb";
/** 配对结果色：允许走绿、拒绝/失败走琥珀，与卡片现有配色同一套 */
const PAIR_OK = "#7ee0a8";
const PAIR_WARN = "#e0a33a";
/** 允许/拒绝按钮的完成态：沿用 Btn 的 tint 风格，不新增组件 */
const PAIR_ALLOW_DONE: React.CSSProperties = {
  background: "rgba(72,199,142,.22)",
  border: "1px solid rgba(94,214,160,.55)",
  color: "#a7edcb",
};
const PAIR_DENY_DONE: React.CSSProperties = {
  background: "rgba(224,108,117,.18)",
  border: "1px solid rgba(224,108,117,.42)",
  color: "#f0c3c7",
};

/** QAM 提示词多行框：固定 84px（11.5px 字号约 5 行），内部滚动 */
const qamTplStyle: React.CSSProperties = {
  width: "100%",
  height: 84,
  minHeight: 84,
  maxHeight: 84,
  boxSizing: "border-box",
  padding: "6px 8px",
  resize: "none",
  overflowY: "auto",
  borderRadius: 4,
  border: "1px solid rgba(90,169,230,.22)",
  borderLeft: `2px solid ${ACCENT}`,
  background: "rgba(14,20,28,.92)",
  color: TEXT,
  fontSize: 11.5,
  lineHeight: 1.45,
  fontFamily: "inherit",
  outline: "none",
};

const dividerStyle: React.CSSProperties = {
  height: 1,
  flexShrink: 0,
  margin: "0 2px",
  background: "rgba(139,146,154,.30)",
};

const fmtLogTime = (ts: number) => {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

// 页签记忆：截图要收 QAM，面板重开时组件会重新挂载，
// 用模块级变量记住上次停在哪一页，不让用户每次都回到询问页
let lastTab = 0;
// 当前这一轮是从哪个入口发起的：面板重开时 busy 由后端补推，
// 但「哪个按钮在跑」只存在前端，放在模块级才不会丢
type ActionKind = "" | "ask" | "translate" | "send";
let activeAction: ActionKind = "";
// 最近事件：QAM 重挂后仍保留，设置页维护卡片只显示结论码
let logStore: LogEntry[] = [];

const pushLogStore = (code: string, detail = "") => {
  const entry: LogEntry = { code, ts: Math.floor(Date.now() / 1000), detail };
  logStore = [entry, ...logStore].slice(0, 10);
  return logStore;
};

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** 紧凑按钮：窄屏里统一高度和最小宽度，避免 DialogButton 默认 min-width 撑爆行 */
function Btn({
  children,
  onClick,
  disabled,
  primary,
  danger,
  running,
  height = ACTION_H,
  size = 12,
  description,
  style,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
  danger?: boolean;
  running?: boolean;
  height?: number;
  size?: number;
  description?: string;
  style?: React.CSSProperties;
}) {
  return (
    <DialogButton
      disabled={disabled}
      onClick={() => onClick()}
      onOKActionDescription={description ?? "确认"}
      onCancelActionDescription="返回"
      style={{
        width: "100%",
        minWidth: 0,
        height,
        minHeight: height,
        fontSize: size,
        lineHeight: 1.2,
        padding: "0 6px",
        boxSizing: "border-box",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
        borderRadius: 4,
        background: danger
          ? "rgba(224,108,117,.20)"
          : running
            ? "rgba(90,169,230,.18)"
            : primary
              ? "rgba(90,169,230,.28)"
              : "rgba(255,255,255,.08)",
        border: `1px solid ${
          danger
            ? "rgba(224,108,117,.45)"
            : running
              ? "rgba(90,169,230,.55)"
              : primary
                ? "rgba(90,169,230,.55)"
                : "rgba(255,255,255,.12)"
        }`,
        color: danger ? "#f0c3c7" : primary ? TITLE : running ? "#d9ecff" : TEXT,
        ...style,
      }}
    >
      {children}
    </DialogButton>
  );
}

/** 设置页卡片：统一内边距、卡片间距和左强调条 */
function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div
      style={{
        width: "100%",
        boxSizing: "border-box",
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: GAP,
        minWidth: 0,
        borderRadius: 4,
        background: "rgba(14,20,28,.92)",
        border: "1px solid rgba(90,169,230,.10)",
        borderLeft: `2px solid ${ACCENT}`,
      }}
    >
      <div style={{ fontSize: 11, letterSpacing: 1.5, color: ACCENT, fontWeight: 600 }}>
        {title}
      </div>
      {children}
    </div>
  );
}

/** 连接卡里的两列读数：标签 10px、值 11px，窄屏下一行也不挤 */
function StatCell({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{ minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
      <span style={{ fontSize: 10, color: DIM, lineHeight: 1.2 }}>{label}</span>
      <span
        style={{
          fontSize: 11,
          color: "#bcd6ea",
          lineHeight: 1.4,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {value}
      </span>
    </div>
  );
}

function Bubble({
  mine,
  text,
  image,
  streaming,
}: {
  mine?: boolean;
  text: string;
  image?: string;
  streaming?: boolean;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: mine ? "flex-end" : "flex-start",
        width: "100%",
        minWidth: 0,
      }}
    >
      <div
        style={{
          maxWidth: "100%",
          // 有图时吃满整行（百分比宽度会把 fit-content 撑大），纯文字按内容收缩
          width: image ? "100%" : "fit-content",
          minWidth: 0,
          padding: "6px 8px",
          borderRadius: 4,
          background: mine ? "rgba(90,169,230,.16)" : "rgba(255,255,255,.06)",
          borderLeft: mine ? "2px solid transparent" : `2px solid ${ACCENT}`,
          borderRight: mine ? `2px solid ${ACCENT}` : "2px solid transparent",
          boxSizing: "border-box",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          overflowWrap: "anywhere",
          fontSize: 12,
          lineHeight: 1.5,
        }}
      >
        {image && (
          <img
            src={image}
            style={{
              display: "block",
              maxWidth: "100%",
              maxHeight: 180,
              width: "auto",
              height: "auto",
              objectFit: "contain",
              marginBottom: text ? 6 : 0,
              borderRadius: 4,
            }}
          />
        )}
        <Md text={text} align={mine ? "right" : "left"} size={12} />
        {streaming && <span style={{ color: DIM }}>▍</span>}
      </div>
    </div>
  );
}

/** 询问页与聊天页共用同一个消息区：读同一份 items，切页签不丢消息 */
function ChatLog({
  items,
  streaming,
  busy,
  elapsed,
  emptyText,
  scrollRef,
}: {
  items: Item[];
  streaming: string;
  busy: boolean;
  elapsed: number;
  emptyText?: string;
  scrollRef: React.RefObject<HTMLDivElement | null>;
}) {
  return (
    <Focusable
      ref={scrollRef as React.Ref<HTMLDivElement>}
      onOKActionDescription="浏览"
      style={{
        flex: 1,
        minHeight: 0,
        width: "100%",
        overflowY: "auto",
        display: "block",
        boxSizing: "border-box",
        paddingRight: 2,
        // 让焦点滚动停在内容末端，不要多滚出一个空档
        overscrollBehavior: "contain",
        scrollPaddingBottom: 4,
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 6, width: "100%" }}>
        {items.map((m, i) => (
          <Bubble key={`${m.ts}-${i}`} mine={m.role === "user"} text={m.text} image={m.image} />
        ))}
        {streaming && <Bubble text={streaming} streaming />}
        {busy && !streaming && <Bubble text={`思考中…（${elapsed}s）`} />}
        {!items.length && !streaming && !busy && emptyText && (
          <div style={{ color: DIM, textAlign: "center", fontSize: 11, padding: "24px 0" }}>
            {emptyText}
          </div>
        )}
      </div>
    </Focusable>
  );
}

/**
 * QAM 专用会话状态。
 *
 * 大屏页的 useSession 抓帧前后走的是路由导航（NavigateBack / Navigate），
 * QAM 这边必须收起/重开侧栏（CloseSideMenus / OpenQuickAccessMenu），
 * 两条流程不能共用，所以这里保留一份等价的实现；事件流、后端调用和
 * surface 规则都与大屏一致。
 */
function useQamSession() {
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
  const [translateTpl, setTranslateTpl] = useState(DEFAULT_TRANSLATE_TPL);
  const [translateTplEdit, setTranslateTplEdit] = useState(DEFAULT_TRANSLATE_TPL);
  const [translateTplDefault, setTranslateTplDefault] = useState(DEFAULT_TRANSLATE_TPL);
  const [probing, setProbing] = useState(true);
  const [restarting, setRestarting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [killing, setKilling] = useState(false);
  const [newing, setNewing] = useState(false);
  const [pendingPoke, setPendingPoke] = useState<
    { text: string; brief: string; hideQam: boolean; action: ActionKind } | null
  >(null);
  const [runningAction, setRunningActionState] = useState<ActionKind>(activeAction);
  const [logs, setLogs] = useState<LogEntry[]>(logStore);
  const [pairSessions, setPairSessions] = useState<PairSession[]>([]);
  const [pairDevices, setPairDevices] = useState<PairedDevice[]>([]);
  const [pairError, setPairError] = useState("");
  // 点「允许/拒绝」后的即时结果：后端在浏览器确认前仍会返回 approved 会话，
  // 只靠 2 秒轮询看不出变化，所以本地先记一份并渲染成完成态
  const [pairDecisions, setPairDecisions] = useState<Record<string, boolean>>({});
  const [pairBusy, setPairBusy] = useState<Record<string, "allow" | "deny">>({});
  const [pairFlash, setPairFlash] = useState<{ text: string; ok: boolean } | null>(null);
  const pairBusyRef = useRef<Record<string, boolean>>({});
  const pairFlashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const curRef = useRef("");
  const busyRef = useRef(false);

  const setRunningAction = (a: ActionKind) => {
    activeAction = a;
    setRunningActionState(a);
  };

  const pushLog = (code: string, detail = "") => {
    setLogs([...pushLogStore(code, detail)]);
  };

  // 失败统一收敛成结论码：正文只留在设置页维护卡片里
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

  // ---------- 设备配对（QAM 侧展示 PIN、允许/拒绝、设备列表） ----------
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

  /** 面板顶部的单行提示：停留 3 秒后自动消失，不打断焦点 */
  const showPairFlash = (text: string, ok: boolean) => {
    if (pairFlashTimer.current !== null) clearTimeout(pairFlashTimer.current);
    setPairFlash({ text, ok });
    pairFlashTimer.current = setTimeout(() => {
      pairFlashTimer.current = null;
      setPairFlash(null);
    }, 3000);
  };

  // 面板卸载时清掉定时器，避免回到已销毁的组件里 setState
  useEffect(
    () => () => {
      if (pairFlashTimer.current !== null) clearTimeout(pairFlashTimer.current);
    },
    []
  );

  const pairDecide = async (pairId: string, allow: boolean) => {
    // 同一会话的按钮只认第一次点击，防止连点重复提交
    if (pairBusyRef.current[pairId]) return;
    pairBusyRef.current[pairId] = true;
    const sess = pairSessions.find((p) => p.pair_id === pairId);
    const deviceName = sess?.name || "未知设备";
    setPairBusy((prev) => ({ ...prev, [pairId]: allow ? "allow" : "deny" }));
    setPairError("");
    let ok = false;
    let err = "";
    try {
      const r = (await call<[string, boolean], { ok: boolean; error?: string }>(
        "pair_decide", pairId, allow
      )) as { ok: boolean; error?: string };
      ok = !!r.ok;
      if (!ok) err = r.error || "操作失败";
    } catch (e: any) {
      err = String(e?.message ?? e);
    }
    delete pairBusyRef.current[pairId];
    setPairBusy((prev) => {
      const next = { ...prev };
      delete next[pairId];
      return next;
    });
    if (ok) {
      setPairDecisions((prev) => ({ ...prev, [pairId]: allow }));
      showPairFlash(allow ? `已允许 ${deviceName} 配对` : `已拒绝 ${deviceName} 的配对`, allow);
    } else {
      // 失败时不改会话状态，按钮回到可点，同时把原因留在卡片内
      setPairError(err);
      showPairFlash(`${allow ? "允许" : "拒绝"}失败：${err}`, false);
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

  /** 保存后端端口：与网页/大屏共用 [server].port。 */
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

  const restoreQam = () => {
    try {
      Navigation.OpenQuickAccessMenu(999);
    } catch {
      try {
        Navigation.OpenQuickAccessMenu();
      } catch {
        /* 面板已经开着时再开一次会抛，忽略即可 */
      }
    }
  };

  const restoreQamSafe = async () => {
    restoreQam();
  };

  const openBigScreen = () => {
    try {
      Navigation.Navigate(BIG_SCREEN_ROUTE);
      Navigation.CloseSideMenus();
    } catch (e: any) {
      setError(`打开大屏失败：${String(e?.message ?? e)}`);
    }
  };

  const runTurn = async (
    text: string,
    grab: boolean,
    hideQam: boolean,
    brief = "",
    action: ActionKind = ""
  ) => {
    if (busyRef.current) return;
    setError("");
    setActivity("");
    setPendingPoke(null);
    curRef.current = "";
    setStreaming("");
    setRunningAction(action);
    setBusyBoth(true);

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
        // 抓不到就别收侧栏白等了：capture_reason 已经是人话（桌面模式没装
        // spectacle / 核心没起来 / 缺 gst），这轮照常只发文字
        fail(
          "SHOT_FAILED",
          `当前模式暂时截不到画面：${st.capture_reason || "原因未知"}，这轮只发文字`
        );
      } else if (grab) {
        // QAM 收起后 Steam 还要重绘一帧游戏画面，抓早了会把正在消失的侧栏拍进去
        if (hideQam) Navigation.CloseSideMenus();
        if (hideQam) await wait(1200);
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
        >("screenshot", hideQam).catch((e: any) => ({
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
        if (hideQam) await restoreQamSafe();
        if (snap.needPoke) {
          setPendingPoke({ text, brief, hideQam, action });
          setBusyBoth(false);
          setRunningAction("");
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
      // 只有可信来源才注入游戏名，避免模型硬套旧名字去搜攻略
      const gname = game?.confidence === "high" ? (game?.name ?? "") : "";
      if (gname) {
        const aid = game?.appid ? `（appid ${game.appid}）` : "";
        body = `当前游戏：《${gname}》${aid}。\n${text}`;
      }

      // surface 决定后端是否放开多轮：聊天页 = chat，询问/翻译保持一问一答
      const surface = action === "send" ? "chat" : action;
      const res = (await call<
        [string, string | null, string, string],
        { ok: boolean; error?: string }
      >("start_turn", body, shotPath, brief, surface)) as {
        ok: boolean;
        error?: string;
      };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

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
      if (grab && hideQam) void restoreQamSafe();
    }
  };

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || busyRef.current) return;
    // 带画面发送时先收起 QAM 再抓帧，否则拍到的是「面板 + 游戏」
    await runTurn(t, withShot, withShot, "", "send");
  };

  const shotAsk = async () => {
    if (busyRef.current) return;
    await runTurn(askTpl || DEFAULT_ASK_TPL, true, true, SHOT_ASK_BRIEF, "ask");
  };

  const shotTranslate = async () => {
    if (busyRef.current) return;
    await runTurn(translateTpl || DEFAULT_TRANSLATE_TPL, true, true, SHOT_TRANSLATE_BRIEF, "translate");
  };

  const retryPoke = async () => {
    if (busyRef.current || !pendingPoke) return;
    const p = pendingPoke;
    setPendingPoke(null);
    await runTurn(p.text, true, p.hideQam, p.brief, p.action);
  };

  const cancel = async () => {
    setBusyBoth(false);
    setRunningAction("");
    setActivity("");
    try {
      await call<[], { ok: boolean; error?: string }>("cancel");
    } catch {
      /* 断开本身已经够用，取消失败不再打扰用户 */
    }
    fail("CANCELLED", "这一轮已中断");
    setPendingPoke(null);
    restoreQam();
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

  const toggleTts = async (v: boolean) => {
    setTts(v);
    try {
      const r = (await call<[boolean], { ok: boolean; tts?: boolean; error?: string }>(
        "set_tts",
        v
      )) as { ok: boolean; tts?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setTts(!!r.tts);
    } catch (e: any) {
      setTts(!v);
      fail("TTS_TOGGLE_FAILED", `语音播报切换失败：${String(e?.message ?? e)}`);
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

  /** 把当前生效提示词存成当前游戏的预设；反馈走维护事件（带游戏名） */
  const savePreset = async () => {
    try {
      const r = (await call<[], {
        ok: boolean;
        game_name?: string;
        title?: string;
        error?: string;
      }>("save_preset")) as {
        ok: boolean;
        game_name?: string;
        title?: string;
        error?: string;
      };
      if (!r.ok) throw new Error(r.error ?? "保存失败");
      const where = r.game_name || r.title || "当前游戏";
      pushLog("PRESET_SAVED", `已保存《${where}》的预设`);
      setError("");
      await refreshStatus();
    } catch (e: any) {
      fail("PRESET_SAVE_FAILED", `预设保存失败：${String(e?.message ?? e)}`);
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
        if (msg.action === "to1p" || msg.action === "revert") applyPadOrder(msg.action);
        return;
      }
      if (msg.kind === "status") {
        if (msg.running && !busyRef.current) {
          // 面板收起期间由别的入口发起：重开后接着显示这轮状态
          setBusyBoth(true);
        } else if (!msg.running && busyRef.current) {
          setBusyBoth(false);
          setRunningAction("");
          fail("BACKEND_DOWN", "这一轮没有收到答复，可能是后端断开了");
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
    toggleStt,
    tts,
    toggleTts,
    model,
    modelList,
    loadingModels,
    askTplEdit,
    setAskTplEdit,
    askTplDefault,
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
    pairDecisions,
    pairBusy,
    pairFlash,
    loadPairInfo,
    pairRefresh,
    pairDecide,
    pairRemove,
    pairRename,
    pairRemoveAll,
    savePort,
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
    saveAskTpl,
    saveTranslateTpl,
    savePreset,
    coreStart,
    coreRestart,
    coreKill,
    openBigScreen,
  };
}

export default function QamScreen() {
  const s = useQamSession();
  const [tab, setTabState] = useState(lastTab);
  // 设备改名草稿：按 device_id 保存输入框里的值，保存后才写回后端
  const [renameDrafts, setRenameDrafts] = useState<Record<string, string>>({});
  // 端口草稿与提示：保存后提示需重启，改一处另两处下次读取即生效
  const [portDraft, setPortDraft] = useState("");
  const [portHint, setPortHint] = useState("");
  useEffect(() => {
    if (!portDraft && s.status?.port) setPortDraft(String(s.status.port));
  }, [s.status?.port]);
  const savePortDraft = async () => {
    const n = Number(portDraft.trim());
    if (!Number.isInteger(n) || n < 1024 || n > 65535) {
      setPortHint("端口需为 1024-65535 之间的整数");
      return;
    }
    const r = await s.savePort(n);
    if (!r.ok) {
      setPortHint(r.error ?? "端口保存失败");
      return;
    }
    setPortHint(`已保存端口 ${r.port}，重启后端后生效`);
  };
  const setTab = (t: number | ((prev: number) => number)) => {
    setTabState((prev) => {
      const next = typeof t === "function" ? t(prev) : t;
      lastTab = next;
      return next;
    });
  };
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 语音结果追加到已有输入后面，不覆盖已经打的字
  const voice = useVoiceInput((t) =>
    setInput((prev) => (prev.trim() ? prev.replace(/\s*$/, " ") + t : t))
  );

  // 语音状态提示 + 下载进度：窄屏单独占一行，避免压住输入框和消息区
  const qdl = voice.download;
  const qdlActive = !!voice.downloading;
  const qdlPercent = Math.max(0, Math.min(100, qdl?.percent ?? 0));
  const qVoiceTip = (() => {
    if (voice.ready || qdlActive) return null;
    if (voice.downloadError) return { text: voice.downloadError, color: ERR_COLOR, canDownload: true };
    if (voice.error) return { text: voice.error, color: ERR_COLOR, canDownload: false };
    const st = voice.status;
    if (!st) return { text: "语音状态读取中…", color: DIM, canDownload: false };
    if (!st.ok) return { text: "语音后端未连上", color: DIM, canDownload: false };
    if (st.enable === false) return { text: "语音已在设置里关闭", color: DIM, canDownload: false };
    if (!st.runtime) return { text: "后端没装 sherpa-onnx", color: DIM, canDownload: false };
    if (!st.model) return { text: "语音模型未下载（约 1GB）", color: DIM, canDownload: true };
    if (st.record === false) return { text: st.record_reason || "录音设备不可用", color: DIM, canDownload: false };
    return null;
  })();

  // 窄屏按时间顺序排列，最新一条贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.items, s.streaming, tab]);

  // 进设置页拉一次模型列表；配对信息每 2 秒刷新一次（PIN 倒计时/新设备提示）
  useEffect(() => {
    if (tab !== 2) return;
    void s.loadModels();
    void s.loadPairInfo();
    const timer = setInterval(() => void s.loadPairInfo(), 2000);
    return () => clearInterval(timer);
  }, [tab]);

  // L1 / R1 翻页：页签事件与大屏共用 tabNav 的同一套按键映射。
  // 原来这里按 6/7 判断（把 6 当 L1），实际 BUMPER_LEFT=5、BUMPER_RIGHT=6，
  // 于是 L1 完全没绑上、R1 反而变成「上一页」——用户看到的就是切换失效。
  useEffect(() => {
    let reg: any = null;
    try {
      const api = steamClient()?.Input;
      if (api && typeof api.RegisterForControllerInputMessages === "function") {
        reg = api.RegisterForControllerInputMessages((msg: any) => {
          if (!msg || msg.is_repeat) return;
          const step = bumpOnce(msg.button ?? msg.nButton);
          if (step) setTab((p) => stepTab(p, step, TABS.length));
        });
      }
    } catch {
      // 绑定失败不影响使用：页签仍可点按切换
    }
    return () => {
      try {
        reg?.unregister?.();
      } catch {
        /* 解绑失败不影响卸载 */
      }
    };
  }, []);

  const submit = () => {
    const text = input.trim();
    if (!text || s.busy) return;
    setInput("");
    void s.send(text);
  };

  const configError = s.status?.config_error || "";
  const coreRunning = !!s.status?.core_ok;
  const coreStopped = !!s.status?.core_stopped;
  const coreLine = s.status
    ? coreRunning
      ? "核心运行中"
      : coreStopped
        ? "核心已停止"
        : "核心异常"
    : s.probing
      ? "连接中"
      : "未连上";
  const coreColor = coreRunning ? ACCENT : coreStopped ? DIM : ERR_COLOR;

  // 询问页忙碌态：被点的那格原地显示「思考中…」，另一格换成「中断」
  const askRunning = s.busy && s.runningAction !== "translate";
  const translateRunning = s.busy && s.runningAction === "translate";

  const shell = (children: React.ReactNode) => (
    <PanelSection>
      <PanelSectionRow>
        {/* 根节点做成 Focusable：焦点在面板内任意控件上时，L1/R1 也会冒泡到这里。
            它和上面的控制器消息回调共用 bumpOnce 去重，同一次按键只翻一页。 */}
        <Focusable
          onButtonDown={(evt) => {
            const d = evt?.detail;
            if (!d || d.is_repeat) return;
            const step = bumpOnce(d.button);
            if (step) setTab((p) => stepTab(p, step, TABS.length));
          }}
          style={{
            width: "100%",
            height: PAGE_H,
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            minWidth: 0,
            minHeight: 0,
            // 内容不能溢出到固定高度之外，否则外层滚动会出现空白带
            overflow: "hidden",
            boxSizing: "border-box",
          }}
        >
          {children}
        </Focusable>
      </PanelSectionRow>
    </PanelSection>
  );

  // 页签两端的 L1/R1 小提示：只做视觉指引，不参与焦点，也不挤压页签文字。
  // 视觉语言与大屏页头的 KeyCap 一致（白底黑字小键帽），QAM 里缩到 8.5px，
  // 在 300px 宽的面板里也不换行、不盖住页签。
  // reverse 只换排列方向，键帽与箭头的样式完全相同：左端「‹ L1」、右端「R1 ›」，
  // 箭头始终朝外，两端镜像对称。
  const padHint = (arrow: string, label: string, reverse = false) => (
    <span
      aria-hidden
      style={{
        flexShrink: 0,
        display: "flex",
        flexDirection: reverse ? "row-reverse" : "row",
        alignItems: "center",
        gap: 2,
        height: TAB_H,
        padding: "0 2px",
        boxSizing: "border-box",
        whiteSpace: "nowrap",
        userSelect: "none",
        color: "rgba(139,146,154,.9)",
        fontSize: 9.5,
        lineHeight: 1,
      }}
    >
      {arrow}
      <span
        style={{
          flexShrink: 0,
          background: "#fff",
          color: "#000",
          fontWeight: 700,
          fontSize: 8.5,
          lineHeight: 1.3,
          borderRadius: 3,
          padding: "0 3px",
        }}
      >
        {label}
      </span>
    </span>
  );

  const tabRow = (
    <Focusable
      style={{ display: "flex", gap: 6, width: "100%", height: TAB_H, flexShrink: 0 }}
      onCancelActionDescription="返回"
    >
      {padHint("‹", "L1")}
      {TABS.map((name, i) => (
        <Focusable
          key={name}
          autoFocus={i === tab}
          onActivate={() => setTab(i)}
          onOKActionDescription="切换页签"
          onCancelActionDescription="返回"
          style={{
            flex: "1 1 0",
            minWidth: 0,
            height: TAB_H,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            boxSizing: "border-box",
            fontSize: 12,
            whiteSpace: "nowrap",
            borderRadius: 4,
            background: tab === i ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
            color: tab === i ? TITLE : DIM,
          }}
        >
          {name}
        </Focusable>
      ))}
      {padHint("›", "R1", true)}
    </Focusable>
  );

  // 没有状态：只留连接卡片和重试
  if (!s.status) {
    return shell(
      <>
        <Card title="连接">
          <div style={{ fontSize: 12, color: TEXT }}>
            {s.probing ? "正在连接插件后端…" : "插件后端未连上"}
          </div>
          <div style={{ fontSize: 11, color: DIM, lineHeight: 1.5 }}>
            后端由 Decky 插件管理；一直连不上就重试，或在设置页启动后端。
          </div>
          <Btn primary onClick={() => void s.refreshStatus()} description="重试">
            重试连接
          </Btn>
          <Btn height={26} size={11} onClick={s.openBigScreen} description="打开大屏">
            打开大屏
          </Btn>
        </Card>
      </>
    );
  }

  // 读不到后端配置：只留明确的错误卡片，不再让用户猜口令
  if (configError) {
    return shell(
      <>
        <Card title="读不到后端配置">
          <div style={{ fontSize: 12, color: TEXT }}>读不到后端配置：{configError}</div>
          <div style={{ fontSize: 11, color: DIM, lineHeight: 1.5 }}>
            检查数据目录里的 config.toml（通常在 userdata/），确认能读到后重试。
          </div>
          <Btn primary onClick={() => void s.refreshStatus()} description="重试">
            重试连接
          </Btn>
          <Btn height={26} size={11} onClick={s.openBigScreen} description="打开大屏">
            打开大屏
          </Btn>
        </Card>
      </>
    );
  }

  return shell(
    <>
      {tabRow}

      {/* ================= 询问页 ================= */}
      {tab === 0 && (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            width: "100%",
          }}
        >
          {/* 状态行只放结论：昵称状态 + 核心地址；错误码/明细只进设置页维护卡 */}
          <div
            style={{
              height: 16,
              flexShrink: 0,
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 11,
              lineHeight: "16px",
              minWidth: 0,
            }}
          >
            <span
              style={{
                color: coreColor,
                flex: 1,
                minWidth: 0,
                whiteSpace: "nowrap",
                overflow: "hidden",
                textOverflow: "ellipsis",
              }}
            >
              {coreRunning ? statusLineFor(s.status, s.busy) : coreLine}
            </span>
            <span style={{ color: DIM, flexShrink: 0 }}>
              {s.status ? `${s.status.ip}:${s.status.port}` : "—"}
            </span>
          </div>

          {/* 画面静止的提示：只在需要时占一行，不常驻 */}
          {s.pendingPoke && (
            <div
              style={{
                flexShrink: 0,
                height: 30,
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "0 10px",
                borderRadius: 4,
                background: "rgba(255,255,255,.06)",
                border: "1px solid rgba(255,255,255,.10)",
                boxSizing: "border-box",
              }}
            >
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 11,
                  color: DIM,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                画面静止，动一下摇杆再截
              </span>
              <div style={{ flex: "0 0 92px", width: 92 }}>
                <Btn
                  height={24}
                  size={11}
                  onClick={() => void s.retryPoke()}
                  description="重新截图"
                >
                  重新截图
                </Btn>
              </div>
            </div>
          )}

          {/* 两个等宽动作按钮：忙时被点的那格留在原位，另一格变中断 */}
          <div
            style={{
              display: "flex",
              gap: 6,
              width: "100%",
              height: ACTION_H,
              flexShrink: 0,
            }}
          >
            <div style={{ flex: "1 1 0", minWidth: 0 }}>
              {!s.busy ? (
                <Btn primary onClick={() => void s.shotAsk()} description="截屏询问">
                  截屏询问
                </Btn>
              ) : askRunning ? (
                <Btn running onClick={() => {}} description="思考中">
                  思考中…（{s.elapsed}s）
                </Btn>
              ) : (
                <Btn danger onClick={() => void s.cancel()} description="中断">
                  中断
                </Btn>
              )}
            </div>
            <div style={{ flex: "1 1 0", minWidth: 0 }}>
              {!s.busy ? (
                <Btn onClick={() => void s.shotTranslate()} description="截屏翻译">
                  截屏翻译
                </Btn>
              ) : translateRunning ? (
                <Btn running onClick={() => {}} description="思考中">
                  思考中…（{s.elapsed}s）
                </Btn>
              ) : (
                <Btn danger onClick={() => void s.cancel()} description="中断">
                  中断
                </Btn>
              )}
            </div>
          </div>

          <div style={dividerStyle} />

          <ChatLog
            items={s.items}
            streaming={s.streaming}
            // 只有聊天页发起的那轮才显示思考占位；询问/翻译由上面的按钮反馈
            busy={s.busy && s.runningAction === "send"}
            elapsed={s.elapsed}
            emptyText="还没有对话，先截个图或去聊天页提问吧"
            scrollRef={scrollRef}
          />
        </div>
      )}

      {/* ================= 聊天页 ================= */}
      {tab === 1 && (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            width: "100%",
          }}
        >
          {/* 输入行：输入框 + 语音 + 发送，三者等高 */}
          <div
            style={{
              display: "flex",
              alignItems: "stretch",
              gap: 6,
              width: "100%",
              height: INPUT_H,
              flexShrink: 0,
            }}
          >
            <Focusable
              onOKActionDescription="输入"
              style={{
                flex: 1,
                minWidth: 0,
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr)",
                gridAutoFlow: "column",
                padding: 0,
              }}
            >
              <TextField
                value={input}
                placeholder="问问 Meido..."
                onChange={(e) => setInput(e.target.value)}
                style={{
                  width: "100%",
                  maxWidth: "100%",
                  height: INPUT_H,
                  minHeight: INPUT_H,
                  padding: "0 8px",
                  boxSizing: "border-box",
                  fontSize: 12,
                  lineHeight: `${INPUT_H - 2}px`,
                  borderRadius: 4,
                  border: "1px solid rgba(255,255,255,.14)",
                  background: "rgba(255,255,255,.08)",
                  color: TEXT,
                }}
              />
            </Focusable>
            <div style={{ flex: "0 0 48px", width: 48, height: INPUT_H }}>
              <Btn
                height={INPUT_H}
                size={11}
                disabled={
                  (s.busy && s.runningAction === "send") ||
                  voice.phase === "working" ||
                  !voice.ready
                }
                onClick={() => (voice.phase === "recording" ? voice.stop() : voice.start())}
                description={
                  !voice.ready
                    ? voice.status?.model === false
                      ? "语音模型未下载"
                      : "语音输入不可用"
                    : voice.phase === "recording"
                      ? "松开结束"
                      : "语音输入"
                }
                style={{
                  background:
                    voice.phase === "recording" ? ACCENT : "rgba(255,255,255,.08)",
                  color: voice.phase === "recording" ? "#1b1b1b" : TEXT,
                }}
              >
                {voice.phase === "working" ? "识别中" : voice.phase === "recording" ? "松开" : "语音"}
              </Btn>
            </div>
            <div style={{ flex: "0 0 60px", width: 60, height: INPUT_H }}>
              {!s.busy ? (
                <Btn
                  primary
                  height={INPUT_H}
                  size={11}
                  disabled={!input.trim()}
                  onClick={submit}
                  description="发送"
                >
                  发送
                </Btn>
              ) : s.runningAction === "send" ? (
                <Btn running height={INPUT_H} size={11} onClick={() => {}} description="思考中">
                  思考中
                </Btn>
              ) : (
                <Btn danger height={INPUT_H} size={11} onClick={() => void s.cancel()} description="中断">
                  中断
                </Btn>
              )}
            </div>
          </div>

          {(qVoiceTip || qdlActive) && (
            <div style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", minHeight: 22, flexShrink: 0 }}>
              {qdlActive ? (
                <>
                  <span style={{ fontSize: 10, color: DIM, whiteSpace: "nowrap" }}>
                    {qdl?.state === "extracting" ? "解压" : "下载"} {qdlPercent}%
                  </span>
                  <div
                    style={{
                      flex: 1,
                      minWidth: 40,
                      height: 6,
                      borderRadius: 3,
                      background: "rgba(255,255,255,.10)",
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        width: `${qdlPercent}%`,
                        height: "100%",
                        borderRadius: 3,
                        background: ACCENT,
                        transition: "width .4s ease",
                      }}
                    />
                  </div>
                  <div style={{ flex: "0 0 44px", width: 44 }}>
                    <Btn height={22} size={10} onClick={voice.cancelDownload} description="取消下载">
                      取消
                    </Btn>
                  </div>
                </>
              ) : qVoiceTip ? (
                <>
                  <span style={{ fontSize: 10, color: qVoiceTip.color, flex: 1, minWidth: 0 }}>{qVoiceTip.text}</span>
                  {qVoiceTip.canDownload && (
                    <div style={{ flex: "0 0 56px", width: 56 }}>
                      <Btn
                        height={22}
                        size={10}
                        disabled={voice.downloading}
                        onClick={voice.downloadModel}
                        description="下载语音模型"
                      >
                        下载
                      </Btn>
                    </div>
                  )}
                </>
              ) : null}
            </div>
          )}

          <div style={dividerStyle} />

          <ChatLog
            items={s.items}
            streaming={s.streaming}
            busy={s.busy && s.runningAction === "send"}
            elapsed={s.elapsed}
            scrollRef={scrollRef}
          />
        </div>
      )}

      {/* ================= 设置页 ================= */}
      {tab === 2 && (
        <Focusable
          flow-children="vertical"
          onOKActionDescription="浏览"
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
            overflowY: "auto",
            overscrollBehavior: "contain",
            scrollPaddingBottom: 6,
            paddingBottom: 2,
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            boxSizing: "border-box",
            paddingRight: 2,
          }}
        >
          {/* 局域网访问：只展示地址；鉴权走设备配对 PIN，不再有令牌 */}
          <Card title="局域网访问">
            <div style={{ fontSize: 11, color: DIM, lineHeight: 1.5 }}>
              手机 / 平板 / 电脑浏览器打开这个地址，按掌机上的 PIN 配对一次即可。
            </div>
            <div style={{ fontSize: 12, color: TEXT, wordBreak: "break-all" }}>
              {s.status?.lan_url || "—"}
            </div>
            <Btn
              height={26}
              size={11}
              onClick={() => void copyText(s.status?.lan_url || "")}
              description="复制局域网地址"
            >
              复制地址
            </Btn>
            <div style={{ fontSize: 11, color: DIM }}>
              当前端口 {s.status?.port ?? "—"}（改后需重启后端）
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <TextField
                value={portDraft}
                onChange={(e) => setPortDraft(e.target.value)}
              />
              <Btn
                height={26}
                size={10.5}
                onClick={() => void savePortDraft()}
                description="保存端口"
              >
                保存端口
              </Btn>
              <Btn
                height={26}
                size={10.5}
                disabled={s.restarting}
                onClick={() => void s.coreRestart()}
                description="重启后端使端口生效"
              >
                {s.restarting ? "重启中…" : "重启后端"}
              </Btn>
            </div>
            {portHint ? (
              <div style={{ fontSize: 10.5, color: "#e0a33a", lineHeight: 1.4 }}>
                {portHint}
              </div>
            ) : null}
          </Card>

          {/* 设备配对：PIN 只在掌机上显示，浏览器输入 PIN 才能拿到设备凭据 */}
          <Card title="设备配对">
            {s.status?.security_note ? (
              <div style={{ fontSize: 11.5, color: "#e0a33a", lineHeight: 1.5 }}>
                {s.status.security_note}
              </div>
            ) : null}
            {!s.status?.lan_enable ? (
              <div style={{ fontSize: 11, color: DIM, lineHeight: 1.5 }}>
                局域网访问当前关闭（仅本机）。在网页设置里开启并重启后端后，新设备才能配对。
              </div>
            ) : null}
            {/* 结果提示占固定高度：出现/消失都不推挤下方 PIN 与按钮，窄屏不跳动 */}
            <div
              style={{
                height: 14,
                flexShrink: 0,
                display: "flex",
                alignItems: "center",
                minWidth: 0,
              }}
            >
              {s.pairFlash ? (
                <div
                  role="status"
                  style={{
                    width: "100%",
                    fontSize: 10.5,
                    lineHeight: "14px",
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    color: s.pairFlash.ok ? PAIR_OK : PAIR_WARN,
                  }}
                >
                  {s.pairFlash.ok ? "✓ " : "! "}
                  {s.pairFlash.text}
                </div>
              ) : null}
            </div>
            {s.pairSessions.length === 0 ? (
              <div style={{ fontSize: 11, color: DIM }}>
                暂无待配对设备。对方点「配对新设备」后会出现在这里。
              </div>
            ) : (
              s.pairSessions.map((p) => {
                const busy = s.pairBusy[p.pair_id];
                const local = s.pairDecisions[p.pair_id];
                // 本地点击结果优先：后端在浏览器确认前仍会返回 approved，
                // 靠轮询要等 2 秒，先把完成态画出来
                const approved = local === true || p.state === "approved";
                const denied = approved ? false : local === false || p.state === "denied";
                const decided = approved || denied;
                const stateText = busy
                  ? `正在${busy === "allow" ? "允许" : "拒绝"}…`
                  : approved
                    ? "已允许 · 等待对方输入 PIN 完成配对"
                    : denied
                      ? "已拒绝 · 本次配对已作废"
                      : `剩余 ${Math.max(0, p.expires_in)} 秒 · 已试 ${p.attempts}/3 次`;
                const stateColor = busy
                  ? ACCENT
                  : approved
                    ? PAIR_OK
                    : denied
                      ? PAIR_WARN
                      : DIM;
                return (
                  <div
                    key={p.pair_id}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      gap: 4,
                      padding: "8px 9px",
                      borderRadius: 4,
                      background: approved
                        ? "rgba(72,199,142,.10)"
                        : denied
                          ? "rgba(224,108,117,.08)"
                          : "rgba(255,255,255,.05)",
                      border: `1px solid ${
                        approved
                          ? "rgba(94,214,160,.40)"
                          : denied
                            ? "rgba(224,108,117,.35)"
                            : "rgba(90,169,230,.16)"
                      }`,
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                      <span
                        style={{
                          fontSize: 11,
                          color: TEXT,
                          minWidth: 0,
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
                        }}
                      >
                        {p.name || "未知设备"} · {p.ip || "未知来源"}
                      </span>
                      {approved || denied ? (
                        <span
                          style={{
                            flexShrink: 0,
                            fontSize: 10,
                            lineHeight: 1.2,
                            padding: "1px 5px",
                            borderRadius: 3,
                            whiteSpace: "nowrap",
                            color: approved ? PAIR_OK : PAIR_WARN,
                            border: `1px solid ${
                              approved ? "rgba(94,214,160,.45)" : "rgba(224,108,117,.40)"
                            }`,
                          }}
                        >
                          {approved ? "已允许" : "已拒绝"}
                        </span>
                      ) : null}
                    </div>
                    {denied ? (
                      <div style={{ fontSize: 11, lineHeight: "26px", color: DIM }}>PIN 已失效</div>
                    ) : (
                      <div
                        style={{
                          fontSize: 22,
                          letterSpacing: 6,
                          color: TITLE,
                          fontFamily: "monospace",
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                        }}
                      >
                        {p.pin}
                      </div>
                    )}
                    <div
                      style={{
                        fontSize: 10.5,
                        color: stateColor,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                    >
                      {stateText}
                    </div>
                    <div style={{ display: "flex", gap: 6 }}>
                      <Btn
                        height={26}
                        size={10.5}
                        disabled={!!busy || decided}
                        style={approved ? PAIR_ALLOW_DONE : undefined}
                        onClick={() => void s.pairDecide(p.pair_id, true)}
                      >
                        {approved ? "✓ 已允许" : busy === "allow" ? "允许中…" : "允许"}
                      </Btn>
                      <Btn
                        height={26}
                        size={10.5}
                        disabled={!!busy || decided}
                        style={denied ? PAIR_DENY_DONE : undefined}
                        onClick={() => void s.pairDecide(p.pair_id, false)}
                      >
                        {denied ? "✓ 已拒绝" : busy === "deny" ? "拒绝中…" : "拒绝"}
                      </Btn>
                      <Btn
                        height={26}
                        size={10.5}
                        disabled={!!busy}
                        onClick={() => void s.pairRefresh(p.pair_id)}
                      >
                        刷新 PIN
                      </Btn>
                    </div>
                  </div>
                );
              })
            )}
            {s.pairDevices.length === 0 ? (
              <div style={{ fontSize: 11, color: DIM }}>还没有已配对设备。</div>
            ) : (
              s.pairDevices.map((d) => (
                <div key={d.device_id} style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                  <div style={{ fontSize: 11, color: TEXT }}>
                    {d.name || "未命名设备"} · {d.ip || "—"}
                  </div>
                  <div style={{ fontSize: 10, color: DIM }}>
                    最后在线 {d.last_seen ? new Date(d.last_seen * 1000).toLocaleString() : "—"}
                  </div>
                  <TextField
                    value={renameDrafts[d.device_id] ?? d.name}
                    onChange={(e) =>
                      setRenameDrafts((prev) => ({ ...prev, [d.device_id]: e.target.value }))
                    }
                  />
                  <div style={{ display: "flex", gap: 6 }}>
                    <Btn
                      height={24}
                      size={10.5}
                      onClick={() =>
                        void s.pairRename(
                          d.device_id,
                          renameDrafts[d.device_id] ?? d.name
                        )
                      }
                    >
                      保存名称
                    </Btn>
                    <Btn height={24} size={10.5} onClick={() => void s.pairRemove(d.device_id)}>
                      移除
                    </Btn>
                  </div>
                </div>
              ))
            )}
            {s.pairDevices.length > 0 ? (
              <Btn height={24} size={10.5} onClick={() => void s.pairRemoveAll()}>
                移除全部设备
              </Btn>
            ) : null}
            {s.pairError ? (
              <div style={{ fontSize: 10.5, color: ERR_COLOR }}>{s.pairError}</div>
            ) : null}
          </Card>

          {/* 模型 */}
          <Card title="模型">
            <div style={{ width: "100%", display: "flex" }}>
              <DropdownItem
                childrenContainerWidth="max"
                layout="below"
                rgOptions={(s.modelList.length ? s.modelList : [s.model].filter(Boolean)).map(
                  (m) => ({ data: m, label: m })
                )}
                selectedOption={s.model}
                menuLabel="选择模型"
                strDefaultLabel={s.loadingModels ? "加载中…" : "选择模型"}
                disabled={!coreRunning}
                onChange={(o) => {
                  if (o.data && o.data !== s.model) void s.changeModel(String(o.data));
                }}
              />
            </div>
            <Btn
              height={26}
              size={11}
              disabled={s.loadingModels}
              onClick={() => void s.loadModels()}
              description="刷新模型列表"
            >
              {s.loadingModels ? "拉取中…" : "刷新模型列表"}
            </Btn>
            <ToggleField label="带画面发送" checked={s.withShot} onChange={s.toggleWithShot} />
            <ToggleField label="语音输入" checked={s.stt} onChange={s.toggleStt} />
            <ToggleField
              label="录音时静音外放"
              description={
                s.sttDuckAvailable
                  ? "按住说话时把游戏声音静音，松开自动恢复"
                  : "这台机器没有 wpctl/pactl，开关只保存不生效"
              }
              checked={s.sttDuck}
              onChange={s.toggleSttDuck}
            />
            <ToggleField label="语音播报回复" checked={s.tts} onChange={s.toggleTts} />
          </Card>

          {/* 提示词 */}
          <Card title="提示词">
            <div style={{ fontSize: 11, color: DIM }}>截屏询问</div>
            <Focusable onOKActionDescription="编辑" style={{ width: "100%", padding: 0 }}>
              <ImeTextArea
                value={s.askTplEdit}
                onValueChange={s.setAskTplEdit}
                nMinHeight={84}
                nMaxHeight={84}
                spellCheck={false}
                style={{ ...qamTplStyle }}
              />
            </Focusable>
            <div style={{ display: "flex", gap: 6, width: "100%" }}>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  onClick={() => void s.saveAskTpl()}
                  description="保存询问提示词"
                >
                  保存
                </Btn>
              </div>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  onClick={() => s.setAskTplEdit(s.askTplDefault)}
                  description="恢复默认询问提示词"
                >
                  恢复默认
                </Btn>
              </div>
            </div>

            <div style={{ fontSize: 11, color: DIM, marginTop: 2 }}>截屏翻译</div>
            <Focusable onOKActionDescription="编辑" style={{ width: "100%", padding: 0 }}>
              <ImeTextArea
                value={s.translateTplEdit}
                onValueChange={s.setTranslateTplEdit}
                nMinHeight={84}
                nMaxHeight={84}
                spellCheck={false}
                style={{ ...qamTplStyle }}
              />
            </Focusable>
            <div style={{ display: "flex", gap: 6, width: "100%" }}>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  onClick={() => void s.saveTranslateTpl()}
                  description="保存翻译提示词"
                >
                  保存
                </Btn>
              </div>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  onClick={() => s.setTranslateTplEdit(s.translateTplDefault)}
                  description="恢复默认翻译提示词"
                >
                  恢复默认
                </Btn>
              </div>
            </div>

            {/* 按游戏提示词预设：把当前生效提示词存到正在运行的游戏上 */}
            <div style={{ display: "flex", alignItems: "center", gap: 6, width: "100%", marginTop: 2 }}>
              <div
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 10.5,
                  color: DIM,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                按游戏预设：{s.status?.preset_applied
                  ? (s.status.preset_title || "已套用")
                  : (s.status?.game_name ? "暂无" : "未运行游戏")}
              </div>
              <div style={{ flex: "0 0 96px", width: 96 }}>
                <Btn
                  height={26}
                  size={11}
                  onClick={() => void s.savePreset()}
                  description="把当前生效提示词保存为当前游戏的预设"
                >
                  存为预设
                </Btn>
              </div>
            </div>
          </Card>

          {/* 连接 */}
          <Card title="连接">
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(2, minmax(0, 1fr))",
                columnGap: 10,
                rowGap: 6,
                width: "100%",
              }}
            >
              <StatCell label="女仆核心" value={coreLine} />
              <StatCell
                label="设备地址"
                value={s.status ? `${s.status.ip}:${s.status.port}` : "—"}
              />
              <StatCell
                label="当前游戏"
                value={
                  !s.status
                    ? "—"
                    : s.status.game_name
                      ? `${s.status.game_name}（${s.status.game_source || "?"}）`
                      : "未运行游戏"
                }
              />
              <StatCell
                label="屏幕截图"
                value={s.status?.capture_ok ? "可用" : s.status?.capture_reason || "不可用"}
              />
              <StatCell
                label="更新检测"
                value={
                  !s.status
                    ? "检测中"
                    : s.status.update_has
                      ? `有更新${s.status.update_latest ? `（v${s.status.update_latest}）` : ""}`
                      : "当前最新"
                }
              />
              <StatCell
                label="核心版本"
                value={!s.status ? "读取中" : s.status.version || "—"}
              />
              <StatCell
                label="语音输入"
                value={
                  !voice.status
                    ? "读取中"
                    : voice.ready
                      ? "可用"
                      : voice.status.enable === false
                        ? "已关闭"
                        : "不可用"
                }
              />
            </div>
            {!coreRunning && (
              <Btn
                height={26}
                size={11}
                primary
                disabled={s.starting}
                onClick={() => void s.startBackend()}
                description="启动后端"
              >
                {s.starting ? "正在启动后端…" : "启动后端"}
              </Btn>
            )}
          </Card>

          {/* 维护 */}
          <Card title="维护">
            <div style={{ display: "flex", flexDirection: "column", gap: 3, width: "100%" }}>
              {s.logs.length ? (
                s.logs.slice(0, 3).map((e, i) => (
                  <div
                    key={`${e.ts}-${i}`}
                    style={{
                      fontSize: 10.5,
                      lineHeight: 1.4,
                      color: i === 0 ? TEXT : DIM,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {e.code} · {fmtLogTime(e.ts)}
                    {e.detail ? ` · ${e.detail}` : ""}
                  </div>
                ))
              ) : (
                <div style={{ fontSize: 10.5, color: DIM }}>暂无事件</div>
              )}
            </div>

            {s.status?.version_match === false && (
              <div style={{ fontSize: 10.5, color: "#e0a33a", width: "100%", lineHeight: 1.4 }}>
                磁盘已是 v{s.status.disk_version || "?"}，运行中的是 v
                {s.status.running_version || s.status.version || "?"}，点「重启核心」后生效
              </div>
            )}

            <div style={{ display: "flex", gap: 6, width: "100%" }}>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  primary={!coreRunning}
                  disabled={coreRunning || s.starting || s.restarting || s.killing}
                  onClick={() => void s.coreStart()}
                  description="启动核心"
                >
                  {s.starting ? "核心启动中…" : "启动核心"}
                </Btn>
              </div>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  disabled={!coreRunning || s.starting || s.restarting || s.killing}
                  onClick={() => void s.coreRestart()}
                  description="重启核心"
                >
                  {s.restarting ? "核心重启中…" : "重启核心"}
                </Btn>
              </div>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  disabled={!coreRunning || s.starting || s.restarting || s.killing}
                  onClick={() => void s.coreKill()}
                  description="关闭核心"
                >
                  {s.killing ? "核心关闭中…" : "关闭核心"}
                </Btn>
              </div>
            </div>

            <div style={{ display: "flex", gap: 6, width: "100%" }}>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn
                  height={26}
                  size={11}
                  disabled={s.busy || s.newing}
                  onClick={() => void s.newSession()}
                  description="新会话"
                >
                  {s.newing ? "新建中…" : "新会话"}
                </Btn>
              </div>
              <div style={{ flex: "1 1 0", minWidth: 0 }}>
                <Btn height={26} size={11} onClick={s.openBigScreen} description="打开大屏">
                  打开大屏
                </Btn>
              </div>
            </div>

            <div style={{ fontSize: 10, color: DIM, lineHeight: 1.4 }}>
              核心 {s.status?.version || "—"} · 插件 {PLUGIN_VERSION}
            </div>
            <div style={{ fontSize: 10, color: DIM, lineHeight: 1.4 }}>
              {coreStopped
                ? "核心已停止：不会自动拉起，只有点「启动核心」才恢复。"
                : "关闭核心后保持停止；「重启核心」只在运行中可用。"}
            </div>
          </Card>
        </Focusable>
      )}
    </>
  );
}
