import { useEffect, useRef, useState } from "react";
import {
  DialogButton,
  DropdownItem,
  Focusable,
  GamepadButton,
  Navigation,
  TextField,
  ToggleField,
} from "@decky/ui";
import { ACCENT, DIM, ERR_COLOR, Md, TEXT } from "./ui";
import { Item, useSession } from "./useSession";
import { useVoiceInput } from "./useVoiceInput";

// 全屏页版式：按 Deck 的 1280×800 算，页面内高 800-48-44=708
// 页头 26 + 状态行（询问 46 / 聊天 28 / 设置 28）+ 间距 + 输入卡（仅聊天 60）+ 分隔线 1
// 询问页消息区 612、聊天页消息区 549；14px/1.5=21px/行 → 每行约 75 个汉字
const MAX_W = 1360;
const PAGE_TOP = 48;
const PAGE_BOTTOM = 44;

const CHAT_FONT = 14;
const CHAT_LH = 1.5;
const TAB_H = 26;
const ACTION_H = 46;
const COMPACT_H = 28;

/** 维护行的日志时间只到秒，24 小时制 */
const fmtLogTime = (ts: number) => {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const TABS = ["询问", "聊天", "设置"] as const;

const cardStyle: React.CSSProperties = {
  background: "rgba(255,255,255,.05)",
  border: "1px solid rgba(255,255,255,.08)",
  borderRadius: 4,
  padding: 16,
  display: "flex",
  flexDirection: "column",
  gap: 10,
  minWidth: 0,
  boxSizing: "border-box",
};

/** 询问页、聊天页共用同一条分隔线：1px 弱线，左右各留 2px 不贴边 */
const dividerStyle: React.CSSProperties = {
  height: 1,
  flexShrink: 0,
  margin: "0 2px",
  background: "rgba(139,146,154,.30)",
};

const sectionTitle: React.CSSProperties = {
  fontSize: 13,
  letterSpacing: 1,
  textTransform: "uppercase",
  color: ACCENT,
  fontWeight: 700,
};

const tabStyle = (active: boolean): React.CSSProperties => ({
  flexShrink: 0,
  height: TAB_H,
  minWidth: 64,
  padding: "0 10px",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  boxSizing: "border-box",
  fontSize: 12.5,
  borderRadius: 4,
  background: active ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
  color: active ? "#eaf2fb" : DIM,
});

// 忙碌态：跑着的那个保持原位、只换文案和底色，避免焦点跳走
const runningBtnStyle: React.CSSProperties = {
  background: "rgba(90,169,230,.18)",
  boxShadow: "inset 0 0 0 1px rgba(90,169,230,.55)",
  color: "#d9ecff",
};
const cancelBtnStyle: React.CSSProperties = {
  background: "rgba(224,108,117,.18)",
  color: "#f0c3c7",
};

function ActionButton({
  children,
  onClick,
  disabled,
  primary,
  description,
  style,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
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
        minHeight: 46,
        borderRadius: 4,
        fontSize: 14,
        boxSizing: "border-box",
        background: primary ? "rgba(90,169,230,.24)" : "rgba(255,255,255,.10)",
        color: TEXT,
        ...style,
      }}
    >
      {children}
    </DialogButton>
  );
}

/** 键帽：白底黑字小章，和 decky-music 的 Chip（src/ui/AppShell.tsx）一个路子 */
function KeyCap({ children }: { children: React.ReactNode }) {
  return (
    <span
      style={{
        flexShrink: 0,
        background: "#fff",
        color: "#000",
        fontWeight: 700,
        fontSize: 11,
        lineHeight: 1.4,
        borderRadius: 3,
        padding: "1px 5px",
      }}
    >
      {children}
    </span>
  );
}

