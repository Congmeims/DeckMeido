import { useEffect, useRef, useState } from "react";
import { addEventListener, call, removeEventListener } from "@decky/api";
import { applyPadOrder } from "./padOrder";

// 后端在不在、截图能不能用、代理走哪条路，由插件后端探测
export type Status = {
  install_dir: string;
  config_ok: boolean;
  port: number;
  token_ok: boolean;
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
  workspace: string;
  gamepad: boolean;
  tts: boolean;
  model: string;
  game_name: string;
  version: string;
  unlocked: boolean;
  ticket: string;
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
  | { kind: "status"; running: boolean }
  | { kind: "pad_order"; action: "to1p" | "revert" }
  | { kind: "thread"; items: Item[] };

export type PendingPoke = { text: string; brief: string };

export const SHOT_ASK_BRIEF = "看看这个，我该怎么做？";
export const SHOT_TRANSLATE_BRIEF = "翻译画面上的文字";

export const DEFAULT_ASK_TPL =
  "看我卡在哪一步，说说现在这个画面里我该做什么。" +
  "已经能从上图判断就不要搜索；最多搜 2 次，得到一个答案就停。\n" +
  "再总结成几条可以直接照做的步骤，200 字以内。\n" +
  "不要剧透：只讲眼前这一步怎么过，不要提前说后面的剧情、结局或其他关卡的解法。";

export const SHOT_TRANSLATE_TPL =
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
  const [tts, setTts] = useState(false);
  const [model, setModel] = useState("");
  const [modelList, setModelList] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [askTpl, setAskTpl] = useState("");
  const [askTplEdit, setAskTplEdit] = useState("");
  const [tokenInput, setTokenInput] = useState("");
  const [ticket, setTicket] = useState("");
  const [probing, setProbing] = useState(true);
  const [restarting, setRestarting] = useState(false);
  const [starting, setStarting] = useState(false);
  const [newing, setNewing] = useState(false);
  const [pendingPoke, setPendingPoke] = useState<PendingPoke | null>(null);

  const curRef = useRef("");
  const busyRef = useRef(false);

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
    setActivity("");
    if (cancelled) setError("这一轮已中断");
  };

  const loadAskTpl = async (tk: string) => {
    try {
      const r = (await call<[string], { ok: boolean; text?: string }>(
        "get_ask_template",
        tk
      )) as { ok: boolean; text?: string };
      if (r.ok && r.text) {
        setAskTpl(r.text);
        setAskTplEdit(r.text);
      }
    } catch {
      /* 取不到就用后端默认模板 */
    }
  };

  const refreshStatus = async () => {
    setProbing(true);
    try {
      const st = (await call<[], Status>("get_status")) as Status;
      setStatus(st);
      setTts(!!st.tts);
      if (st.model) setModel(st.model);
      setTicket(st.ticket || "");
      if (st.ticket) void loadAskTpl(st.ticket);
      return st;
    } catch (e: any) {
      setError(`插件后端未就绪：${String(e?.message ?? e)}`);
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
      setError(r.error ?? "解锁失败");
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
      setError(String(e?.message ?? e));
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
        setError(r.error ?? "拉起后端失败");
        return;
      }
      await refreshStatus();
      await call<[string], { ok: boolean; error?: string }>("session_open", ticket);
      await call<[string], { ok: boolean }>("ready", ticket);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setStarting(false);
    }
  };

  const runTurn = async (text: string, grab: boolean, brief = "") => {
    if (busyRef.current) return;
    setError("");
    setActivity("");
    setPendingPoke(null);
    curRef.current = "";
    setStreaming("");
    setBusyBoth(true);

    try {
      const st = await refreshStatus();
      if (!st) throw new Error("插件后端未就绪");
      if (!st.config_ok) throw new Error(`找不到配置：${st.install_dir}/server/config.toml`);
      if (!st.token_ok) throw new Error("后端配置里的 token 是空的，先在网页设置页生成一个");
      if (!st.unlocked || !st.ticket) throw new Error("插件未解锁，先输入访问令牌");
      const tk = st.ticket;

      let shotPath: string | null = null;
      let game: { appid?: string; name?: string } | null = null;
      if (grab) {
        // TODO: /deckmeido 是全屏路由，当前位置抓帧会把这一页也拍进去。
        // 第一版先按原链路调用；后续需要在抓帧前 NavigateBack 回游戏、等 1.2s，
        // 抓完再 Navigate 回来，否则截屏询问拿到的会是插件页而不是游戏画面。
        const snap = (await call<
          [string, boolean],
          {
            ok: boolean;
            shot?: string;
            thumb?: string;
            error?: string;
            needPoke?: boolean;
            game?: { appid?: string; name?: string };
          }
        >("screenshot", tk, false).catch((e: any) => ({
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
          setPendingPoke({ text, brief });
          setBusyBoth(false);
          return;
        }
        if (snap.ok && snap.shot) {
          shotPath = snap.shot;
        } else {
          setError(`截图失败，这轮只发文字：${snap.error ?? "未知原因"}`);
        }
        game = snap.game ?? null;
      }

      let body = text;
      const gname = game?.name ?? "";
      if (gname) {
        const aid = game?.appid ? `（appid ${game.appid}）` : "";
        body = `当前游戏：《${gname}》${aid}。\n${text}`;
      }

      const res = (await call<[string, string | null, string, string], { ok: boolean; error?: string }>(
        "start_turn",
        body,
        shotPath,
        brief,
        tk
      )) as { ok: boolean; error?: string };
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
      setError(String(e?.message ?? e));
      setBusyBoth(false);
      setActivity("");
      setStreaming("");
    }
  };

  const send = async (text: string) => {
    const t = text.trim();
    if (!t || busyRef.current) return;
    await runTurn(t, withShot, "");
  };

  const shotAsk = async () => {
    if (busyRef.current) return;
    await runTurn(askTpl || DEFAULT_ASK_TPL, true, SHOT_ASK_BRIEF);
  };

  const shotTranslate = async () => {
    if (busyRef.current) return;
    await runTurn(SHOT_TRANSLATE_TPL, true, SHOT_TRANSLATE_BRIEF);
  };

  const retryPoke = async () => {
    if (busyRef.current || !pendingPoke) return;
    const p = pendingPoke;
    setPendingPoke(null);
    await runTurn(p.text, true, p.brief);
  };

  const cancel = async () => {
    setBusyBoth(false);
    setActivity("");
    try {
      await call<[string], { ok: boolean; error?: string }>("cancel", ticket);
    } catch {
      /* 断开本身已经够用 */
    }
    setError("这一轮已中断");
    setPendingPoke(null);
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
    } catch (e: any) {
      setError(`新建会话失败：${String(e?.message ?? e)}`);
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
      setError(`切换模型失败：${String(e?.message ?? e)}`);
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
      setError(`语音播报切换失败：${String(e?.message ?? e)}`);
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
      setError(`提示词保存失败：${String(e?.message ?? e)}`);
    }
  };

  const restartCore = async () => {
    if (restarting) return;
    setRestarting(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; version?: string; error?: string }>(
        "restart_core",
        ticket
      )) as { ok: boolean; version?: string; error?: string };
      if (!r.ok) throw new Error(r.error ?? "核心没有回来");
      await refreshStatus();
    } catch (e: any) {
      setError(`重启核心失败：${String(e?.message ?? e)}`);
    } finally {
      setRestarting(false);
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
        void applyPadOrder(msg.action);
        return;
      }
      if (msg.kind === "status") {
        if (msg.running && !busyRef.current) {
          // 可能是别的端已经发起了一轮；大屏页打开后要接着显示这轮的状态
          setBusyBoth(true);
        } else if (!msg.running && busyRef.current) {
          setBusyBoth(false);
          setError("这一轮没有收到答复，可能是后端断开了");
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
    elapsed,
    activity,
    withShot,
    setWithShot,
    tts,
    model,
    modelList,
    loadingModels,
    askTpl,
    askTplEdit,
    setAskTplEdit,
    tokenInput,
    setTokenInput,
    ticket,
    probing,
    restarting,
    starting,
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
    toggleTts,
    saveAskTpl,
    restartCore,
    setError,
  };
}
