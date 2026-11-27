"""哔哔啵啵：把一句话变成小人说话的电子音，纯代码合成，不依赖任何模型。

移植自 https://github.com/hakihakii/bibibobo （MIT），出自《路墨小剧场》。
原实现是 JS（bibibobo.js），这里逐行对应成 Python，参数、哈希、
语气规则都保持一致，所以同一个字得到的声音和原版相同。

做法只有五步：
    一条锯齿波 → 两个带通滤波器捏出口型 → 一个字一声（按字算出音高和口型）
    → 每个角色一组音阶 → 标点管语气。

只用标准库（math / struct / wave），Deck 上不需要装任何东西。
"""
from __future__ import annotations

import io
import math
import re
import struct
import wave

TAU = math.pi * 2
SAMPLE_RATE = 44100

# 五种口型：每种只是两个共振峰频率（Hz）
VOWELS = (
    (800, 1200),
    (400, 2200),
    (350, 800),
    (600, 1700),
    (300, 2600),
)

# 声线：音阶（MIDI 音高）、共振峰倍率、每个字的时长上限、字头小滑音、
# 句尾是否下落、音量、默认语速（每个字间隔多少秒）
VOICES = {
    # 大肥鱼：E5 G5 A5 B5 C6，共振峰更尖，每个字开头有个很短的小滑音
    "fish": {
        "scale": (76, 79, 81, 83, 84),
        "formant": 1.25, "max_dur": 0.13, "hold": 0.8,
        "chirp": 1.5, "fall": 0, "gain": 0.16, "step": 0.13,
    },
    # 路墨：D4 E4 G4 A4，共振峰更低，句尾轻轻往下落
    "lumo": {
        "scale": (62, 64, 67, 69),
        "formant": 0.9, "max_dur": 0.2, "hold": 0.7,
        "chirp": 0, "fall": -2, "gain": 0.17, "step": 0.16,
    },
}

DEFAULT_VOICE = "fish"
GAP = 0.22            # 句子之间停多少秒
TAIL = 0.4            # 末尾留白，避免播放器把最后一个字的尾巴切掉

# 一个汉字算一声；一串英文或数字算一声
_SYL_RE = re.compile(r"[A-Za-z_/.0-9]+|[\u4e00-\u9fff]")
# 按标点切句，每句各自套一条语气规则
_SENT_RE = re.compile(r"[^？?！!。.，,、；;…\n]+[？?！!。.，,、；;…]*")
# 只有标点的残句不发声
_HAS_SYL_RE = re.compile(r"[A-Za-z_/.0-9\u4e00-\u9fff]")


def _mtof(midi: float) -> float:
    """MIDI 音高 → 频率"""
    return 440 * 2 ** ((midi - 69) / 12)


def _hash(text: str) -> int:
    """同一个字永远得到同一个数，所以同一个字永远是同一声"""
    h = 7
    for ch in text:
        h = (h * 31 + ord(ch)) % 9973
    return h


def syllables(text: str) -> list[str]:
    """一个汉字算一声，一串英文或数字算一声"""
    return _SYL_RE.findall(text)


def _voice_of(name: str | dict) -> dict:
    if isinstance(name, dict):
        base = dict(VOICES[DEFAULT_VOICE])
        base.update(name)
        return base
    return dict(VOICES.get(name) or VOICES[DEFAULT_VOICE])