function StatRow({
  label,
  value,
  wrap,
}: {
  label: string;
  value?: React.ReactNode;
  wrap?: boolean;
}) {
  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "84px minmax(0, 1fr)",
        gap: 8,
        alignItems: "baseline",
        fontSize: 13,
        lineHeight: 1.6,
        minWidth: 0,
      }}
    >
      <span style={{ color: DIM }}>{label}</span>
      <span
        style={{
          color: TEXT,
          minWidth: 0,
          whiteSpace: wrap ? "normal" : "nowrap",
          overflow: wrap ? "visible" : "hidden",
          textOverflow: "ellipsis",
          overflowWrap: wrap ? "anywhere" : "normal",
        }}
      >
        {value ?? "—"}
      </span>
    </div>
  );
}

function MessageBubble({
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
          maxWidth: "92%",
          minWidth: 0,
          padding: "8px 12px",
          borderRadius: 4,
          background: mine ? "rgba(90,169,230,.16)" : "rgba(255,255,255,.06)",
          borderLeft: mine ? "3px solid transparent" : `3px solid ${ACCENT}`,
          borderRight: mine ? `3px solid ${ACCENT}` : "3px solid transparent",
          boxSizing: "border-box",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          overflowWrap: "anywhere",
          // 正文字号/行高挂在这里：Md 的段落 div 不设字号，靠继承拿到
          fontSize: CHAT_FONT,
          lineHeight: CHAT_LH,
        }}
      >
        {image && (
          <img
            src={image}
            style={{
              display: "block",
              maxWidth: "100%",
              maxHeight: 320,
              objectFit: "contain",
              marginBottom: 8,
              borderRadius: 4,
            }}
          />
        )}
        <Md text={text} align={mine ? "right" : "left"} size={CHAT_FONT} />
        {streaming && <span style={{ color: DIM }}>▍</span>}
      </div>
    </div>
  );
}

function SectionCard({
  title,
  children,
  style,
}: {
  title: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <div style={{ ...cardStyle, minHeight: 0, ...style }}>
      <div style={sectionTitle}>{title}</div>
      {children}
    </div>
  );
}

/**
 * 消息区只此一份：询问页和聊天页都渲染同一个组件、读同一份 useSession 状态，
 * 切页签不会卸载消息数据，也不会出现两套列表。
 */
function ChatLog({
  items,
  streaming,
  busy,
  elapsed,
  scrollRef,
}: {
  items: Item[];
  streaming: string;
  busy: boolean;
  elapsed: number;
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
        padding: "2px 4px 2px 0",
        boxSizing: "border-box",
      }}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 8, width: "100%" }}>
        {items.map((m, i) => (
          <MessageBubble
            key={`${m.ts}-${i}`}
            mine={m.role === "user"}
            text={m.text}
            image={m.image}
          />
        ))}
        {streaming && <MessageBubble text={streaming} streaming />}
        {busy && !streaming && <MessageBubble text={`思考中…（${elapsed}s）`} />}
        {!items.length && !streaming && !busy && (
          <div style={{ color: DIM, textAlign: "center", fontSize: 13, padding: "28px 0" }}>
            还没有对话，先截个图或直接问吧
          </div>
        )}
      </div>
    </Focusable>
  );
}

/**
 * 多行提示词编辑框。@decky/ui 的 TextField 只有单行，装不下整段提示词，
 * 所以这里用原生 textarea：固定高度、内部滚动，全文可见。
 * 焦点树只认 Focusable，按 A 时把 DOM 焦点交给 textarea，Steam 会照常
 * 弹出虚拟键盘；B 退出键盘后焦点回到这层 Focusable。
 */
