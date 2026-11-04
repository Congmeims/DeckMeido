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
import { ACCENT, DIM, ERR_COLOR, ImeTextArea, Md, TEXT } from "./ui";
import { statusLineFor } from "./statusLine";
import { applyPadOrder, logPadOrder, steamClient } from "./padOrder";
import { useVoiceInput } from "./useVoiceInput";
import {
  BIG_SCREEN_ROUTE,
  DEFAULT_ASK_TPL,
  DEFAULT_TRANSLATE_TPL,
  Delta,
  Item,
  LogEntry,
  SHOT_ASK_BRIEF,
  SHOT_TRANSLATE_BRIEF,
  Status,
} from "./useSession";

declare const __PLUGIN_VERSION__: string;
const PLUGIN_VERSION =
  typeof __PLUGIN_VERSION__ === "string" ? __PLUGIN_VERSION__ : "—";

// QAM 面板只有 300~400px 宽，按窄屏重排：整页固定高度，消息区吃满剩余空间，
// 减少一长串纵向滚动。设置页内部允许滚动，但一屏最多露出三张卡。
const PAGE_H = 460;
const TAB_H = 26;
const ACTION_H = 34;
const INPUT_H = 32;
const GAP = 8;

const TABS = ["询问", "聊天", "设置"] as const;

