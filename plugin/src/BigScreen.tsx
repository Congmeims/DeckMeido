import { useEffect, useRef, useState } from "react";
import {
  DialogButton,
  DropdownItem,
  Focusable,
  GamepadButton,
  TextField,
  ToggleField,
} from "@decky/ui";
import { ACCENT, DIM, ERR_COLOR, Md, TEXT } from "./ui";
import { DEFAULT_ASK_TPL, useSession } from "./useSession";

const MAX_W = 1120;
const PAGE_TOP = 48;
const PAGE_BOTTOM = 44;

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

const sectionTitle: React.CSSProperties = {
  fontSize: 13,
  letterSpacing: 1,
  textTransform: "uppercase",
  color: ACCENT,
  fontWeight: 700,
};

const tabStyle = (active: boolean): React.CSSProperties => ({
  flex: "1 1 0",
  minWidth: 0,
  textAlign: "center",
  padding: "10px 0",
  fontSize: 15,
  borderRadius: 4,
  background: active ? "rgba(90,169,230,.28)" : "rgba(255,255,255,.06)",
  color: active ? "#eaf2fb" : DIM,
});

function ActionButton({
  children,
  onClick,
  disabled,
  primary,
  description,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
  description?: React.ReactNode;
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
      }}
    >
      {children}
    </DialogButton>
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
        lineHeight: 1.5,
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
          maxWidth: "88%",
          minWidth: 0,
          padding: "12px 16px",
          borderRadius: 4,
          background: mine ? "rgba(90,169,230,.16)" : "rgba(255,255,255,.06)",
          borderLeft: mine ? "3px solid transparent" : `3px solid ${ACCENT}`,
          borderRight: mine ? `3px solid ${ACCENT}` : "3px solid transparent",
          boxSizing: "border-box",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          overflowWrap: "anywhere",
        }}
      >
        {image && (
          <img
            src={image}
            style={{
              display: "block",
              maxWidth: "100%",
              maxHeight: 360,
              objectFit: "contain",
              marginBottom: 10,
              borderRadius: 4,
            }}
          />
        )}
        <Md text={text} align={mine ? "right" : "left"} size={15.5} />
        {streaming && <span style={{ color: DIM }}>▍</span>}
      </div>
    </div>
  );
}

function SectionCard({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div style={cardStyle}>
      <div style={sectionTitle}>{title}</div>
      {children}
    </div>
  );
}

