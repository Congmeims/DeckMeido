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

type Phase = "idle" | "recording" | "working";

type Status = {
  ok: boolean;
  error?: string;
  enable?: boolean;
  runtime?: boolean;
  model?: boolean;
  record?: boolean;
  record_reason?: string;
  attribution?: string;
};

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

export function useVoiceInput(ticket: string, onText: (t: string) => void) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);

  const ctxRef = useRef<AudioContext | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const nodeRef = useRef<ScriptProcessorNode | null>(null);
  const chunksRef = useRef<Float32Array[]>([]);
  const startedRef = useRef(0);
  const phaseRef = useRef<Phase>("idle");
  phaseRef.current = phase;

  const refresh = useCallback(async () => {
    if (!ticket) return;
    try {
      const r = (await call<[string], Status>("stt_status", ticket)) as Status;
      setStatus(r);
    } catch (e: any) {
      setStatus({ ok: false, error: String(e?.message ?? e) });
    }
  }, [ticket]);

  useEffect(() => {
    refresh();
    // ticket 可能比组件挂载晚到（要先 get_status 才拿到），空票直接跳过；
    // 这里再兜一段：后端重启、状态一时取不到时不至于一直灰着
    if (status) return;
    const id = window.setInterval(refresh, 3000);
    return () => window.clearInterval(id);
  }, [refresh, status]);

  const cleanup = useCallback(() => {
    try {
      nodeRef.current?.disconnect();
    } catch {
      /* 已经断开 */
    }
    nodeRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    ctxRef.current?.close().catch(() => undefined);
    ctxRef.current = null;
  }, []);

  // 组件卸载时一定释放麦克风，否则录制指示灯会一直亮
  useEffect(() => cleanup, [cleanup]);

  const stop = useCallback(async () => {
    if (phaseRef.current !== "recording") return;
    const ms = Date.now() - startedRef.current;
    cleanup();
    const parts = chunksRef.current;
    chunksRef.current = [];
    setPhase("idle");

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

    setPhase("working");
    setError("");
    try {
      const wav = encodeWav(merged, SAMPLE_RATE);
      const r = (await call<[string, string], { ok: boolean; text?: string; error?: string }>(
        "stt_transcribe",
        toBase64(wav),
        ticket
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
      setPhase("idle");
    }
  }, [cleanup, onText, ticket]);

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
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: SAMPLE_RATE,
          echoCancellation: true,
          noiseSuppression: true,
        },
      });
      // 采样率不一定是 16k（浏览器有自己的最小值），这里统一转成 16k
      const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      const src = ctx.createMediaStreamSource(stream);
      const node = ctx.createScriptProcessor(4096, 1, 1);
      chunksRef.current = [];
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

      ctxRef.current = ctx;
      streamRef.current = stream;
      nodeRef.current = node;
      startedRef.current = Date.now();
      setPhase("recording");

      // 兜底：最长录 60 秒，防止忘了松开
      window.setTimeout(() => {
        if (phaseRef.current === "recording") stop();
      }, MAX_MS);
    } catch (e: any) {
      cleanup();
      setError("打不开麦克风：" + String(e?.message ?? e));
    }
  }, [cleanup, status, stop]);

  const downloadModel = useCallback(async () => {
    if (downloading) return;
    setDownloading(true);
    setError("");
    try {
      const r = (await call<[string], { ok: boolean; error?: string }>(
        "stt_download_model",
        ticket
      )) as { ok: boolean; error?: string };
      if (!r.ok) setError(r.error || "模型下载失败");
      await refresh();
    } catch (e: any) {
      setError(String(e?.message ?? e));
    } finally {
      setDownloading(false);
    }
  }, [downloading, refresh, ticket]);

  return {
    phase,
    status,
    error,
    downloading,
    /** 能不能按：状态查到了、且各项都齐 */
    ready: !!status?.ok && status.enable !== false && status.runtime === true && status.model === true,
    start,
    stop,
    refresh,
    downloadModel,
    clearError: () => setError(""),
  };
}