const TITLE = "#eaf2fb";

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
  const [tokenInput, setTokenInput] = useState("");
  const [ticket, setTicket] = useState("");
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

  const loadTemplates = async (tk: string) => {
    try {
      const r = (await call<[string], { ok: boolean; text?: string; default?: string }>(
        "get_ask_template",
        tk
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
      const r = (await call<[string], { ok: boolean; text?: string; default?: string }>(
        "get_translate_template",
        tk
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

  const refreshStatus = async () => {
    setProbing(true);
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      setTts(!!st.tts);
      setWithShot(!!st.with_shot);
      setStt(!!st.stt);
      if (st.model) setModel(st.model);
      setTicket(st.ticket || "");
      if (st.ticket) void loadTemplates(st.ticket);
      return st;
    } catch (e: any) {
      fail("BACKEND_DOWN", `插件后端未就绪：${String(e?.message ?? e)}`);
      return null;
    } finally {
      setProbing(false);
    }
  };

  const applyTicket = async (tk: string) => {
    const r = (await call<[string], { ok: boolean; ticket?: string; error?: string }>(
      "unlock",
      tk
    )) as { ok: boolean; ticket?: string; error?: string };
    if (!r.ok || !r.ticket) {
      fail("AUTH_FAILED", r.error ?? "解锁失败");
      return;
    }
    setTicket(r.ticket);
    setTokenInput("");
    setError("");
    await refreshStatus();
  };

  const unlock = async () => {
    const tk = tokenInput.trim();
    if (!tk) return;
    try {
      await applyTicket(tk);
    } catch (e: any) {
      fail("AUTH_FAILED", String(e?.message ?? e));
    }
  };

  const startBackend = async () => {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; error?: string; already?: boolean }>(
        "start_backend",
        ticket
      )) as { ok: boolean; error?: string; already?: boolean };
      if (!r.ok) {
        fail("CORE_START_FAILED", r.error ?? "拉起后端失败");
        return;
      }
      await refreshStatus();
      await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
      await call<[string], { ok: boolean }>("ready", ticket);
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

  const restoreQamSafe = async (tk: string) => {
    restoreQam();
    if (!tk) return;
    try {
      await applyTicket(tk);
    } catch (e: any) {
      setError(`${String(e?.message ?? e)}（面板已打开，需要重新输入访问令牌）`);
    }
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
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");
      if (!st.unlocked || !st.ticket) throw new Error("插件未解锁，先输入访问令牌");
      const tk = st.ticket;

      let shotPath: string | null = null;
      let game: {
        appid?: string;
        name?: string;
        source?: string;
        confidence?: string;
        ts?: number;
      } | null = null;
      if (grab) {
        // QAM 收起后 Steam 还要重绘一帧游戏画面，抓早了会把正在消失的侧栏拍进去
        if (hideQam) Navigation.CloseSideMenus();
        if (hideQam) await wait(1200);
        const snap = (await call<
          [string, boolean],
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
        >("screenshot", tk, hideQam).catch((e: any) => ({
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
        if (hideQam) await restoreQamSafe(tk);
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
        [string, string | null, string, string, string],
        { ok: boolean; error?: string }
      >("start_turn", body, shotPath, brief, tk, surface)) as {
        ok: boolean;
        error?: string;
      };
      if (!res.ok) throw new Error(res.error ?? "发不出去");

      const fin = (await call<[number, string], { ok: boolean; text?: string; error?: string }>(
        "wait_final",
        900,
        tk
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
      if (grab && hideQam) void restoreQamSafe("");
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
      await call<[string], { ok: boolean; error?: string }>("cancel", ticket);
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
      const r = (await call<[string], { ok: boolean; error?: string; unchanged?: boolean }>(
        "session_new",
        ticket
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
      const r = (await call<[string], { ok: boolean; model?: string; error?: string }>(
        "get_models",
        ticket
      )) as { ok: boolean; model?: string; error?: string };
      if (r.ok && r.model) setModel(r.model);
    } catch {
      /* 当前模型拿不到就保持原样 */
    }
    try {
      const r = (await call<[string], { ok: boolean; models?: string[]; error?: string }>(
        "refresh_models",
        ticket
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
      const r = (await call<[string, string], { ok: boolean; model?: string; error?: string }>(
        "set_model",
        name,
        ticket
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
      const r = (await call<[boolean, string], { ok: boolean; stt?: boolean; error?: string }>(
        "set_stt",
        v,
        ticket
      )) as { ok: boolean; stt?: boolean; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setStt(!!r.stt);
    } catch (e: any) {
      setStt(!v);
      fail("STT_TOGGLE_FAILED", `语音输入切换失败：${String(e?.message ?? e)}`);
    }
  };

  const toggleTts = async (v: boolean) => {
    setTts(v);
    try {
      const r = (await call<[boolean, string], { ok: boolean; tts?: boolean; error?: string }>(
        "set_tts",
        v,
        ticket
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
      const r = (await call<[boolean, string], { ok: boolean; with_shot?: boolean; error?: string }>(
        "set_with_shot",
        v,
        ticket
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
      const r = (await call<[string, string], { ok: boolean; text?: string; error?: string }>(
        "set_ask_template",
        askTplEdit,
        ticket
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
      const r = (await call<[string, string], { ok: boolean; text?: string; error?: string }>(
        "set_translate_template",
        translateTplEdit,
        ticket
      )) as { ok: boolean; text?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "后端没有接受这次修改");
      setTranslateTpl(r.text ?? "");
      setTranslateTplEdit(r.text ?? "");
      setError("");
    } catch (e: any) {
      fail("TRANSLATE_TPL_SAVE_FAILED", `翻译提示词保存失败：${String(e?.message ?? e)}`);
    }
  };

  const coreStart = async () => {
    if (starting) return;
    setStarting(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; version?: string; error?: string }>(
        "core_start",
        ticket
      )) as { ok: boolean; version?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "核心没有起来");
      await refreshStatus();
      await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
      await call<[string], { ok: boolean }>("ready", ticket);
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
      const r = (await call<[string], { ok: boolean; version?: string; error?: string }>(
        "core_restart",
        ticket
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
      const r = (await call<[string], { ok: boolean; error?: string }>(
        "core_kill",
        ticket
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
    if (!ticket) return;
    (async () => {
      try {
        await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
        await call<[string], { ok: boolean }>("ready", ticket);
      } catch {
        /* 连不上会在 get_status 里体现 */
      }
    })();
  }, [ticket]);

  useEffect(() => {
    const onDelta = (msg: Delta) => {
      if (msg.kind === "thread") {
        setItems(msg.items);
        return;
      }
      if (msg.kind === "pad_order") {
        logPadOrder(`QAM 收到 pad_order/${msg.action}`);
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
    tokenInput,
    setTokenInput,
    ticket,
    probing,
    restarting,
    starting,
    killing,
    newing,
    pendingPoke,
    refreshStatus,
    unlock,
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
    coreStart,
    coreRestart,
    coreKill,
    openBigScreen,
  };
}

export default function QamScreen() {
  const s = useQamSession();
  const [tab, setTabState] = useState(lastTab);
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
  const voice = useVoiceInput(s.ticket, (t) =>
    setInput((prev) => (prev.trim() ? prev.replace(/\s*$/, " ") + t : t))
  );

  // 窄屏按时间顺序排列，最新一条贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.items, s.streaming, tab]);

  // 进设置页拉一次模型列表，和之前的 QAM 行为一致
  useEffect(() => {
    if (tab === 2 && s.ticket) void s.loadModels();
  }, [tab, s.ticket]);

  // LB / RB 翻页：沿用 Steam 前端的控制器输入回调（LB=6、RB=7，不是 Linux 键码）
  useEffect(() => {
    let reg: any = null;
    try {
      const api = steamClient()?.Input;
      if (api && typeof api.RegisterForControllerInputMessages === "function") {
        reg = api.RegisterForControllerInputMessages((msg: any) => {
          if (!msg || msg.is_repeat) return;
          if (msg.button === 6) setTab((p) => Math.max(0, p - 1));
          else if (msg.button === 7) setTab((p) => Math.min(TABS.length - 1, p + 1));
        });
      }
    } catch (e) {
      logPadOrder(`LB/RB 绑定失败，翻页改用标签点按：${String(e)}`);
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

  const ready = !!s.status?.unlocked;
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

  const latestLog = s.logs[0];

  // 询问页忙碌态：被点的那格原地显示「思考中…」，另一格换成「中断」
  const askRunning = s.busy && s.runningAction !== "translate";
  const translateRunning = s.busy && s.runningAction === "translate";

  const shell = (children: React.ReactNode) => (
    <PanelSection>
      <PanelSectionRow>
        <div
          style={{
            width: "100%",
            height: PAGE_H,
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            minWidth: 0,
            boxSizing: "border-box",
          }}
        >
          {children}
        </div>
      </PanelSectionRow>
    </PanelSection>
  );

  const tabRow = (
    <Focusable
      style={{ display: "flex", gap: 6, width: "100%", height: TAB_H, flexShrink: 0 }}
      onCancelActionDescription="返回"
    >
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

  // 未解锁：只留解锁卡片
  if (!ready) {
    return shell(
      <>
        <Card title="解锁">
          <div style={{ fontSize: 11, color: DIM, lineHeight: 1.5 }}>
            插件以 root 运行，输入访问令牌后才能使用（默认 meido）。
          </div>
          <Focusable onOKActionDescription="输入" style={{ width: "100%", padding: 0 }}>
            <TextField
              value={s.tokenInput}
              placeholder="访问令牌"
              onChange={(e) => s.setTokenInput(e.target.value)}
              style={{ width: "100%" }}
            />
          </Focusable>
          <Btn primary onClick={() => void s.unlock()} description="解锁">
            解锁
          </Btn>
          {s.error && (
            <div style={{ fontSize: 11, color: ERR_COLOR, lineHeight: 1.4 }}>{s.error}</div>
          )}
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
          {/* 状态行只放结论：核心状态、地址、最近事件码（明细留在设置页） */}
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
            <span style={{ color: coreColor, flexShrink: 0 }}>
              {coreRunning ? statusLineFor(s.status, s.busy) : coreLine}
            </span>
            <span style={{ color: DIM, flexShrink: 0 }}>
              {s.status ? `${s.status.ip}:${s.status.port}` : "—"}
            </span>
            {s.busy && s.activity ? (
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  color: DIM,
                  textAlign: "right",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {s.activity}
              </span>
            ) : latestLog ? (
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  color: DIM,
                  textAlign: "right",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {latestLog.code} · {fmtLogTime(latestLog.ts)}
              </span>
            ) : null}
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
                <Btn primary onClick={() => void s.shotAsk()} description="截图询问">
                  截图询问
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
                <Btn onClick={() => void s.shotTranslate()} description="截图翻译">
                  截图翻译
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
            display: "flex",
            flexDirection: "column",
            gap: GAP,
            boxSizing: "border-box",
            paddingRight: 2,
          }}
        >
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

            <div style={{ fontSize: 11, color: DIM, marginTop: 2 }}>截图翻译</div>
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
                  s.status?.game_name
                    ? `${s.status.game_name}（${s.status.game_source || "?"}）`
                    : "—"
                }
              />
              <StatCell
                label="屏幕截图"
                value={s.status?.capture_ok ? "可用" : s.status?.capture_reason || "不可用"}
              />
              <StatCell label="核心版本" value={s.status?.version || "—"} />
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