export default function BigScreen() {
  const s = useSession();
  const [tab, setTab] = useState(0);
  const [input, setInput] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 大屏上最新一条应该在最下面，输入后自动贴底
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [s.items, s.streaming]);

  // 进设置页再拉模型列表，和 QAM 的行为一致
  useEffect(() => {
    if (tab === 1 && s.ticket) void s.loadModels();
  }, [tab, s.ticket]);

  const locked = !!s.status && !s.status.unlocked;
  const ready = !!s.status && s.status.unlocked;
  const coreLine = s.status
    ? s.status.core_ok
      ? "核心正常"
      : "核心异常"
    : s.probing
      ? "连接中"
      : "未连上";

  const submit = () => {
    const text = input.trim();
    if (!text || s.busy) return;
    setInput("");
    void s.send(text);
  };

  return (
    <Focusable
      noFocusRing
      flow-children="vertical"
      onCancelActionDescription="返回"
      onButtonDown={(evt) => {
        const detail = evt?.detail;
        if (!detail || detail.is_repeat) return;
        if (detail.button === GamepadButton.BUMPER_LEFT) {
          setTab((t) => Math.max(0, t - 1));
        } else if (detail.button === GamepadButton.BUMPER_RIGHT) {
          setTab((t) => Math.min(1, t + 1));
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
        <div style={{ fontSize: 22, fontWeight: 700, color: "#fff", letterSpacing: 0.4 }}>
          DeckMeido
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            minWidth: 0,
            overflow: "hidden",
            justifyContent: "flex-end",
          }}
        >
          <StatusChip color={s.status?.core_ok ? ACCENT : ERR_COLOR}>{coreLine}</StatusChip>
          <StatusChip>{s.status ? `${s.status.ip}:${s.status.port}` : "—"}</StatusChip>
          {s.status?.game_name && <StatusChip>{s.status.game_name}</StatusChip>}
        </div>
      </header>

      {/* 忙碌时给一条过程提示，别让用户盯着静止页面 */}
      {(s.busy || s.activity) && (
        <div
          style={{
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            minWidth: 0,
            color: DIM,
            fontSize: 12.5,
            lineHeight: 1.4,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
            flexShrink: 0,
          }}
        >
          {s.activity || `思考中…（${s.elapsed}s）`}
        </div>
      )}

      {!locked && (
        <div
          style={{
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            display: "flex",
            gap: 8,
            flexShrink: 0,
          }}
        >
          {["对话", "设置"].map((name, i) => (
            <Focusable
              key={name}
              autoFocus={i === 0}
              onActivate={() => setTab(i)}
              onOKActionDescription="切换"
              onCancelActionDescription="返回"
              style={tabStyle(tab === i)}
            >
              {name}
            </Focusable>
          ))}
        </div>
      )}

      {s.error && (
        <div
          style={{
            width: "100%",
            maxWidth: MAX_W,
            margin: "0 auto",
            padding: "10px 14px",
            borderRadius: 4,
            background: "rgba(224,108,117,.10)",
            color: ERR_COLOR,
            fontSize: 13,
            lineHeight: 1.45,
            boxSizing: "border-box",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            flexShrink: 0,
          }}
        >
          {s.error}
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
            <ActionButton onClick={() => void s.refreshStatus()} primary description="重试">
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
            gap: 12,
          }}
        >
          {s.status && !s.status.core_ok && (
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
              <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {s.status.core_reason || "女仆核心没有连上"}
              </span>
              <div style={{ width: 160, flex: "0 0 160px" }}>
                <ActionButton
                  onClick={() => void s.startBackend()}
                  disabled={s.starting}
                  description="启动"
                >
                  {s.starting ? "启动中…" : "启动后端"}
                </ActionButton>
              </div>
            </div>
          )}

          {/* 四个入口等宽，左右对称；全部是 Steam 可聚焦按钮 */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
              gap: 10,
              width: "100%",
              flexShrink: 0,
            }}
          >
            <ActionButton
              primary
              disabled={s.busy || !ready}
              onClick={() => void s.shotAsk()}
              description="截图询问"
            >
              {s.busy ? `思考中…（${s.elapsed}s）` : "截图询问"}
            </ActionButton>
            <ActionButton
              disabled={s.busy || !ready}
              onClick={() => void s.shotTranslate()}
              description="截图翻译"
            >
              截图翻译
            </ActionButton>
            <ActionButton
              disabled={s.busy || s.newing || !ready}
              onClick={() => void s.newSession()}
              description="新会话"
            >
              {s.newing ? "新建中…" : "新会话"}
            </ActionButton>
            <ActionButton
              disabled={!s.busy}
              onClick={() => void s.cancel()}
              description="中断"
            >
              中断
            </ActionButton>
          </div>

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

          <Focusable
            ref={scrollRef}
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
            <div style={{ display: "flex", flexDirection: "column", gap: 10, width: "100%" }}>
              {s.items.map((m, i) => (
                <MessageBubble
                  key={`${m.ts}-${i}`}
                  mine={m.role === "user"}
                  text={m.text}
                  image={m.image}
                />
              ))}
              {s.streaming && <MessageBubble text={s.streaming} streaming />}
              {s.busy && !s.streaming && (
                <MessageBubble text={`思考中…（${s.elapsed}s）`} />
              )}
              {!s.items.length && !s.streaming && !s.busy && (
                <div style={{ color: DIM, textAlign: "center", fontSize: 14, padding: "32px 0" }}>
                  还没有对话，先截个图或直接问吧
                </div>
              )}
            </div>
          </Focusable>

          <div
            style={{
              ...cardStyle,
              flexShrink: 0,
              gap: 10,
            }}
          >
            <Focusable
              onOKActionDescription="输入"
              style={{ width: "100%", minWidth: 0 }}
            >
              <TextField
                value={input}
                placeholder="问问 Meido..."
                onChange={(e) => setInput(e.target.value)}
                style={{ width: "100%" }}
              />
            </Focusable>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) 200px",
                gap: 10,
                alignItems: "center",
                width: "100%",
              }}
            >
              <ToggleField
                label="带画面"
                description="问的时候附带当前游戏画面"
                checked={s.withShot}
                onChange={s.setWithShot}
              />
              <ActionButton
                primary
                disabled={s.busy || !input.trim()}
                onClick={submit}
                description="发送"
              >
                发送
              </ActionButton>
            </div>
          </div>
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
            gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
            gap: 16,
            alignItems: "start",
            overflowY: "auto",
            boxSizing: "border-box",
          }}
        >
          <SectionCard title="连接">
            <StatRow label="核心" value={coreLine} />
            <StatRow label="地址" value={s.status ? `${s.status.ip}:${s.status.port}` : "—"} />
            <StatRow label="截图" value={s.status?.capture_ok ? "可用" : s.status?.capture_reason || "不可用"} wrap />
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
            {s.status && !s.status.core_ok && (
              <ActionButton
                onClick={() => void s.startBackend()}
                disabled={s.starting}
                primary
                description="启动"
              >
                {s.starting ? "启动中…" : "启动后端"}
              </ActionButton>
            )}
            <ActionButton
              onClick={() => void s.restartCore()}
              disabled={s.restarting}
              description="重启"
            >
              {s.restarting ? "重启中…" : "重启核心"}
            </ActionButton>
          </SectionCard>

          <SectionCard title="对话">
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
            <ToggleField
              label="语音播报"
              description="回复生成完在本机念一遍"
              checked={s.tts}
              onChange={(v) => void s.toggleTts(v)}
            />
            <div style={{ color: DIM, fontSize: 12.5, lineHeight: 1.45 }}>
              截屏询问的提示词
            </div>
            <Focusable onOKActionDescription="输入" style={{ width: "100%", minWidth: 0 }}>
              <TextField
                value={s.askTplEdit}
                onChange={(e) => s.setAskTplEdit(e.target.value)}
                style={{ width: "100%" }}
              />
            </Focusable>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)",
                gap: 10,
                width: "100%",
              }}
            >
              <ActionButton onClick={() => void s.saveAskTpl()} description="保存">
                保存
              </ActionButton>
              <ActionButton
                onClick={() => s.setAskTplEdit(DEFAULT_ASK_TPL)}
                description="恢复默认"
              >
                恢复默认
              </ActionButton>
            </div>
            <ActionButton
              onClick={() => void s.newSession()}
              disabled={s.busy || s.newing}
              description="新会话"
            >
              {s.newing ? "新建中…" : "新会话"}
            </ActionButton>
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
