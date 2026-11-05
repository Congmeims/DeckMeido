import { useEffect, useRef, useState } from "react";
import {
  DialogButton,
  Focusable,
  GamepadButton,
  Navigation,
  showModal,
} from "@decky/ui";
import { ACCENT, DIM, ERR_COLOR, ImeTextArea, ImeTextField, Md, TEXT, copyText, groupToken } from "./ui";
import { statusLineFor } from "./statusLine";
import { Item, useSession } from "./useSession";
import { useVoiceInput } from "./useVoiceInput";

// 全屏页版式：按 Deck 的 1280×800 算。
// 800 里真正能用的只有中间那条：顶部状态栏约 65、底部按键图例条约 50。
// 实测页头还能再往上提一点，所以顶部只让 44、底部让 46，多出的 ~25px
// 全部给内容区（三页共用）。
const PAGE_TOP = 44;
const PAGE_BOTTOM = 46;

const CHAT_FONT = 14;
const CHAT_LH = 1.5;
const TAB_H = 26;
const ACTION_H = 46;

/** 维护行的日志时间只到秒，24 小时制 */
const fmtLogTime = (ts: number) => {
  const d = new Date(ts * 1000);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
};

const TABS = ["询问", "聊天", "设置"] as const;

/** 控制盘基调：深海军底 + 细微内阴影，卡片有角色色左边条 */
const cardStyle: React.CSSProperties = {
  background: "rgba(14,20,28,.92)",
  border: "1px solid rgba(90,169,230,.08)",
  borderRadius: 3,
  padding: 8,
  display: "flex",
  flexDirection: "column",
  gap: 6,
  minWidth: 0,
  boxSizing: "border-box",
};

/** 主题色：所有卡片统一用同一个蓝，不再按功能分色 */
const C = {
  cyber: "#5AA9E6",
  model: "#5AA9E6",
  prompt: "#5AA9E6",
  ops: "#5AA9E6",
};

/** 询问页、聊天页共用同一条分隔线：1px 弱线，左右各留 2px 不贴边 */
const dividerStyle: React.CSSProperties = {
  height: 1,
  flexShrink: 0,
  margin: "0 2px",
  background: "rgba(139,146,154,.30)",
};