function PromptEditor({
  value,
  onChange,
  height = 130,
}: {
  value: string;
  onChange: (v: string) => void;
  height?: number;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  return (
    <Focusable
      onOKActionDescription="编辑"
      onActivate={() => ref.current?.focus()}
      style={{ width: "100%", minWidth: 0, display: "flex" }}
    >
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        style={{
          width: "100%",
          height,
          minHeight: height,
          resize: "none",
          overflowY: "auto",
          boxSizing: "border-box",
          padding: "10px 12px",
          borderRadius: 4,
          border: "1px solid rgba(255,255,255,.14)",
          background: "rgba(0,0,0,.22)",
          color: TEXT,
          fontSize: 13,
          lineHeight: 1.5,
          fontFamily: "inherit",
          outline: "none",
        }}
      />
    </Focusable>
  );
}

export default function BigScreen() {
  const s = useSession();
  const [tab, setTab] = useState(0);
  const [input, setInput] = useState("");
  const [logOpen, setLogOpen] = useState(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  // 语音结果追加到已有输入后面，不覆盖用户已经打的字
  const voice = useVoiceInput(s.ticket, (t) =>
    setInput((prev) => (prev.trim() ? prev.replace(/\s*$/, " ") + t : t))
  );

  // 大屏上最新一条应该在最下面；切页签后消息区会重新挂载，也要贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.items, s.streaming, tab]);

  // 进设置页再拉模型列表，和 QAM 的行为一致
  useEffect(() => {
    if (tab === 2 && s.ticket) void s.loadModels();
  }, [tab, s.ticket]);

  const locked = !!s.status && !s.status.unlocked;
  const ready = !!s.status && s.status.unlocked;
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

  const submit = () => {
    const text = input.trim();
    if (!text || s.busy) return;
    setInput("");
    void s.send(text);
  };

  // 语音提示只在不就绪时出现：报错优先，其次按原因给一句短提示 + 下载入口
  const voiceTip = (() => {
    if (voice.ready) return null;
    if (voice.error) return { text: voice.error, color: ERR_COLOR, canDownload: false };
    const st = voice.status;
    if (!st) return null;
    if (!st.ok) return { text: "语音不可用：后端没连上", color: DIM, canDownload: false };
    if (st.enable === false) return { text: "语音输入已在设置里关闭", color: DIM, canDownload: false };
    if (!st.runtime) return { text: "语音不可用：后端没装 sherpa-onnx", color: DIM, canDownload: false };
    if (!st.model) return { text: "语音模型未下载（约 1GB，只需一次）", color: DIM, canDownload: true };
    if (st.record === false) return { text: st.record_reason || "录音设备不可用", color: DIM, canDownload: false };
    return null;
  })();

  // 状态行只显示结论：空闲留空（结构保留，后续填内容），忙时显示思考中
  const statusLine = s.busy ? `思考中…（${s.elapsed}s）` : "";

  // 询问页两个动作按钮的忙碌态：被点的那个留在原位显示「思考中」，
  // 另一个原地换成「中断」，焦点不会因为按钮消失而跳走。
  const askRunning = s.busy && s.runningAction !== "translate";
  const translateRunning = s.busy && s.runningAction === "translate";

  // 询问页和聊天页共用同一条核心异常提示
  const coreAlert = () =>
    s.status && !s.status.core_ok ? (
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          padding: "10px 14px",
          borderRadius: 4,
          background: "rgba(224,108,117,.10)",
          color: ERR_COLOR,
          fontSize: 13,
          flexShrink: 0,
          minWidth: 0,
        }}
      >
        <span
          style={{
            flex: 1,
            minWidth: 0,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {coreStopped
            ? "核心已停止（不会自动拉起，点「启动核心」恢复）"
            : "核心异常（点「启动核心」恢复）"}
        </span>
        <div style={{ width: 160, flex: "0 0 160px" }}>
          <ActionButton
            onClick={() => void s.coreStart()}
            disabled={s.starting}
            description="启动核心"
          >
            {s.starting ? "核心启动中…" : "启动核心"}
          </ActionButton>
        </div>
      </div>
    ) : null;

  const chatLog = () => (
    <ChatLog
      items={s.items}
      streaming={s.streaming}
      busy={s.busy}
      elapsed={s.elapsed}
      scrollRef={scrollRef}
    />
  );

  // 维护行只显示最新一条，展开后看最近 3 条
  const latestLog = s.logs[0];

  return (
    <Focusable
      noFocusRing
      flow-children="vertical"
      onCancelActionDescription="返回"
      // 有图例槽位的快捷键挂到根节点，焦点在子元素上时 Steam 会把祖先的说明合并显示
      {...(tab === 0 && ready && !s.busy
        ? {
            onSecondaryActionDescription: "翻译",
            onOptionsActionDescription: "截图询问",
          }
        : {})}
      {...(s.busy ? { onMenuActionDescription: "中断" } : {})}
      onButtonDown={(evt) => {
        const detail = evt?.detail;
        if (!detail || detail.is_repeat) return;
        if (detail.button === GamepadButton.BUMPER_LEFT) {
          setTab((t) => Math.max(0, t - 1));
          return;
        }
        if (detail.button === GamepadButton.BUMPER_RIGHT) {
          setTab((t) => Math.min(TABS.length - 1, t + 1));
          return;
        }
        if (detail.button === GamepadButton.START && s.busy) {
          void s.cancel();
          return;
        }
        if (tab !== 0 || !ready || s.busy) return;
        // X = 翻译，Y = 截图询问
        if (detail.button === GamepadButton.SECONDARY) {
          void s.shotTranslate();
        } else if (detail.button === GamepadButton.OPTIONS) {
          void s.shotAsk();
        }
      }}
      style={{
        height: "100%",
        width: "100%",
        boxSizing: "border-box",
        padding: `${PAGE_TOP}px 48px ${PAGE_BOTTOM}px`,
        display: "flex",
        flexDirection: "column",
        gap: 12,
        background: "#1b1f26",
        color: TEXT,
        overflow: "hidden",
      }}
    >
      <header
        style={{
          width: "100%",
          maxWidth: MAX_W,
          margin: "0 auto",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          minWidth: 0,
          flexShrink: 0,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            minWidth: 0,
            flexShrink: 1,
            overflow: "hidden",
          }}
        >
          <div
            style={{
              flexShrink: 0,
              fontSize: 22,
              lineHeight: 1.2,
              fontWeight: 700,
              color: "#fff",
              letterSpacing: 0.4,
            }}
          >
            DeckMeido
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              minWidth: 0,
              overflow: "hidden",
            }}
          >
            <StatusChip color={coreColor}>{coreLine}</StatusChip>
            <StatusChip>{s.status ? `${s.status.ip}:${s.status.port}` : "—"}</StatusChip>
            {s.status?.game_name && <StatusChip>{s.status.game_name}</StatusChip>}
          </div>
        </div>

        {/* 页签收进页头右侧；L1/R1 没有原生图例槽位，用键帽就地标出 */}
        {!locked && ready && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              flexShrink: 0,
            }}
          >
            <KeyCap>L1</KeyCap>
            {TABS.map((name, i) => (
              <Focusable
                key={name}
                autoFocus={i === 0}
                onActivate={() => setTab(i)}
                onOKActionDescription="切换页签"
                onCancelActionDescription="返回"
                style={tabStyle(tab === i)}
              >
                {name}
              </Focusable>
            ))}
            <KeyCap>R1</KeyCap>
          </div>
        )}
      </header>

      {/* 状态行：询问页常驻（右侧是截图按钮）；聊天页/设置页只在忙时出现「中断」 */}
      {(tab === 0 || s.busy) && (
      <div
        style={{
          width: "100%",
          maxWidth: MAX_W,
          margin: "0 auto",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
          minHeight: tab === 0 ? ACTION_H : COMPACT_H,
        }}
      >
        <span
          style={{
            flex: 1,
            minWidth: 0,
            color: DIM,
            fontSize: 12.5,
            lineHeight: 1.4,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {statusLine}
        </span>

        {tab === 0 && (
          <div style={{ display: "flex", gap: 10, flexShrink: 0, height: ACTION_H }}>
            <div style={{ width: 160, height: ACTION_H }}>
              {!s.busy ? (
                <ActionButton
                  primary
                  disabled={!ready}
                  onClick={() => void s.shotAsk()}
                  description="截图询问"
                >
                  截图询问
                </ActionButton>
              ) : askRunning ? (
                <ActionButton
                  onClick={() => {}}
                  description="思考中"
                  style={runningBtnStyle}
                >
                  思考中…（{s.elapsed}s）
                </ActionButton>
              ) : (
                <ActionButton
                  onClick={() => void s.cancel()}
                  description="中断"
                  style={cancelBtnStyle}
                >
                  中断
                </ActionButton>
              )}
            </div>
            <div style={{ width: 160, height: ACTION_H }}>
              {!s.busy ? (
                <ActionButton
                  disabled={!ready}
                  onClick={() => void s.shotTranslate()}
                  description="截图翻译"
                >
                  截图翻译
                </ActionButton>
              ) : translateRunning ? (
                <ActionButton
                  onClick={() => {}}
                  description="思考中"
                  style={runningBtnStyle}
                >
                  思考中…（{s.elapsed}s）
                </ActionButton>
              ) : (
                <ActionButton
                  onClick={() => void s.cancel()}
                  description="中断"
                  style={cancelBtnStyle}
                >
                  中断
                </ActionButton>
              )}
            </div>
          </div>
        )}

        {tab !== 0 && s.busy && (
          <div style={{ width: 110, height: COMPACT_H, flexShrink: 0 }}>
            <ActionButton
              onClick={() => void s.cancel()}
              description="中断"
              style={{ minHeight: COMPACT_H, height: COMPACT_H, fontSize: 13 }}
            >
              中断
            </ActionButton>
          </div>
        )}
      </div>
      )}

      {!s.status ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ ...cardStyle, width: "min(560px, 100%)", alignItems: "stretch" }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#fff" }}>
              {s.probing ? "正在连接插件后端…" : "插件后端未连上"}
            </div>
            <div style={{ color: DIM, fontSize: 13, lineHeight: 1.5 }}>
              后端由 Decky 插件管理；若一直连不上，可重试，或按 B 返回 QAM 启动后端。
            </div>
            <ActionButton
              onClick={() => void s.refreshStatus()}
              primary
              description="重试"
            >
              重试连接
            </ActionButton>
          </div>
        </div>
      ) : locked ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ ...cardStyle, width: "min(560px, 100%)", alignItems: "stretch" }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#fff" }}>需要访问令牌</div>
            <div style={{ color: DIM, fontSize: 13, lineHeight: 1.5 }}>
              插件以 root 运行，输入访问令牌后才能使用（默认 meido）。
            </div>
            <Focusable autoFocus onOKActionDescription="输入" style={{ width: "100%" }}>
              <TextField
                value={s.tokenInput}
                placeholder="访问令牌"
                onChange={(e) => s.setTokenInput(e.target.value)}
                style={{ width: "100%" }}
              />
            </Focusable>
            <ActionButton onClick={() => void s.unlock()} primary description="解锁">
              解锁
            </ActionButton>
          </div>
        </div>
      ) : tab === 0 ? (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {coreAlert()}

          {/* 动作按钮已移到状态行右侧，整行高度让给消息区 */}

          {s.pendingPoke && (
            <div
              style={{
                ...cardStyle,
                flexDirection: "row",
                alignItems: "center",
                gap: 12,
                padding: "10px 14px",
                flexShrink: 0,
              }}
            >
              <div style={{ flex: 1, minWidth: 0, color: DIM, fontSize: 13, lineHeight: 1.45 }}>
                画面静止，截不到图。随便动一下摇杆或触摸板，再重新截图。
              </div>
              <div style={{ width: 170, flex: "0 0 170px" }}>
                <ActionButton onClick={() => void s.retryPoke()} description="重试">
                  重新截图
                </ActionButton>
              </div>
            </div>
          )}

          {/* 状态行与消息区之间：规格与聊天页一致；-2px 抵消页面根间距，上下都留 10px */}
          <div style={{ ...dividerStyle, marginTop: -2 }} />

          {chatLog()}
        </div>
      ) : tab === 1 ? (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {coreAlert()}

          {/* 聊天页：输入区不要卡片外框；输入框自带细边，与发送按钮同为 44px */}
          <div
            style={{
              display: "flex",
              alignItems: "stretch",
              gap: 8,
              width: "100%",
              height: 44,
              flexShrink: 0,
              position: "relative",
            }}
          >
            <Focusable
              onOKActionDescription="输入"
              style={{ flex: 1, minWidth: 0, display: "flex" }}
            >
              <TextField
                value={input}
                placeholder="问问 Meido..."
                onChange={(e) => setInput(e.target.value)}
                style={{
                  width: "100%",
                  height: 44,
                  minHeight: 44,
                  padding: "0 12px",
                  boxSizing: "border-box",
                  fontSize: 14,
                  lineHeight: "42px",
                  borderRadius: 4,
                  border: "1px solid rgba(255,255,255,.14)",
                  background: "rgba(255,255,255,.08)",
                }}
              />
            </Focusable>
            <div style={{ flex: "0 0 96px", width: 96, height: 44 }}>
              <ActionButton
                disabled={s.busy || voice.phase === "working" || !voice.ready}
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
                  height: 44,
                  minHeight: 44,
                  // 录音中整块变强调色，手柄注意力在输入框上也能一眼看出在录
                  background: voice.phase === "recording" ? ACCENT : "rgba(255,255,255,.10)",
                  color: voice.phase === "recording" ? "#1b1b1b" : TEXT,
                }}
              >
                {voice.phase === "working" ? "识别中" : voice.phase === "recording" ? "松开" : "语音"}
              </ActionButton>
            </div>
            <div style={{ flex: "0 0 160px", width: 160, height: 44 }}>
              <ActionButton
                primary
                disabled={s.busy || !input.trim()}
                onClick={submit}
                description="发送"
                style={{ height: 44, minHeight: 44 }}
              >
                发送
              </ActionButton>
            </div>
            {/* 语音提示：绝对定位浮在输入行下方，不占版式高度 */}
            {voiceTip && (
              <div
                style={{
                  position: "absolute",
                  top: 48,
                  left: 0,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  height: 26,
                  // 只有下载按钮要接焦点，整条浮层不挡消息区
                  pointerEvents: voiceTip.canDownload ? "auto" : "none",
                }}
              >
                <span style={{ fontSize: 12, color: voiceTip.color }}>{voiceTip.text}</span>
                {voiceTip.canDownload && (
                  <ActionButton
                    disabled={voice.downloading}
                    onClick={voice.downloadModel}
                    description="下载语音模型"
                    style={{
                      width: "auto",
                      height: 26,
                      minHeight: 26,
                      padding: "0 10px",
                      fontSize: 12,
                    }}
                  >
                    {voice.downloading ? "下载中…" : "下载模型"}
                  </ActionButton>
                )}
              </div>
            )}
          </div>
          {/* 输入区与消息区之间：与询问页共用同一条分隔线 */}
          <div style={dividerStyle} />

          {chatLog()}
        </div>
      ) : (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            display: "grid",
            gridTemplateColumns: "repeat(6, minmax(0, 1fr))",
            gridTemplateRows: "1fr 1fr",
            gap: 20,
            boxSizing: "border-box",
            overflow: "hidden",
          }}
        >
          <SectionCard
            title="模型"
            style={{ gridColumn: "span 2", padding: 16, gap: 10, overflow: "hidden" }}
          >
            <div style={{ width: "100%", minWidth: 0 }}>
              <DropdownItem
                childrenContainerWidth="max"
                layout="below"
                rgOptions={(s.modelList.length ? s.modelList : [s.model].filter(Boolean)).map((m) => ({
                  data: m,
                  label: m,
                }))}
                selectedOption={s.model}
                menuLabel="选择模型"
                strDefaultLabel={s.loadingModels ? "加载中…" : "选择模型"}
                disabled={!s.status?.core_ok}
                onChange={(o) => {
                  if (o.data && o.data !== s.model) void s.changeModel(String(o.data));
                }}
              />
            </div>
            <ActionButton
              onClick={() => void s.loadModels()}
              disabled={s.loadingModels}
              description="刷新"
            >
              {s.loadingModels ? "拉取中…" : "刷新模型"}
            </ActionButton>
            {/* 两个开关同一组：语音播报 + 带画面（后者从对话页移过来） */}
            <ToggleField
              label="语音播报"
              description="回复生成完在本机念一遍"
              checked={s.tts}
              onChange={(v) => void s.toggleTts(v)}
            />
            <ToggleField
              label="带画面"
              description="问的时候附带当前游戏画面"
              checked={s.withShot}
              onChange={s.setWithShot}
            />
          </SectionCard>

          <SectionCard
            title="截屏询问"
            style={{ gridColumn: "span 2", padding: 16, gap: 8, overflow: "hidden" }}
          >
            <div style={{ color: DIM, fontSize: 12.5, lineHeight: 1.45 }}>
              截图询问时带给模型的提示词
            </div>
            <PromptEditor value={s.askTplEdit} onChange={s.setAskTplEdit} />
            <div style={{ color: DIM, fontSize: 11.5, lineHeight: 1.4 }}>
              按 A 编辑；内容超长可在框内滚动。
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                gap: 8,
                width: "100%",
              }}
            >
              <ActionButton onClick={() => void s.saveAskTpl()} description="保存">
                保存
              </ActionButton>
              <ActionButton
                onClick={() => s.setAskTplEdit(s.askTplDefault)}
                description="恢复默认"
              >
                恢复默认
              </ActionButton>
            </div>
          </SectionCard>

          <SectionCard
            title="截图翻译"
            style={{ gridColumn: "span 2", padding: 16, gap: 8, overflow: "hidden" }}
          >
            <div style={{ color: DIM, fontSize: 12.5, lineHeight: 1.45 }}>
              截图翻译时带给模型的提示词
            </div>
            <PromptEditor value={s.translateTplEdit} onChange={s.setTranslateTplEdit} />
            <div style={{ color: DIM, fontSize: 11.5, lineHeight: 1.4 }}>
              保存后写入后端配置，下一次「截图翻译」立即生效。
            </div>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                gap: 8,
                width: "100%",
              }}
            >
              <ActionButton onClick={() => void s.saveTranslateTpl()} description="保存">
                保存
              </ActionButton>
              <ActionButton
                onClick={() => s.setTranslateTplEdit(s.translateTplDefault)}
                description="恢复默认"
              >
                恢复默认
              </ActionButton>
            </div>
          </SectionCard>

          <SectionCard
            title="连接"
            style={{ gridColumn: "span 2", padding: 16, gap: 10, overflow: "hidden" }}
          >
            <StatRow label="核心" value={coreLine} />
            <StatRow label="地址" value={s.status ? `${s.status.ip}:${s.status.port}` : "—"} />
            <StatRow
              label="截图"
              value={s.status?.capture_ok ? "可用" : s.status?.capture_reason || "不可用"}
              wrap
            />
            <StatRow label="虚拟手柄" value={s.status?.gamepad ? "可用" : "不可用"} />
            <StatRow
              label="当前游戏"
              value={
                s.status?.game_name
                  ? `${s.status.game_name}（${s.status.game_source || "?"}）`
                  : "—"
              }
            />
            <StatRow label="版本" value={s.status?.version || "—"} />
            <StatRow label="工作区" value={s.status?.workspace || "—"} wrap />
          </SectionCard>

          <SectionCard
            title="维护"
            style={{ gridColumn: "span 4", padding: 16, gap: 10, overflow: "hidden" }}
          >
            {logOpen ? (
              <>
                {/* 大屏唯一看日志的地方：最近 3 条，正文过长就在框内滚动 */}
                <div
                  style={{
                    flex: 1,
                    minHeight: 0,
                    overflowY: "auto",
                    display: "flex",
                    flexDirection: "column",
                    gap: 10,
                  }}
                >
                  {s.logs.slice(0, 3).map((e, i) => (
                    <div
                      key={`${e.ts}-${i}`}
                      style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}
                    >
                      <div style={{ fontSize: 12, fontWeight: 700, color: ACCENT }}>
                        {e.code} · {fmtLogTime(e.ts)}
                      </div>
                      <div
                        style={{
                          fontSize: 12.5,
                          lineHeight: 1.5,
                          color: TEXT,
                          overflowWrap: "anywhere",
                          wordBreak: "break-word",
                        }}
                      >
                        {e.detail || "—"}
                      </div>
                    </div>
                  ))}
                  {!s.logs.length && <div style={{ color: DIM, fontSize: 13 }}>—</div>}
                </div>
                <ActionButton onClick={() => setLogOpen(false)} description="收起">
                  收起
                </ActionButton>
              </>
            ) : (
              <>
                <Focusable
                  onActivate={() => setLogOpen(true)}
                  onOKActionDescription="查看"
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    minWidth: 0,
                    flexShrink: 0,
                    padding: "6px 10px",
                    borderRadius: 4,
                    background: "rgba(255,255,255,.06)",
                    border: "1px solid rgba(255,255,255,.10)",
                    boxSizing: "border-box",
                  }}
                >
                  <span style={{ color: DIM, fontSize: 12.5, flexShrink: 0 }}>最近事件</span>
                  <span
                    style={{
                      flex: 1,
                      minWidth: 0,
                      textAlign: "right",
                      fontSize: 13,
                      color: latestLog ? TEXT : DIM,
                      whiteSpace: "nowrap",
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                  >
                    {latestLog ? `${latestLog.code} · ${fmtLogTime(latestLog.ts)}` : "—"}
                  </span>
                </Focusable>

                {/* 核心三按钮：启动核心 / 重启核心 / 关闭核心。关闭后保持停止，只有「启动核心」能恢复 */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                    gap: 10,
                    width: "100%",
                  }}
                >
                  <ActionButton
                    onClick={() => void s.coreStart()}
                    disabled={coreRunning || s.starting || s.restarting || s.killing}
                    primary={!coreRunning}
                    description="启动核心"
                  >
                    {s.starting ? "核心启动中…" : "启动核心"}
                  </ActionButton>
                  <ActionButton
                    onClick={() => void s.coreRestart()}
                    disabled={!coreRunning || s.starting || s.restarting || s.killing}
                    description="重启核心"
                  >
                    {s.restarting ? "核心重启中…" : "重启核心"}
                  </ActionButton>
                  <ActionButton
                    onClick={() => void s.coreKill()}
                    disabled={!coreRunning || s.starting || s.restarting || s.killing}
                    description="关闭核心"
                  >
                    {s.killing ? "核心关闭中…" : "关闭核心"}
                  </ActionButton>
                </div>
                <div style={{ color: DIM, fontSize: 12.5, lineHeight: 1.45 }}>
                  {coreStopped
                    ? "核心已停止：不会自动拉起，只有点「启动核心」才会恢复。"
                    : "关闭核心会停止后端进程并保持停止；「重启核心」只在运行中可用。"}
                </div>
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                    gap: 10,
                    width: "100%",
                  }}
                >
                  <ActionButton
                    onClick={() => void s.newSession()}
                    disabled={s.busy || s.newing}
                    description="新会话"
                  >
                    {s.newing ? "新建中…" : "新会话"}
                  </ActionButton>
                  {/* B 键由 Steam 原生路由返回处理；这里再给一个显式入口，原生返回不生效时也不至于卡在页里 */}
                  <ActionButton
                    onClick={() => {
                      try {
                        Navigation.NavigateBack();
                      } catch (e: any) {
                        console.error("[DeckMeido] 返回游戏失败", e);
                      }
                    }}
                    description="返回游戏"
                  >
                    返回游戏
                  </ActionButton>
                </div>
                <div style={{ color: DIM, fontSize: 12.5, lineHeight: 1.45 }}>
                  新会话只清本地记录，不会重启核心。
                </div>
              </>
            )}
          </SectionCard>
        </div>
      )}
    </Focusable>
  );
}

function StatusChip({
  children,
  color,
}: {
  children: React.ReactNode;
  color?: string;
}) {
  return (
    <span
      style={{
        display: "inline-block",
        maxWidth: 260,
        padding: "4px 10px",
        borderRadius: 4,
        background: "rgba(255,255,255,.07)",
        color: color ?? DIM,
        fontSize: 12,
        lineHeight: 1.4,
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
        minWidth: 0,
      }}
    >
      {children}
    </span>
  );
}