def synth(text: str, voice: str | dict = DEFAULT_VOICE, *,
          step: float | None = None, sample_rate: int = SAMPLE_RATE,
          gap: float = GAP, speed: float = 1.0,
          pitch: float = 1.0) -> list[float]:
    """把一句话合成为单声道采样（范围 -1..1）。

    与 JS 版一致：问号结尾尾音上扬 5 个半音；带感叹号整句升高 3 个半音；
    省略号结尾整句压低 3 个半音、尾音下滑 4 个半音。

    pitch：整体音调倍率（0.5~2.0），换算成半音偏移后加到每个字的音高上。
    """
    v = _voice_of(voice)
    step = v["step"] if step is None else step
    # speed：界面的语速倍率（1.0 = 声线原本的字步长）。
    # 只缩字与字之间的步长，句间停顿保持不变，快起来也不会糊成一串
    step = step / min(2.0, max(0.5, float(speed or 1.0)))
    # 音调倍率 → 半音偏移：2.0 = 高一个八度（+12），0.5 = 低一个八度（-12）
    pitch = min(2.0, max(0.5, float(pitch or 1.0)))
    shift = 12.0 * math.log2(pitch)

    sentences = []
    for chunk in _SENT_RE.findall(text or ""):
        tokens = syllables(chunk)
        if tokens:
            sentences.append((chunk, tokens))

    total = sum(len(tk) * step + gap for _, tk in sentences) + TAIL
    out = [0.0] * max(1, math.ceil(total * sample_rate))

    t = 0.02
    for chunk, tokens in sentences:
        ask = re.search(r"[？?]$", chunk) is not None
        shout = re.search(r"[！!]", chunk) is not None
        sad = re.search(r"(……|\.\.\.|…)$", chunk) is not None
        last_index = len(tokens) - 1
        for i, word in enumerate(tokens):
            last = i == last_index
            code = _hash(word)
            midi = (v["scale"][code % len(v["scale"])]
                    + (3 if shout else 0) - (3 if sad else 0)
                    + shift)
            dur = min(step * v["hold"], v["max_dur"])
            if last and (ask or shout or sad):
                dur += 0.1
            if last and ask:
                glide = 5
            elif last and sad:
                glide = -4
            elif last:
                glide = v["fall"]
            else:
                glide = 0
            _voice_into(out, sample_rate, t + i * step, midi, dur, v, glide, code)
        t += len(tokens) * step + gap

    # 软限幅，响一点也不破音
    return [math.tanh(x * 2.4) for x in out]


def _saw_step(phase: float, dt: float) -> tuple[float, float]:
    """polyBLEP 抗混叠锯齿波的一步：返回 (输出, 新相位)"""
    phase += dt
    if phase >= 1:
        phase -= 1
    val = 2 * phase - 1
    if phase < dt:
        t = phase / dt
        val -= t + t - t * t - 1
    elif phase > 1 - dt:
        t = (phase - 1) / dt
        val -= t * t + t + t + 1
    return val, phase


def _voice_into(out: list[float], sample_rate: int, t: float, midi: float,
                dur: float, v: dict, glide: float, vowel: int) -> None:
    """把一声写进 out：音高 midi，时长 dur 秒，glide 是结尾滑多少个半音"""
    a, b = VOWELS[vowel % len(VOWELS)]
    start = int(t * sample_rate)
    n = int(dur * sample_rate)
    if n <= 0:
        return
    f1c, f2c = a * v["formant"], b * v["formant"]
    gain, chirp = v["gain"], v["chirp"]
    # 状态变量滤波器（带通）的系数在固定截止频率下是常量，先算好
    q1, q2 = 4.0, 5.0
    limit = sample_rate / 6
    f1k = 2 * math.sin(math.pi * min(f1c, limit) / sample_rate)
    f2k = 2 * math.sin(math.pi * min(f2c, limit) / sample_rate)
    lp1 = bp1 = lp2 = bp2 = 0.0
    phase = 0.0
    for i in range(n):
        idx = start + i
        if idx >= len(out):
            break
        x = i / sample_rate
        # 滑音 + 字头小滑音 + 一点颤音
        hz = _mtof(midi + glide * (x / dur)
                   + chirp * math.exp(-x * 30)) * (1 + 0.012 * math.sin(TAU * 6 * x))
        s, phase = _saw_step(phase, hz / sample_rate)
        # 5 毫秒起音，25 毫秒收尾
        env = min(1.0, x * 200) * min(1.0, (dur - x) * 40)
        hp1 = s - lp1 - bp1 / q1
        bp1 += f1k * hp1
        lp1 += f1k * bp1
        hp2 = s - lp2 - bp2 / q2
        bp2 += f2k * hp2
        lp2 += f2k * bp2
        out[idx] += (bp1 + 0.7 * bp2) * env * gain


def to_wav(samples: list[float], sample_rate: int = SAMPLE_RATE) -> bytes:
    """把采样打包成 16 位单声道 WAV 文件内容"""
    frames = bytearray()
    for value in samples:
        clamped = max(-1.0, min(1.0, value))
        frames += struct.pack("<h", int(round(clamped * 32767)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(sample_rate)
        fh.writeframes(bytes(frames))
    return buf.getvalue()


def render(text: str, voice: str = DEFAULT_VOICE, *,
           step: float | None = None, sample_rate: int = SAMPLE_RATE,
           speed: float = 1.0, pitch: float = 1.0) -> bytes:
    """直接给一段话的完整 wav 字节"""
    return to_wav(synth(text, voice, step=step, sample_rate=sample_rate,
                        speed=speed, pitch=pitch),
                  sample_rate)
