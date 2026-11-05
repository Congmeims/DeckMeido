/**
 * 大屏语音输入：按住说话，松开识别，结果填进输入框（不自动发送）
 *
 * 为什么在插件里录音而不是后端：麦克风在桌面会话里，Decky 插件跑在游戏模式，
 * 能直接拿到 getUserMedia；录成 WAV 再交给插件后端转写，后端只做识别。
 *
 * 录音格式固定 16k 单声道 16bit —— SenseVoice 的输入要求，转写接口也只收这个，
 * 格式在这里一次调好，避免后端再重采样。
 *
 * 太短的录音（< MIN_MS）直接丢弃：误触一下会录到半秒静音，识别出来是空串，
 * 白白加载一次模型。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { call } from "@decky/api";

const SAMPLE_RATE = 16000;
const MIN_MS = 400;
const MAX_MS = 60000;
// 转写请求自己兜个底：后端卡死/桥接丢消息时也不能让 phase 永远停在 working，
// 否则语音入口会一直灰着，用户只能重启页面
const CALL_TIMEOUT_MS = 120000;

type Phase = "idle" | "recording" | "working";

type Download = {
  state: "idle" | "downloading" | "extracting" | "done" | "error" | "cancelled";
  running?: boolean;
  cancellable?: boolean;
  done?: number;
  total?: number;
  percent?: number;
  phase?: string;
  error?: string;
  error_kind?: string;
};

type Status = {
  ok: boolean;
  error?: string;
  enable?: boolean;
  runtime?: boolean;
  model?: boolean;
  record?: boolean;
  record_reason?: string;
  attribution?: string;
  download?: Download;
};

/** 下载失败按类别给一句人话；后端只回原始错误，措辞在前端统一 */
function downloadErrorText(d?: Download | null): string {
  if (!d || d.state !== "error") return "";
  const kind = d.error_kind || "general";
  const map: Record<string, string> = {
    network: "下载失败：网络断了或 GitHub 访问不了，检查网络后重试",
    disk: "下载失败：磁盘空间不够，至少需要 1.5GB 空闲",
    permission: "下载失败：数据目录不可写，检查 userdata/ 权限",
    extract: "下载失败：安装包解压出错，重新下载即可",
    general: "下载失败，稍后重试",
  };
  return `${map[kind] || map.general}${d.error ? `（${d.error}）` : ""}`;
}

/** Float32 [-1,1] → 16bit PCM 的 WAV 容器 */
function encodeWav(samples: Float32Array, rate: number): Uint8Array {
  const n = samples.length;
  const buf = new ArrayBuffer(44 + n * 2);
  const v = new DataView(buf);
  const str = (off: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  str(0, "RIFF");
  v.setUint32(4, 36 + n * 2, true);
  str(8, "WAVE");
  str(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // 单声道
  v.setUint32(24, rate, true);
  v.setUint32(28, rate * 2, true);
  v.setUint16(32, 2, true);
  v.setUint16(34, 16, true);
  str(36, "data");
  v.setUint32(40, n * 2, true);
  let off = 44;
  for (let i = 0; i < n; i++) {
    // 夹紧到 [-1,1] 再缩放，越界会绕回成爆音
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(off, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    off += 2;
  }
  return new Uint8Array(buf);
}

function toBase64(bytes: Uint8Array): string {
  let bin = "";
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CH));
  }
  return btoa(bin);
}

function withTimeout<T>(p: Promise<T>, ms: number, msg: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const id = window.setTimeout(() => reject(new Error(msg)), ms);
    p.then(
      (v) => {
        window.clearTimeout(id);
        resolve(v);
      },
      (e) => {
        window.clearTimeout(id);
        reject(e);
      }
    );
  });
}