const sectionTitle: React.CSSProperties = {
  fontSize: 11.5,
  letterSpacing: 2.5,
  textTransform: "uppercase",
  color: DIM,
  fontWeight: 600,
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
  compact,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
  description?: string;
  style?: React.CSSProperties;
  /** 紧凑高度：按钮高度压到和一行文字齐平，给设置页省纵向空间 */
  compact?: boolean;
}) {
  return (
    <DialogButton
      disabled={disabled}
      onClick={() => onClick()}
      onOKActionDescription={description ?? "确认"}
      onCancelActionDescription="返回"
      style={{
        width: "100%",
        // DialogButton 自带 Steam 默认 min-width，塞进窄列会被撑爆导致
        // 出界/重叠（箭头按钮、开关按钮都栽过这里），必须压掉
        minWidth: 0,
        minHeight: compact ? 22 : 46,
        fontSize: 13,
        lineHeight: 1.2,
        paddingLeft: 8,
        paddingRight: 8,
        paddingTop: compact ? 1 : 4,
        paddingBottom: compact ? 1 : 4,
        whiteSpace: "nowrap",
        boxSizing: "border-box",
        // 控制盘按钮：primary 青高亮，普通半透黑 + 青细边
        background: primary ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.04)",
        border: `1px solid ${primary ? "rgba(90,169,230,.55)" : "rgba(255,255,255,.12)"}`,
        borderRadius: 3,
        color: primary ? "#eaf2fb" : TEXT,
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
        gridTemplateColumns: "52px minmax(0, 1fr)",
        gap: 8,
        alignItems: "center",
        fontSize: 12.5,
        lineHeight: 1.3,
        minWidth: 0,
        height: 18,
        overflow: "hidden",
      }}
    >
      <span style={{ color: DIM, fontSize: 11 }}>{label}</span>
      <span
        style={{
          minWidth: 0,
          whiteSpace: wrap ? "normal" : "nowrap",
          overflow: wrap ? "visible" : "hidden",
          textOverflow: "ellipsis",
          overflowWrap: wrap ? "anywhere" : "normal",
          // 等宽字体 + 青色微亮，终端读数感
          fontFamily: "'JetBrains Mono', 'Consolas', monospace",
          fontSize: 12,
          color: "#bcd6ea",
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
          // 气泡按内容收缩：没有这行时盒子会被最宽子元素撑开后不再回缩，
          // 短行消息右侧就留一大块空白（截图里接收方气泡右边空的来源）
          width: "fit-content",
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
              // 图片按自身缩放上限定宽：百分比会反过来撑大 fit-content 气泡，
              // 带图消息右边就留一大块空白
              maxWidth: 420,
              maxHeight: 256,
              width: "auto",
              height: "auto",
              objectFit: "contain",
              marginBottom: text ? 8 : 0,
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
  inlineTitle,
  accent,
}: {
  title: string;
  children: React.ReactNode;
  style?: React.CSSProperties;
  inlineTitle?: boolean;
  /** 卡片角色色：控制标题文字 + 左边条 */
  accent?: string;
}) {
  if (inlineTitle) {
    return (
      <div
        style={{
          ...cardStyle,
          flex: 1,
          minHeight: 0,
          display: "flex",
          flexDirection: "row",
          gap: 10,
          overflow: "hidden",
          // 角色色左边条 + 顶边微光
          borderLeft: `3px solid ${accent ?? C.cyber}`,
          boxShadow: `inset 1px 0 0 ${accent ?? C.cyber}11`,
          ...style,
        }}
      >
        {/* 外层普通 flex 做垂直居中；writingMode 只放内层文字。
            Deck 的 CEF 对 vertical-rl 元素自身的 flex 轴映射不可靠，
            之前把 flex 直接压在竖排元素上导致标题被裁掉 */}
        <div style={{ flexShrink: 0, display: "flex", alignItems: "center" }}>
          <div
            style={{
              ...sectionTitle,
              writingMode: "vertical-rl",
              whiteSpace: "nowrap",
              lineHeight: 1.1,
              color: accent ?? C.cyber,
            }}
          >
            {title}
          </div>
        </div>
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 4,
            minWidth: 0,
            minHeight: 0,
            flex: 1,
            // 卡内内容整体垂直居中：上下留白对称，不再顶贴
            justifyContent: "center",
          }}
        >
          {children}
        </div>
      </div>
    );
  }
  return (
    <div
      style={{
        ...cardStyle,
        minHeight: 0,
        borderLeft: `3px solid ${accent ?? C.cyber}`,
        ...style,
      }}
    >
      <div style={{ ...sectionTitle, color: accent ?? C.cyber }}>{title}</div>
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
 * 提示词编辑模态。卡内的 textarea 只是预览：Steam 游戏模式下原生 textarea
 * 拿焦点不弹虚拟键盘，所以按 A 弹这个模态，用 ImeTextArea（Steam 的 TextArea）
 * 做真正的多行编辑——它注册了虚拟键盘，聚焦即弹键盘，固定高度 + 内部滚动。
 * 确认才写回外层。
 */
function PromptEditModal({
  initial,
  onChange,
  onDone,
  onCancel,
}: {
  initial: string;
  onChange: (v: string) => void;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  return (
    <div
      style={{
        // 模态在 Deck 1280 宽屏上原来固定 720，两侧留白明显、看着没拉满；
        // 改成按视口算宽，同时留 64px 安全边（Steam 的模态外框不吃满整屏），
        // 窄屏也不会溢出
        width: "min(880px, calc(100vw - 64px))",
        maxWidth: "100%",
        boxSizing: "border-box",
        padding: 16,
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
    >
      <div style={{ fontSize: 15, fontWeight: 700, color: "#fff" }}>编辑提示词</div>
      <ImeTextArea
        value={draft}
        focusOnMount
        onValueChange={setDraft}
        // 固定高度 + 内部滚动：多行提示词不会把模态撑破，也不会被裁掉
        nMinHeight={190}
        nMaxHeight={190}
        spellCheck={false}
        style={{
          width: "100%",
          height: 190,
          minHeight: 190,
          maxHeight: 190,
          boxSizing: "border-box",
          padding: "8px 10px",
          resize: "none",
          overflowY: "auto",
          borderRadius: 3,
          border: "1px solid rgba(90,169,230,.22)",
          borderLeft: "2px solid #5AA9E6",
          background: "rgba(14,20,28,.92)",
          color: TEXT,
          fontSize: 14,
          lineHeight: 1.5,
          fontFamily: "inherit",
          outline: "none",
        }}
      />
      <div
        style={{
          display: "flex",
          gap: 10,
          width: "100%",
          boxSizing: "border-box",
          // 两个按钮同宽同高、右对齐，和上面的文本框共用同一条右边线；
          // 原来一个 96 一个 96 套原生 DialogButton，Steam 自带 min-width
          // 会把窄盒子撑开，视觉上就错位了
          justifyContent: "flex-end",
          alignItems: "stretch",
        }}
      >
        <div style={{ flex: "0 0 132px", maxWidth: 132 }}>
          <ActionButton onClick={onCancel} description="取消" style={{ height: 40, minHeight: 40 }}>
            取消
          </ActionButton>
        </div>
        <div style={{ flex: "0 0 132px", maxWidth: 132 }}>
          <ActionButton
            primary
            description="保存"
            style={{ height: 40, minHeight: 40 }}
            onClick={() => {
              onChange(draft);
              onDone();
            }}
          >
            保存
          </ActionButton>
        </div>
      </div>
    </div>
  );
}

/**
 * 提示词卡片：卡内 textarea 只做全文预览（只读、撑满卡片、内部滚动），
 * 真正编辑走 PromptEditModal。不要把可编辑输入直接放在卡里——原生 textarea
 * 在游戏模式下拿焦点不弹虚拟键盘。
 */
function PromptEditor({
  value,
  onChange,
}: {
  value: string;
  onChange: (v: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null);
  return (
    // flex:1 + minHeight:0 让它吃掉卡片剩余高度：写死 130 时卡片一矮，
    // textarea 就顶出卡片底部，最后一行被切掉半截
    <Focusable
      onOKActionDescription="编辑"
      onActivate={() => {
        // Steam 游戏模式下原生 textarea 拿到 DOM 焦点不会弹虚拟键盘。
        // 所以按 A 弹模态，模态里用 ImeTextArea（注册了虚拟键盘的多行框）
        const close = showModal(
          <PromptEditModal
            initial={value}
            onChange={onChange}
            onDone={() => close.Close()}
            onCancel={() => close.Close()}
          />
        );
      }}
      // Focusable 自带上下内边距（约各 8px），压到 0 让 textarea 撑满
      style={{ width: "100%", minWidth: 0, flex: 1, minHeight: 0, display: "flex", padding: 0 }}
    >
      <textarea
        ref={ref}
        value={value}
        readOnly
        spellCheck={false}
        style={{
          width: "100%",
          height: "100%",
          minHeight: 0,
          resize: "none",
          overflowY: "auto",
          boxSizing: "border-box",
          // 上下留白和模态里一致，首行文字不会贴着上边框，光标/行高也对得齐
          padding: "8px 10px",
          borderRadius: 3,
          border: "1px solid rgba(90,169,230,.22)",
          borderLeft: "2px solid #5AA9E6",
          background: "rgba(14,20,28,.92)",
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

// 页签记忆：截图要收 QAM/导航，Decky 路由返回时 BigScreen 会整个重挂载，
// useState 初值归零就跳回询问页。用模块级变量跨挂载记住上次页签
let lastTab = 0;

export default function BigScreen() {
  const s = useSession();
  // 聊天区的滚动容器：追加消息后自动贴底（chatLog 里的 ChatLog 挂这个 ref）
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [tab, setTabState] = useState(lastTab);
  const setTab = (t: number | ((prev: number) => number)) => {
    setTabState((prev) => {
      const next = typeof t === "function" ? t(prev) : t;
      lastTab = next;
      return next;
    });
  };
  const [input, setInput] = useState("");
  // 端口草稿与提示：三处设置页共用 [server].port，改完需重启后端
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
  // 语音结果追加到已有输入后面，不覆盖用户已经打的字
  const voice = useVoiceInput((t) =>
    setInput((prev) => (prev.trim() ? prev.replace(/\s*$/, " ") + t : t))
  );

  // 大屏上最新一条应该在最下面；切页签后消息区会重新挂载，也要贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.items, s.streaming, tab]);

  // 进设置页拉模型列表；配对信息每 2 秒刷新（PIN 倒计时 / 新设备提示）
  useEffect(() => {
    if (tab !== 2) return;
    void s.loadModels();
    void s.loadPairInfo();
    const timer = setInterval(() => void s.loadPairInfo(), 2000);
    return () => clearInterval(timer);
  }, [tab]);

  // 读不到后端配置 / 令牌：显示明确原因，不再弹「需要访问令牌」
  const configError = s.status?.config_error || "";
  const ready = !!s.status && !configError;
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

  // 模型左右切换器：在列表里循环步进换模型
  const modelList = s.modelList.length ? s.modelList : s.model ? [s.model] : [];
  const stepModel = (dir: 1 | -1) => {
    if (!s.status?.core_ok || modelList.length < 2) return;
    const idx = modelList.indexOf(s.model);
    const next = modelList[((idx < 0 ? 0 : idx + dir) + modelList.length) % modelList.length];
    if (next !== s.model) void s.changeModel(next);
  };

  const submit = () => {
    const text = input.trim();
    if (!text || s.busy) return;
    setInput("");
    void s.send(text);
  };

  // 语音提示只在不就绪时出现：报错优先，其次按原因给一句短提示 + 下载入口
  const voiceTip = (() => {
    if (voice.ready) return null;
    // 下载中由下面的进度行负责，不再叠一条文字
    if (voice.downloading) return null;
    // 下载失败：按类别给具体原因，并允许直接重试
    if (voice.downloadError) return { text: voice.downloadError, color: ERR_COLOR, canDownload: true };
    if (voice.error) return { text: voice.error, color: ERR_COLOR, canDownload: false };
    const st = voice.status;
    // 插件桥报错时 status 就是空的：这也要说出来，
    // 否则按钮只是灰着，用户不知道在等什么
    if (!st) return { text: "语音状态读取中…", color: DIM, canDownload: false };
    if (!st.ok) return { text: "语音不可用：后端没连上", color: DIM, canDownload: false };
    if (st.enable === false) return { text: "语音输入已在设置里关闭", color: DIM, canDownload: false };
    if (!st.runtime) return { text: "语音不可用：后端没装 sherpa-onnx", color: DIM, canDownload: false };
    if (!st.model) return { text: "语音模型未下载（约 1GB，只需一次）", color: DIM, canDownload: true };
    if (st.record === false) return { text: st.record_reason || "录音设备不可用", color: DIM, canDownload: false };
    return null;
  })();

  const dl = voice.download;
  const dlActive = !!voice.downloading;
  const dlPercent = Math.max(0, Math.min(100, dl?.percent ?? 0));
  const dlPhaseText = dl?.state === "extracting" ? "解压中" : "下载中";
  const dlSizeText = (() => {
    const done = dl?.done ?? 0;
    const total = dl?.total ?? -1;
    const mb = (n: number) => `${(n / 1024 / 1024).toFixed(1)}MB`;
    return total > 0 ? `${mb(done)} / ${mb(total)}` : mb(done);
  })();

  // 询问页两个动作按钮的忙碌态：被点的那个留在原位显示「思考中」，
  // 另一个原地换成「中断」，焦点不会因为按钮消失而跳走。
  const askRunning = s.busy && s.runningAction !== "translate";
  const translateRunning = s.busy && s.runningAction === "translate";

  // 核心异常提示已并入底部回执卡片（coreAlert 大红条已移除）

  const chatLog = () => (
    <ChatLog
      items={s.items}
      streaming={s.streaming}
      // 只有聊天页自己发起的那轮才显示「思考中」占位；
      // 询问页发起的（ask/translate）在这个页签下不显示
      busy={s.busy && s.runningAction === "send"}
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
      {...(s.busy && (tab === 0 || s.runningAction === "send")
        ? { onMenuActionDescription: "中断" }
        : {})}
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
        // 聊天页按住 R2 说话：只在这一页、且语音可用时接管，
        // 其它页签的扳机键保持原样（扳机在别处通常是滚轮/加速之类的默认绑定）
        if (detail.button === GamepadButton.TRIGGER_RIGHT && tab === 1 && voice.ready) {
          if (voice.phase === "idle" && !s.busy) void voice.start();
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
      onButtonUp={(evt) => {
        const detail = evt?.detail;
        if (!detail) return;
        // 松开 R2 结束录音；这里不看 tab：按下时还在聊天页，松手前切走了也要收尾，
        // 否则麦克风会一直开着
        if (detail.button === GamepadButton.TRIGGER_RIGHT) {
          void voice.stop();
        }
      }}
      style={{
        // 路由容器从视口左上角开始铺，且不给确定高度，所以不能靠它排版。
        // 直接固定到视口上，再用内边距把 Steam 的顶部状态栏、底部按键条让出来：
        // 实测状态栏底边在 y≈65、按键条顶边在 y≈750，但页头还能再上提一点，
        // 顶部只让 44、底部让 46，把省下来的近 25px 分给三个页签的内容区。
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        zIndex: 10,
        width: "100%",
        boxSizing: "border-box",
        padding: `${PAGE_TOP}px 14px ${PAGE_BOTTOM}px`,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        background: "#1b1f26",
        color: TEXT,
        overflow: "hidden",
      }}
    >
      <header
        style={{
          width: "100%",
          margin: "0 auto",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 16,
          minWidth: 0,
          // 页头永不被压扁（消息区 flex:1 会抢空间）
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
        {ready && (
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

      {/* 询问页的状态行已移入页面容器内（与聊天页输入行同一层级），
          避免两页因容器层数不同导致分隔线下移 */}

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
      ) : configError ? (
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <div style={{ ...cardStyle, width: "min(560px, 100%)", alignItems: "stretch" }}>
            <div style={{ fontSize: 16, fontWeight: 700, color: "#fff" }}>读不到后端配置</div>
            <div style={{ color: DIM, fontSize: 13, lineHeight: 1.5 }}>
              读不到后端配置：{configError}
            </div>
            <div style={{ color: DIM, fontSize: 13, lineHeight: 1.5 }}>
              检查数据目录里的 config.toml（通常在 userdata/），确认能读到后重试。
            </div>
            <ActionButton onClick={() => void s.refreshStatus()} primary description="重试">
              重试连接
            </ActionButton>
          </div>
        </div>
      ) : tab === 0 ? (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
              margin: "0 auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {/* 动作按钮已移到状态行右侧，整行高度让给消息区；
              核心停止/异常提示并入底部回执卡片，不再弹红色大条 */}

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

          {/* 状态行：回执卡 + 截图询问/截图翻译按钮。
              与聊天页输入行同层级、同高 46px，分隔线位置才能完全一致 */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              width: "100%",
              height: ACTION_H,
              flexShrink: 0,
            }}
          >
            {/* 回执卡：核心停止/异常时整卡转红并带「启动核心」按钮 */}
            <div
              style={{
                flex: 1,
                minWidth: 0,
                height: ACTION_H,
                display: "flex",
                alignItems: "center",
                gap: 8,
                padding: "0 10px",
                borderRadius: 4,
                boxSizing: "border-box",
                background:
                  s.status && !s.status.core_ok ? "rgba(224,108,117,.10)" : "rgba(255,255,255,.05)",
                border:
                  s.status && !s.status.core_ok
                    ? "1px solid rgba(224,108,117,.30)"
                    : "1px solid rgba(90,169,230,.14)",
                borderLeft: `2px solid ${s.status && !s.status.core_ok ? "#e06c75" : "#5AA9E6"}`,
                overflow: "hidden",
              }}
            >
              <span
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: 12.5,
                  lineHeight: 1.4,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  color: s.status && !s.status.core_ok ? ERR_COLOR : TEXT,
                }}
              >
                {s.status && !s.status.core_ok
                  ? coreStopped
                    ? "核心已停止（不会自动拉起，点「启动核心」恢复）"
                    : "核心异常（点「启动核心」恢复）"
                  : statusLineFor(s.status, s.busy)}
              </span>
              {s.status && !s.status.core_ok && (
                <ActionButton
                  onClick={() => void s.coreStart()}
                  disabled={s.starting}
                  description="启动核心"
                  compact
                  style={{
                    width: 120,
                    flex: "0 0 120px",
                    fontSize: 12,
                    background: "rgba(224,108,117,.18)",
                    borderColor: "rgba(224,108,117,.45)",
                    color: "#f0c3c7",
                  }}
                >
                  {s.starting ? "启动中…" : "启动核心"}
                </ActionButton>
              )}
            </div>

            {/* 四个按钮统一 138px，回执卡（flex:1）自动收缩匹配 */}
            <div style={{ display: "flex", gap: 8, flexShrink: 0 }}>
              {!s.busy ? (
                <ActionButton
                  primary
                  disabled={!ready}
                  onClick={() => void s.shotAsk()}
                  description="截图询问"
                  style={{ width: 138 }}
                >
                  截图询问
                </ActionButton>
              ) : askRunning ? (
                <ActionButton
                  onClick={() => {}}
                  description="思考中"
                  style={{ ...runningBtnStyle, width: 138 }}
                >
                  思考中…（{s.elapsed}s）
                </ActionButton>
              ) : (
                <ActionButton
                  onClick={() => void s.cancel()}
                  description="中断"
                  style={{ ...cancelBtnStyle, width: 138 }}
                >
                  中断
                </ActionButton>
              )}
              {!s.busy ? (
                <ActionButton
                  disabled={!ready}
                  onClick={() => void s.shotTranslate()}
                  description="截图翻译"
                  style={{ width: 138 }}
                >
                  截图翻译
                </ActionButton>
              ) : translateRunning ? (
                <ActionButton
                  onClick={() => {}}
                  description="思考中"
                  style={{ ...runningBtnStyle, width: 138 }}
                >
                  思考中…（{s.elapsed}s）
                </ActionButton>
              ) : (
                <ActionButton
                  onClick={() => void s.cancel()}
                  description="中断"
                  style={{ ...cancelBtnStyle, width: 138 }}
                >
                  中断
                </ActionButton>
              )}
            </div>
          </div>

          {/* 状态行与消息区之间：规格与聊天页输入行完全一致（同高、同 gap），分隔线零补偿 */}
          <div style={dividerStyle} />

          {chatLog()}
        </div>
      ) : tab === 1 ? (
        <div
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
              margin: "0 auto",
            display: "flex",
            flexDirection: "column",
            gap: 10,
          }}
        >
          {/* 聊天页：输入区不要卡片外框；输入框自带细边，与发送按钮同为 46px
              （与询问页状态行等高，两页分界线位置一致） */}
          <div
            style={{
              display: "flex",
              alignItems: "stretch",
              gap: 8,
              width: "100%",
              height: ACTION_H,
              flexShrink: 0,
              position: "relative",
            }}
          >
            <Focusable
              onOKActionDescription="输入"
              // @decky/ui 的 TextField 外面还包一层 div，单靠 flex 撑不满
              // （截图里输入框只有几十像素宽）；这里用 grid 强制子项铺满整行
              style={{
                flex: 1,
                minWidth: 0,
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr)",
                gridAutoFlow: "column",
                // Focusable 自带左右内边距，会吃掉输入框宽度，
                // 导致输入框比询问页状态栏短一截
                padding: 0,
              }}
            >
              <ImeTextField
                value={input}
                placeholder="问问 Meido..."
                onValueChange={setInput}
                style={{
                  width: "100%",
                  maxWidth: "100%",
                  height: ACTION_H,
                  minHeight: ACTION_H,
                  padding: "0 12px",
                  boxSizing: "border-box",
                  fontSize: 14,
                  lineHeight: "44px",
                  borderRadius: 4,
                  border: "1px solid rgba(255,255,255,.14)",
                  background: "rgba(255,255,255,.08)",
                  // 不写 color 时 Steam 主题会给输入框套黑色文字，深底上几乎看不见
                  color: TEXT,
                }}
              />
            </Focusable>
            <div style={{ flex: "0 0 138px", width: 138, height: ACTION_H }}>
              <ActionButton
                // 询问页发起思考时语音仍可用（照常说话转文字填入输入框），
                // 只有聊天页自己发起的那轮才锁语音
                disabled={(s.busy && s.runningAction === "send") || voice.phase === "working" || !voice.ready}
                onClick={() => (voice.phase === "recording" ? voice.stop() : voice.start())}
                description={
                  !voice.ready
                    ? voice.status?.model === false
                      ? "语音模型未下载"
                      : "语音输入不可用"
                    : voice.phase === "recording"
                      ? "松开结束"
                      : "语音输入（也可按住 R2）"
                }
                style={{
                  height: ACTION_H,
                  minHeight: ACTION_H,
                  // 录音中整块变强调色，手柄注意力在输入框上也能一眼看出在录
                  background: voice.phase === "recording" ? ACCENT : "rgba(255,255,255,.10)",
                  color: voice.phase === "recording" ? "#1b1b1b" : TEXT,
                }}
              >
                {voice.phase === "working" ? "识别中" : voice.phase === "recording" ? "松开" : "语音"}
              </ActionButton>
            </div>
            <div style={{ flex: "0 0 138px", width: 138, height: ACTION_H }}>
              {!s.busy ? (
                <ActionButton
                  primary
                  disabled={!input.trim()}
                  onClick={submit}
                  description="发送"
                  style={{ height: ACTION_H, minHeight: ACTION_H }}
                >
                  发送
                </ActionButton>
              ) : s.runningAction === "send" ? (
                <ActionButton
                  onClick={() => {}}
                  description="思考中"
                  style={{ ...runningBtnStyle, height: ACTION_H, minHeight: ACTION_H }}
                >
                  思考中…（{s.elapsed}s）
                </ActionButton>
              ) : (
                <ActionButton
                  onClick={() => void s.cancel()}
                  description="中断"
                  style={{ ...cancelBtnStyle, height: ACTION_H, minHeight: ACTION_H }}
                >
                  中断
                </ActionButton>
              )}
            </div>
            {/* 语音提示/下载进度：作为独立一行放在输入行下面，不再绝对定位压住消息区 */}
          </div>
          {(voiceTip || dlActive) && (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                width: "100%",
                minHeight: 26,
                flexShrink: 0,
              }}
            >
              {dlActive ? (
                <>
                  <span style={{ fontSize: 12, color: DIM, whiteSpace: "nowrap" }}>
                    {dlPhaseText} {dlPercent}%
                  </span>
                  <div
                    style={{
                      flex: 1,
                      minWidth: 60,
                      height: 8,
                      borderRadius: 4,
                      background: "rgba(255,255,255,.10)",
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        width: `${dlPercent}%`,
                        height: "100%",
                        borderRadius: 4,
                        background: ACCENT,
                        transition: "width .4s ease",
                      }}
                    />
                  </div>
                  <span style={{ fontSize: 11, color: DIM, whiteSpace: "nowrap" }}>{dlSizeText}</span>
                  <ActionButton
                    onClick={voice.cancelDownload}
                    description="取消下载"
                    style={{ width: "auto", height: 26, minHeight: 26, padding: "0 10px", fontSize: 12 }}
                  >
                    取消
                  </ActionButton>
                </>
              ) : voiceTip ? (
                <>
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
                </>
              ) : null}
            </div>
          )}
          {/* 输入区与消息区之间：与询问页共用同一条分隔线 */}
          <div style={dividerStyle} />

          {chatLog()}
        </div>
      ) : (
        <Focusable
          flow-children="vertical"
          style={{
            flex: 1,
            minHeight: 0,
            width: "100%",
            margin: "0 auto",
            // 6 张卡按 flex 权重分配，卡内内容各自垂直居中
            display: "flex",
            flexDirection: "column",
            gap: 6,
            boxSizing: "border-box",
            overflow: "hidden",
            // 页头与首张卡之间多让一点，和卡间间距更接近
            paddingTop: 6,
          }}
        >
          {/* 连接 */}
          <SectionCard
            title="连接"
            inlineTitle
            accent={C.cyber}
            style={{ padding: 6, flex: 0.7 }}
          >
            {/* 四分栏两行：一行 4 列读数，一屏全展开 */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
                columnGap: 14,
                rowGap: 2,
                alignContent: "center",
                minHeight: 0,
                width: "100%",
              }}
            >
              <StatRow label="女仆核心" value={coreLine} />
              <StatRow
                label="当前游戏"
                value={
                  s.status?.game_name
                    ? `${s.status.game_name}（${s.status.game_source || "?"}）`
                    : "—"
                }
              />
              <StatRow
                label="屏幕截图"
                value={s.status?.capture_ok ? "可用" : s.status?.capture_reason || "不可用"}
              />
              <StatRow
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
              <StatRow label="核心版本" value={s.status?.version || "—"} />
              <StatRow label="设备地址" value={s.status ? `${s.status.ip}:${s.status.port}` : "—"} />
              <StatRow
                label="虚拟手柄"
                value={
                  !s.status
                    ? "未知"
                    : s.status.gamepad_ok === true
                      ? "可用"
                      : s.status.gamepad_ok === false
                        ? `不可用${s.status.gamepad_reason ? `（${s.status.gamepad_reason}）` : ""}`
                        : "未知（后端未上报）"
                }
              />
              <StatRow
                label="更新检测"
                value={
                  !s.status
                    ? "检测中"
                    : s.status.update_has
                      ? `有更新${s.status.update_latest ? `（v${s.status.update_latest}）` : ""}`
                      : "当前最新"
                }
              />
            </div>
          </SectionCard>

          {/* 局域网访问：地址与令牌都从这里拿，别的设备照着填 */}
          <SectionCard
            title="局域网访问"
            inlineTitle
            accent={C.cyber}
            style={{ padding: 6, flex: 0.7 }}
          >
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 190px",
                columnGap: 10,
                alignItems: "stretch",
                minHeight: 0,
                width: "100%",
                flex: 1,
              }}
            >
              <div
                style={{
                  display: "grid",
                  gridTemplateRows: "1fr 1fr",
                  gap: 4,
                  minWidth: 0,
                  minHeight: 0,
                }}
              >
                <StatRow label="地址" value={s.status?.lan_url || "—"} />
                <StatRow label="令牌" value={groupToken(s.status?.lan_token || "") || "—"} />
              </div>
              <div
                style={{
                  display: "grid",
                  gridTemplateRows: "1fr 1fr",
                  gap: 4,
                  minWidth: 0,
                  minHeight: 0,
                }}
              >
                <ActionButton
                  onClick={() => void copyText(s.status?.lan_url || "")}
                  description="复制局域网地址"
                  compact
                >
                  复制地址
                </ActionButton>
                <ActionButton
                  onClick={() => void copyText(groupToken(s.status?.lan_token || ""))}
                  description="复制访问令牌"
                  compact
                >
                  复制令牌
                </ActionButton>
              </div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
              <span style={{ fontSize: 11, color: DIM, flexShrink: 0 }}>
                端口 {s.status?.port ?? "—"}
              </span>
              <ImeTextField
                value={portDraft}
                onValueChange={setPortDraft}
                style={{ flex: 1, minWidth: 0 }}
              />
              <ActionButton
                onClick={() => void savePortDraft()}
                description="保存端口"
                compact
                style={{ width: 96, flexShrink: 0 }}
              >
                保存端口
              </ActionButton>
              <ActionButton
                onClick={() => void s.coreRestart()}
                disabled={s.restarting}
                description="重启后端使端口生效"
                compact
                style={{ width: 96, flexShrink: 0 }}
              >
                {s.restarting ? "重启中…" : "重启后端"}
              </ActionButton>
            </div>
            {portHint ? (
              <div style={{ fontSize: 10.5, color: "#e0a33a", lineHeight: 1.3 }}>
                {portHint}
              </div>
            ) : null}
          </SectionCard>

          {/* 设备配对：PIN 只在掌机显示，允许后才接受浏览器提交的 PIN */}
          <SectionCard
            title="设备配对"
            inlineTitle
            accent={C.cyber}
            style={{ padding: 6, flex: 0.75 }}
          >
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 3,
                width: "100%",
                minHeight: 0,
                overflow: "hidden",
              }}
            >
              {s.status?.security_note ? (
                <div style={{ fontSize: 11, color: "#e0a33a" }}>{s.status.security_note}</div>
              ) : null}
              {!s.status?.lan_enable ? (
                <div style={{ fontSize: 10.5, color: DIM }}>
                  局域网访问已关闭（仅本机）；开启并重启后端后才能配对。
                </div>
              ) : null}
              {s.pairSessions.length === 0 ? (
                <div style={{ fontSize: 11, color: DIM }}>无待配对设备</div>
              ) : (
                <>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                    <span
                      style={{
                        fontSize: 20,
                        letterSpacing: 4,
                        color: TEXT,
                        fontFamily: "monospace",
                      }}
                    >
                      {s.pairSessions[0].pin}
                    </span>
                    <span
                      style={{
                        fontSize: 10.5,
                        color: DIM,
                        minWidth: 0,
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      剩余 {Math.max(0, s.pairSessions[0].expires_in)} 秒 ·{" "}
                      {s.pairSessions[0].name} · {s.pairSessions[0].ip}
                    </span>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                    <ActionButton
                      onClick={() => void s.pairDecide(s.pairSessions[0].pair_id, true)}
                      description="允许这台设备配对"
                      compact
                      style={{ width: "auto", padding: "0 10px" }}
                    >
                      允许
                    </ActionButton>
                    <ActionButton
                      onClick={() => void s.pairDecide(s.pairSessions[0].pair_id, false)}
                      description="拒绝这台设备"
                      compact
                      style={{ width: "auto", padding: "0 10px" }}
                    >
                      拒绝
                    </ActionButton>
                    <ActionButton
                      onClick={() => void s.pairRefresh(s.pairSessions[0].pair_id)}
                      description="刷新 PIN"
                      compact
                      style={{ width: "auto", padding: "0 10px" }}
                    >
                      刷新 PIN
                    </ActionButton>
                  </div>
                </>
              )}
              <div style={{ fontSize: 10.5, color: DIM }}>
                已配对 {s.pairDevices.length} 台
                {s.pairDevices.length
                  ? `：${s.pairDevices.slice(0, 3).map((d) => d.name || "未命名").join("、")}${
                      s.pairDevices.length > 3 ? " 等" : ""
                    }`
                  : ""}
              </div>
              {s.pairDevices.map((d) => (
                <div
                  key={d.device_id}
                  style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}
                >
                  <span
                    style={{
                      flex: 1,
                      fontSize: 10.5,
                      color: TEXT,
                      minWidth: 0,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {d.name || "未命名"} · {d.ip || "—"}
                  </span>
                  <ActionButton
                    onClick={() => void s.pairRemove(d.device_id)}
                    description="移除这台设备"
                    compact
                    style={{ width: "auto", padding: "0 8px", minHeight: 22, height: 22 }}
                  >
                    移除
                  </ActionButton>
                </div>
              ))}
              {s.pairDevices.length > 0 ? (
                <ActionButton
                  onClick={() => void s.pairRemoveAll()}
                  description="移除全部已配对设备"
                  compact
                  style={{ width: "auto", padding: "0 10px", minHeight: 22, height: 22 }}
                >
                  移除全部设备
                </ActionButton>
              ) : null}
              {s.pairError ? (
                <div style={{ fontSize: 10.5, color: ERR_COLOR }}>{s.pairError}</div>
              ) : null}
            </div>
          </SectionCard>

          {/* 模型 */}
          <SectionCard
            title="模型"
            inlineTitle
            accent={C.model}
            style={{ padding: 6, flex: 0.7 }}
          >
            {/* 左侧模型下拉拉满剩余宽度，右栏固定 190px（与截屏卡按钮列等宽） */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 190px",
                columnGap: 14,
                // 右栏三个按钮按卡片剩余高度均分，不用固定像素叠高：
                // 卡片矮时超出的部分原本会被居中裁到卡片外（刷新模型掉出去）
                alignItems: "stretch",
                minHeight: 0,
                width: "100%",
                flex: 1,
              }}
            >
              {/* 左右切换器：‹ 模型名 ›，循环步进 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "36px minmax(0, 1fr) 36px",
                  gap: 6,
                  alignItems: "center",
                  minWidth: 0,
                  minHeight: 0,
                  width: "100%",
                }}
              >
                <ActionButton
                  onClick={() => stepModel(-1)}
                  disabled={!s.status?.core_ok || modelList.length < 2}
                  description="上一个模型"
                  compact
                  style={{ fontSize: 18, padding: 0 }}
                >
                  ‹
                </ActionButton>
                <div
                  style={{
                    minWidth: 0,
                    minHeight: 30,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    padding: "0 8px",
                    borderRadius: 3,
                    border: "1px solid rgba(90,169,230,.22)",
                    borderLeft: "2px solid #5AA9E6",
                    background: "rgba(14,20,28,.92)",
                    color: TEXT,
                    fontSize: 13,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                  title={s.model}
                >
                  {s.loadingModels ? "加载中…" : s.model || "未选择"}
                </div>
                <ActionButton
                  onClick={() => stepModel(1)}
                  disabled={!s.status?.core_ok || modelList.length < 2}
                  description="下一个模型"
                  compact
                  style={{ fontSize: 18, padding: 0 }}
                >
                  ›
                </ActionButton>
              </div>
              {/* 右栏两行 1fr 1fr 均分卡高：第一行两个开关，第二行刷新模型 */}
              <div
                style={{
                  display: "grid",
                  gridTemplateRows: "repeat(2, minmax(0, 1fr))",
                  gap: 4,
                  minWidth: 0,
                  minHeight: 0,
                }}
              >
                {/* 三个开关并排；小字号 + 紧凑内边距防出界 */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                    gap: 4,
                    minWidth: 0,
                    minHeight: 0,
                  }}
                >
                  <ActionButton
                    onClick={() => void s.toggleStt(!s.stt)}
                    description="切换语音输入"
                    compact
                    style={{
                      // 开：蓝色（和 primary 按钮同色系）；关：灰
                      background: s.stt ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
                      border: `1px solid ${s.stt ? "rgba(90,169,230,.55)" : "rgba(255,255,255,.10)"}`,
                      color: s.stt ? "#eaf2fb" : DIM,
                      fontSize: 12,
                      paddingLeft: 4,
                      paddingRight: 4,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      // 撑满所在行高（compact 的固定 22px 会留出空档）
                      minHeight: 0,
                      height: "100%",
                    }}
                  >
                    语音输入
                  </ActionButton>
                  <ActionButton
                    onClick={() => void s.toggleSttDuck(!s.sttDuck)}
                    description={
                      s.sttDuckAvailable
                        ? "录音时静音外放，录完自动恢复"
                        : "这台机器没有音频控制工具，开关只保存不生效"
                    }
                    compact
                    style={{
                      background: s.sttDuck ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
                      border: `1px solid ${s.sttDuck ? "rgba(90,169,230,.55)" : "rgba(255,255,255,.10)"}`,
                      color: s.sttDuck ? "#eaf2fb" : DIM,
                      fontSize: 12,
                      paddingLeft: 4,
                      paddingRight: 4,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      minHeight: 0,
                      height: "100%",
                    }}
                  >
                    静音外放
                  </ActionButton>
                  <ActionButton
                    onClick={() => void s.toggleWithShot(!s.withShot)}
                    description="切换带图发送"
                    compact
                    style={{
                      background: s.withShot ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
                      border: `1px solid ${s.withShot ? "rgba(90,169,230,.55)" : "rgba(255,255,255,.10)"}`,
                      color: s.withShot ? "#eaf2fb" : DIM,
                      fontSize: 12,
                      paddingLeft: 4,
                      paddingRight: 4,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      minHeight: 0,
                      height: "100%",
                    }}
                  >
                    带图发送
                  </ActionButton>
                </div>
                <ActionButton
                  onClick={() => void s.loadModels()}
                  disabled={s.loadingModels}
                  description="刷新"
                  compact
                  style={{
                    fontSize: 12,
                    paddingLeft: 4,
                    paddingRight: 4,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    minHeight: 0,
                    height: "100%",
                  }}
                >
                  {s.loadingModels ? "拉取中…" : "刷新模型"}
                </ActionButton>
              </div>
            </div>
          </SectionCard>

          {/* 截屏询问 */}
          <SectionCard
            title="截屏询问"
            inlineTitle
            accent={C.prompt}
            style={{ padding: 6, flex: 1.5 }}
          >
            {/* 左 4/5 放提示词框，右 1/5 竖排两个按钮（宽度减半给文本框让路） */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 190px",
                columnGap: 10,
                alignItems: "stretch",
                minHeight: 0,
                width: "100%",
                // 关键：撑满卡片剩余高度，textarea 才会跟着撑高
                flex: 1,
              }}
            >
              <PromptEditor value={s.askTplEdit} onChange={s.setAskTplEdit} />
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                  // 上下均分撑满整卡高度（按钮各自 flex:1）
                }}
              >
                <ActionButton
                  onClick={() => void s.saveAskTpl()}
                  description="保存"
                  compact
                  style={{ flex: 1, minHeight: 0 }}
                >
                  保存
                </ActionButton>
                <ActionButton
                  onClick={() => s.setAskTplEdit(s.askTplDefault)}
                  description="恢复默认"
                  compact
                  style={{ flex: 1, minHeight: 0 }}
                >
                  恢复默认
                </ActionButton>
              </div>
            </div>
          </SectionCard>

          {/* 截图翻译 */}
          <SectionCard
            title="截图翻译"
            inlineTitle
            accent={C.prompt}
            style={{ padding: 6, flex: 1.5 }}
          >
            {/* 同上：左 4/5 文本框，右 1/5 按钮 */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 190px",
                columnGap: 10,
                alignItems: "stretch",
                minHeight: 0,
                width: "100%",
                // 关键：撑满卡片剩余高度，textarea 才会跟着撑高
                flex: 1,
              }}
            >
              <PromptEditor value={s.translateTplEdit} onChange={s.setTranslateTplEdit} />
              <div
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 4,
                  // 上下均分撑满整卡高度（按钮各自 flex:1）
                }}
              >
                <ActionButton
                  onClick={() => void s.saveTranslateTpl()}
                  description="保存"
                  compact
                  style={{ flex: 1, minHeight: 0 }}
                >
                  保存
                </ActionButton>
                <ActionButton
                  onClick={() => s.setTranslateTplEdit(s.translateTplDefault)}
                  description="恢复默认"
                  compact
                  style={{ flex: 1, minHeight: 0 }}
                >
                  恢复默认
                </ActionButton>
              </div>
            </div>
          </SectionCard>

          {/* 维护 */}
          <SectionCard
            title="维护"
            inlineTitle
            accent={C.ops}
            style={{ padding: 6, flex: 0.8 }}
          >
            {/* 左栏拉满剩余宽度；右栏固定 190px 与"保存"按钮等宽 */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 190px",
                columnGap: 10,
                // 两栏等高撑满卡片，内部各行上下均分对齐
                alignItems: "stretch",
                minHeight: 0,
                width: "100%",
                flex: 1,
              }}
            >
                {/* 左栏两行用 1fr 1fr 精确均分（flex:1 在 grid item 上不可靠） */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateRows: "1fr 1fr",
                    gap: 4,
                    minWidth: 0,
                    minHeight: 0,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: 10,
                      minWidth: 0,
                      minHeight: 0,
                      padding: "4px 10px",
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
                  </div>

                  {/* 核心三按钮横排：grid item 自动占满 1fr 行高 */}
                  <div
                    style={{
                      display: "grid",
                      gridTemplateColumns: "repeat(3, minmax(0, 1fr))",
                      gap: 6,
                      minWidth: 0,
                      minHeight: 0,
                      alignContent: "stretch",
                    }}
                  >
                    <ActionButton
                      onClick={() => void s.coreStart()}
                      disabled={coreRunning || s.starting || s.restarting || s.killing}
                      primary={!coreRunning}
                      description="启动核心"
                      compact
                    >
                      {s.starting ? "核心启动中…" : "启动核心"}
                    </ActionButton>
                    <ActionButton
                      onClick={() => void s.coreRestart()}
                      disabled={!coreRunning || s.starting || s.restarting || s.killing}
                      description="重启核心"
                      compact
                    >
                      {s.restarting ? "核心重启中…" : "重启核心"}
                    </ActionButton>
                    <ActionButton
                      onClick={() => void s.coreKill()}
                      disabled={!coreRunning || s.starting || s.restarting || s.killing}
                      description="关闭核心"
                      compact
                    >
                      {s.killing ? "核心关闭中…" : "关闭核心"}
                    </ActionButton>
                  </div>
                </div>

                {/* 右 1/3：新会话、返回游戏两行 1fr 1fr 精确均分 */}
                <div
                  style={{
                    display: "grid",
                    gridTemplateRows: "1fr 1fr",
                    gap: 4,
                    minWidth: 0,
                    minHeight: 0,
                  }}
                >
                  <ActionButton
                    onClick={() => void s.newSession()}
                    disabled={s.busy || s.newing}
                    description="新会话"
                    compact
                    style={{ minHeight: 0 }}
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
                    compact
                    style={{ minHeight: 0 }}
                  >
                    返回游戏
                  </ActionButton>
                </div>
            </div>
          </SectionCard>
        </Focusable>
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