export function useVoiceInput(ticket: string, onText: (t: string) => void) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  // 下载轮询：取消时置 true，让循环在下一拍退出（不依赖 status 的异步更新）
  const downloadCancelRef = useRef(false);
  const downloadBusyRef = useRef(false);
  // 最近一次状态快照：refresh 并发时直接复用它，避免轮询读到旧闭包
  const statusRef = useRef<Status | null>(null);

  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const nodeRef = useRef<ScriptProcessorNode | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const startedRef = useRef(0);
  const phaseRef = useRef<Phase>("idle");
  // 每次按下/松开都 +1：松开时立刻作废还没跑完的 getUserMedia，
  // 避免「已经松开了，麦克风才打开」把状态卡在 recording
  const seqRef = useRef(0);
  // getUserMedia 还在等授权/开设备
  const openingRef = useRef(false);
  // 最长 60 秒兜底定时器：只该管这一次录音，必须能清掉
  const timerRef = useRef<number | null>(null);
  const refreshingRef = useRef(false);
  phaseRef.current = phase;

  /** state 提交是异步的，按键事件读到的可能是上一帧的值；ref 同步改，保证松开能命中 */
  const applyPhase = useCallback((p: Phase) => {
    phaseRef.current = p;
    setPhase(p);
  }, []);

  const refresh = useCallback(async (): Promise<Status | null> => {
    if (!ticket) return statusRef.current;
    if (refreshingRef.current) return statusRef.current;
    refreshingRef.current = true;
    try {
      const r = (await call<[string], Status>("stt_status", ticket)) as Status;
      statusRef.current = r;
      setStatus(r);
      return r;
    } catch (e: any) {
      const bad = { ok: false, error: String(e?.message ?? e) } as Status;
      statusRef.current = bad;
      setStatus(bad);
      return bad;
    } finally {
      refreshingRef.current = false;
    }
  }, [ticket]);

  useEffect(() => {
    if (!ticket) return;
    void refresh();
    // ticket 可能比组件挂载晚到（要先 get_status 才拿到），空票直接跳过。
    // 注意依赖里只能有 refresh：status 每次都换新对象，写进依赖会自己触发自己，
    // 变成每秒几十次 stt_status 的死循环，把桥接和主线程都拖死
    const id = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(id);
  }, [refresh]);

  const cleanup = useCallback(() => {
    const node = nodeRef.current;
    nodeRef.current = null;
    try {
      node?.disconnect();
    } catch {
      /* 已经断开 */
    }
    const stream = streamRef.current;
    streamRef.current = null;
    try {
      stream?.getTracks().forEach((t) => {
        try {
          t.stop();
        } catch {
          /* 轨道已经结束 */
        }
      });
    } catch {
      /* 流已经失效 */
    }
    const ctx = ctxRef.current;
    ctxRef.current = null;
    try {
      void ctx?.close().catch(() => undefined);
    } catch {
      /* 已经关闭 */
    }
  }, []);

  // 组件卸载时一定释放麦克风，否则录制指示灯会一直亮
  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(async () => {
    if (phaseRef.current !== "recording") return;
    // 作废挂起的 start（用户在下一次录音里已经重新按下了）
    seqRef.current += 1;
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    const ms = Date.now() - startedRef.current;
    const opening = openingRef.current || !nodeRef.current;
    cleanup();
    openingRef.current = false;
    const parts = chunksRef.current;
    chunksRef.current = [];
    // 先把状态和麦克风放掉：后面不管识别成功、失败还是超时，都不会卡在 recording
    applyPhase("idle");

    // 还没真正开始录就松开了（授权弹窗/开设备慢）：静默收尾，下一次直接能按
    if (opening) return;
    if (ms < MIN_MS) {
      setError("说话时间太短");
      return;
    }
    // 拼接各段
    const total = parts.reduce((a, p) => a + p.length, 0);
    if (!total) {
      setError("没有录到声音");
      return;
    }
    const merged = new Float32Array(total);
    let off = 0;
    for (const p of parts) {
      merged.set(p, off);
      off += p.length;
    }

    applyPhase("working");
    setError("");
    try {
      const wav = encodeWav(merged, SAMPLE_RATE);
      const r = (await withTimeout(
        call<[string, string], { ok: boolean; text?: string; error?: string }>(
          "stt_transcribe",
          toBase64(wav),
          ticket
        ) as Promise<{ ok: boolean; text?: string; error?: string }>,
        CALL_TIMEOUT_MS,
        "识别超时，请重试"
      )) as { ok: boolean; text?: string; error?: string };
      if (!r.ok) {
        setError(r.error || "识别失败");
        return;
      }
      const text = (r.text || "").trim();
      if (!text) {
        setError("没听清，再说一次");
        return;
      }
      onText(text);
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      applyPhase("idle");
    }
  }, [applyPhase, cleanup, onText, ticket]);

  const start = useCallback(async () => {
    if (phaseRef.current !== "idle") return;
    setError("");
    // 先查可用性，缺依赖/模型时直接提示，不要去开麦克风
    if (status && status.ok) {
      if (status.enable === false) {
        setError("语音输入已关闭");
        return;
      }
      if (!status.runtime) {
        setError("后端没装 sherpa-onnx");
        return;
      }
      if (!status.model) {
        setError("语音模型还没下载，先点下载");
        return;
      }
      if (status.record === false) {
        setError(status.record_reason || "录音设备不可用");
        return;
      }
    }
    const gen = seqRef.current + 1;
    seqRef.current = gen;
    chunksRef.current = [];
    openingRef.current = true;
    // 关键：先同步切到 recording。原实现要等 getUserMedia/AudioContext 都建好
    // 才 setPhase，这段时间里松开按键会被 onButtonUp 忽略，麦克风一直开着，
    // 下一次按下去就被「phase !== idle」挡住 —— 表现就是只能问一句
    applyPhase("recording");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      if (seqRef.current !== gen) {
        // 等待期间已经松开/取消：把刚拿到的轨道放掉，不要再开录音
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      // 先挂到 ref 上：后面任何一步失败/被取消，cleanup 都能把麦克风放掉
      streamRef.current = stream;
      // 采样率不一定是 16k（浏览器有自己的最小值），这里统一转成 16k
      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      ctxRef.current = ctx;
      if (ctx.state === "suspended") {
        // 手柄按键不算 user activation，不 resume 的话 ScriptProcessor 不跑，
        // 第二、第三次录音就会变成一片静音
        try {
          await ctx.resume();
        } catch {
          /* 不能 resume 就交给下面的空音频提示 */
        }
      }
      if (seqRef.current !== gen) return;
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(4096, 1, 1);
      node.onaudioprocess = (ev) => {
        // 复制一份：缓冲区会被复用，直接 push 会拿到全 0
        chunksRef.current.push(new Float32Array(ev.inputBuffer.getChannelData(0)));
      };
      src.connect(node);
      // ScriptProcessor 必须接到 destination 才会跑，但接上去会把麦克风回放出去，
      // 用一个增益 0 的节点挡在中间
      const mute = ctx.createGain();
      mute.gain.value = 0;
      node.connect(mute);
      mute.connect(ctx.destination);

      nodeRef.current = node;
      openingRef.current = false;
      startedRef.current = Date.now();

      // 兜底：最长录 60 秒，防止忘了松开。每次录音只留一个，松开时清掉
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => {
        timerRef.current = null;
        if (phaseRef.current === "recording") void stop();
      }, MAX_MS);
    } catch (e: any) {
      if (seqRef.current !== gen) return;
      openingRef.current = false;
      cleanup();
      applyPhase("idle");
      setError("打不开麦克风：" + String(e?.message ?? e));
    }
  }, [applyPhase, cleanup, status, stop]);

  const downloadModel = useCallback(async () => {
    if (downloadBusyRef.current) return;
    downloadBusyRef.current = true;
    downloadCancelRef.current = false;
    setDownloading(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; error?: string }>(
        "stt_download_model",
        ticket
      )) as { ok: boolean; error?: string };
      if (!r.ok) {
        setError(r.error || "模型下载启动失败");
        return;
      }
      // 后端是后台任务：这里 1 秒一拍拉状态，直到 done / error / cancelled。
      // 最多盯 30 分钟，超时就放手（后台仍会继续下，下次进来能接着看到状态）
      const deadline = Date.now() + 30 * 60 * 1000;
      while (!downloadCancelRef.current && Date.now() < deadline) {
        await new Promise((res) => setTimeout(res, 1000));
        const st = await refresh();
        const d = st?.download;
        if (!d) continue;
        if (d.state === "done" || d.state === "error" || d.state === "cancelled") break;
      }
      // 结束时再拉一次，确保 model 字段是最终值：下载完不用重启就能直接说话
      await refresh();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      downloadBusyRef.current = false;
      setDownloading(false);
    }
  }, [refresh, ticket]);

  const cancelDownload = useCallback(async () => {
    downloadCancelRef.current = true;
    try {
      await call<[string], { ok: boolean; error?: string }>("stt_download_cancel", ticket);
    } catch {
      /* 取消失败就让后台自然结束，界面不额外报错 */
    } finally {
      await refresh();
    }
  }, [refresh, ticket]);

  const dl = status?.download;
  return {
    phase,
    status,
    error,
    /** 下载进度（后端后台任务的状态快照），未下载过时为 null */
    download: dl ?? null,
    /** 下载失败的人话提示（按错误类别区分网络/磁盘/权限/解压） */
    downloadError: downloadErrorText(dl),
    downloading: downloading || !!dl?.running,
    cancelDownload,
    /** 能不能按：状态查到了、且各项都齐 */
    ready: !!status?.ok && status.enable !== false && status.runtime === true && status.model === true,
    start,
    stop,
    refresh,
    downloadModel,
    clearError: () => setError(""),
  };
}
