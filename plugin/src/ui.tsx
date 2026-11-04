import type {
  ChangeEvent,
  ComponentProps,
  CompositionEvent,
  CSSProperties,
  KeyboardEvent,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";
import { useEffect, useRef } from "react";
import { CommonUIModule, TextField } from "@decky/ui";

// 全插件共用的颜色和卡片样式，QAM 与大屏页都从这里取，避免两边各抄一套。
export const ACCENT = "#5aa9e6";
export const DIM = "#8b929a";
export const ERR_COLOR = "#e06c75";
export const TEXT = "#dcdedf";

export const CARD: CSSProperties = {
  padding: "8px 10px",
  marginBottom: 6,
  borderRadius: 4,
  fontSize: 12,
  lineHeight: 1.45,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
  overflowWrap: "anywhere",
};

/**
 * 并排小按钮的样式：QAM 里两个 ButtonItem 塞进同一 flex 行会互相压住
 * （ButtonItem 外层固定 100% 宽、文字居中溢出），所以并排场景改用
 * 普通 div 仿按钮，宽高、圆角、底色都对着 GamepadUI 的按钮取值。
 * 大屏页不再使用它，改由 DialogButton 承担。
 */
export const FAKE_BTN: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  minHeight: 40,
  padding: "0 14px",
  borderRadius: 2,
  background: "rgba(255,255,255,.10)",
  color: TEXT,
  fontSize: 14,
  whiteSpace: "nowrap",
  boxSizing: "border-box",
  cursor: "pointer",
};

type ImeTextFieldProps = Omit<ComponentProps<typeof TextField>, "value" | "onChange"> & {
  value: string;
  onValueChange: (v: string) => void;
};

// @decky/ui 的 TextField 运行时是 Steam 的类组件（内部渲染原生 input），
// 实例上有 element 可拿到 DOM 节点；声明文件只写了 FC，这里放宽类型以便同步外部改值。
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const SteamTextField: any = TextField;

/**
 * 中文输入法安全的 TextField（QAM 和大屏页共用）。
 *
 * Steam 游戏模式下输入法组字时，原来的写法把每次 onChange 的 e.target.value
 * 直接写回 React 状态，再用受控 value 回灌 DOM：组字文本被当成最终值参与重渲染，
 * CEF 的组字状态被打断，选候选字时按下的数字键就落成了普通文本。
 *
 * 这里改成组字期间完全由 DOM/输入法维护，React 只在非组字时同步：
 * - defaultValue 起步，组字期间不把组字文本传出去，也不写 DOM；
 * - compositionend 时读取 DOM 的最终文本，一次性回写；
 * - 外部改值（发送后清空、语音转写追加）只在非组字时同步；
 * - 组字期间（isComposing 或 keyCode 229）不处理任何按键。
 */
export function ImeTextField({
  value,
  onValueChange,
  onCompositionStart,
  onCompositionEnd,
  onKeyDown,
  ...rest
}: ImeTextFieldProps) {
  const composing = useRef(false);
  const synced = useRef(value); // 已同步进 DOM 的外部值
  const emitted = useRef(value); // 最后一次交给外层的值
  const field = useRef<{ element?: HTMLInputElement | null } | null>(null);

  useEffect(() => {
    if (value === synced.current) return;
    // 组字期间 DOM 归输入法管，外部的清空/追加等组字结束后的输入事件再处理
    if (composing.current) return;
    synced.current = value;
    emitted.current = value;
    const el = field.current?.element;
    if (el && el.value !== value) el.value = value;
  }, [value]);

  const emit = (v: string) => {
    if (v === emitted.current) return;
    emitted.current = v;
    onValueChange(v);
  };

  return (
    <SteamTextField
      {...rest}
      ref={field}
      defaultValue={value}
      onChange={(e: ChangeEvent<HTMLInputElement>) => {
        // 组字中的变更只留在 DOM 里，不写回 React，也不触发重渲染
        if (composing.current || (e.nativeEvent as InputEvent).isComposing) return;
        emit(e.target.value);
      }}
      onCompositionStart={(e: CompositionEvent<HTMLInputElement>) => {
        composing.current = true;
        onCompositionStart?.(e);
      }}
      onCompositionEnd={(e: CompositionEvent<HTMLInputElement>) => {
        composing.current = false;
        emit(e.currentTarget.value);
        onCompositionEnd?.(e);
      }}
      onKeyDown={(e: KeyboardEvent<HTMLInputElement>) => {
        // 组字期间不碰按键：候选选择用的数字键交给输入法，不能当普通字符拼进去
        if (composing.current || e.nativeEvent.isComposing || e.keyCode === 229) return;
        onKeyDown?.(e);
      }}
    />
  );
}

/**
 * Steam 的 TextArea 类（原生 textarea + 虚拟键盘注册 + 自动高度），
 * 和 TextField 同属 CommonUIModule，但 @decky/ui 没有公开导出。
 * 导出名是压缩产物（当前为 Cl），跨 Steam 版本会变，所以按原型上
 * 未压缩的公开方法名做结构匹配。
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const SteamTextArea: any = ((Object.values(CommonUIModule ?? {}) as any[]).find(
  (m) =>
    typeof m === "function" &&
    typeof m.prototype?.GetMinHeight === "function" &&
    typeof m.prototype?.GetMaxHeight === "function",
) ?? null) as any;

type KeyboardHandle = {
  TakeFocusAndShowKeyboard?: () => void;
  HideVirtualKeyboard?: () => void;
};

type ImeTextAreaProps = Omit<
  TextareaHTMLAttributes<HTMLTextAreaElement>,
  "value" | "onChange" | "defaultValue"
> & {
  value: string;
  onValueChange: (v: string) => void;
  /** 固定最小/最大高度，交给 Steam 的 TextArea 自己维护内部滚动 */
  nMinHeight?: number;
  nMaxHeight?: number;
  /** 挂载后聚焦并主动弹出虚拟键盘（等价于 TextField 的 focusOnMount） */
  focusOnMount?: boolean;
};

/**
 * 中文输入法安全的多行编辑框，组字逻辑与 ImeTextField 一致：
 * 组字期间 DOM 归输入法管，React 不回写；compositionend 时收口。
 * 优先用 Steam 的 TextArea（渲染原生 textarea、注册虚拟键盘）；
 * 找不到时退回原生 textarea——能编辑，但游戏模式下不会自动弹 Steam 键盘。
 */
export function ImeTextArea({
  value,
  onValueChange,
  onCompositionStart,
  onCompositionEnd,
  onKeyDown,
  nMinHeight,
  nMaxHeight,
  focusOnMount,
  style,
  ...rest
}: ImeTextAreaProps) {
  const composing = useRef(false);
  const synced = useRef(value);
  const emitted = useRef(value);
  // Steam 的 TextArea 是类组件，实例上有 textarea getter；退回原生时是 DOM 本身
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const field = useRef<any>(null);
  const keyboard = useRef<KeyboardHandle | null>(null);

  const domNode = (): HTMLTextAreaElement | null => {
    const f = field.current;
    if (!f) return null;
    return f.textarea ?? f;
  };

  useEffect(() => {
    if (value === synced.current) return;
    if (composing.current) return;
    synced.current = value;
    emitted.current = value;
    const el = domNode();
    if (el && el.value !== value) el.value = value;
  }, [value]);

  useEffect(() => {
    if (!focusOnMount) return;
    // 等一帧让 Steam 把虚拟键盘的 ref 挂上，再聚焦/弹键盘
    const t = window.setTimeout(() => {
      const el = domNode();
      if (el && document.activeElement !== el) el.focus();
      keyboard.current?.TakeFocusAndShowKeyboard?.();
    }, 0);
    return () => window.clearTimeout(t);
  }, [focusOnMount]);

  const emit = (v: string) => {
    if (v === emitted.current) return;
    emitted.current = v;
    onValueChange(v);
  };

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const Comp: any = SteamTextArea ?? "textarea";
  const steamProps = SteamTextArea
    ? { ref: field, refKeyboardHandle: keyboard, nMinHeight, nMaxHeight }
    : { ref: field };

  return (
    <Comp
      {...rest}
      {...steamProps}
      defaultValue={value}
      style={style}
      onChange={(e: ChangeEvent<HTMLTextAreaElement>) => {
        if (composing.current || (e.nativeEvent as InputEvent).isComposing) return;
        emit(e.target.value);
      }}
      onCompositionStart={(e: CompositionEvent<HTMLTextAreaElement>) => {
        composing.current = true;
        onCompositionStart?.(e);
      }}
      onCompositionEnd={(e: CompositionEvent<HTMLTextAreaElement>) => {
        composing.current = false;
        emit(e.currentTarget.value);
        onCompositionEnd?.(e);
      }}
      onKeyDown={(e: KeyboardEvent<HTMLTextAreaElement>) => {
        // 组字期间不碰按键：候选选择用的数字键交给输入法，换行键也由输入法决定
        if (composing.current || e.nativeEvent.isComposing || e.keyCode === 229) return;
        onKeyDown?.(e);
      }}
    />
  );
}

type Block =
  | { t: "h"; text: string }
  | { t: "ul"; items: string[] }
  | { t: "ol"; items: string[] }
  | { t: "code"; text: string }
  | { t: "p"; text: string };

function parseMd(src: string): Block[] {
  const out: Block[] = [];
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  let para: string[] = [];

  const flushPara = () => {
    if (para.length) {
      out.push({ t: "p", text: para.join("\n") });
      para = [];
    }
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    // 代码块要整段吃掉，里面的 # 和 - 不能当成结构
    const fence = line.match(/^\s*```/);
    if (fence) {
      flushPara();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      out.push({ t: "code", text: buf.join("\n") });
      continue;
    }
    const h = line.match(/^\s*#{1,6}\s+(.+?)\s*#*\s*$/);
    if (h) {
      flushPara();
      out.push({ t: "h", text: h[1] });
      continue;
    }
    const ul = line.match(/^\s*[-*+]\s+(.+)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.+)$/);
    if (ul || ol) {
      flushPara();
      const list = (ul ? "ul" : "ol") as "ul" | "ol";
      const last = out[out.length - 1];
      const items = ul ? [ul[1]] : [ol![1]];
      // 连续的列表行合成一个块，否则每条都单独排一段会很散
      while (i + 1 < lines.length) {
        const nx = lines[i + 1];
        const mu = nx.match(/^\s*[-*+]\s+(.+)$/);
        const mo = nx.match(/^\s*\d+[.)]\s+(.+)$/);
        if (ul && mu) items.push(mu[1]);
        else if (ol && mo) items.push(mo[1]);
        else break;
        i++;
      }
      if (last && last.t === list) last.items.push(...items);
      else out.push({ t: list, items });
      continue;
    }
    if (!line.trim()) {
      flushPara();
      continue;
    }
    para.push(line);
  }
  flushPara();
  return out;
}

/** 行内：`code`、**粗体**；其余原样（表格竖线、链接文字都留着可读） */
function inline(text: string, size: number): ReactNode[] {
  const nodes: ReactNode[] = [];
  const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tk = m[0];
    if (tk.startsWith("`")) {
      nodes.push(
        <code
          key={k++}
          style={{
            background: "rgba(255,255,255,.1)",
            borderRadius: 3,
            padding: "0 3px",
            fontSize: size * 0.88,
          }}
        >
          {tk.slice(1, -1)}
        </code>
      );
    } else {
      nodes.push(
        <b key={k++} style={{ color: "#fff" }}>
          {tk.slice(2, -2)}
        </b>
      );
    }
    last = m.index + tk.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/**
 * 插件的 markdown 渲染。QAM 只有 ~300px 宽，默认字号 12.5；
 * 大屏页传更大的 size，正文更舒展。只认攻略里真会用到的几种结构，
 * 表格和图片按原文保留。
 */
export function Md({
  text,
  align,
  size = 12.5,
}: {
  text: string;
  align?: "left" | "right";
  size?: number;
}) {
  const blocks = parseMd(text);
  return (
    // 对齐挂在最外层：用户消息靠右显示，自己说的话右边缘对齐才顺眼。
    <div style={{ textAlign: align ?? "left" }}>
      {blocks.map((b, i) => {
        if (b.t === "h") {
          return (
            <div
              key={i}
              style={{ fontWeight: 700, fontSize: size, color: "#fff", margin: "6px 0 3px" }}
            >
              {inline(b.text, size)}
            </div>
          );
        }
        if (b.t === "code") {
          return (
            <pre
              key={i}
              style={{
                margin: "4px 0",
                padding: "6px 8px",
                background: "rgba(0,0,0,.35)",
                borderRadius: 4,
                fontSize: size * 0.88,
                lineHeight: 1.4,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                overflowX: "auto",
                textAlign: "left",
              }}
            >
              <code>{b.text}</code>
            </pre>
          );
        }
        if (b.t === "ul" || b.t === "ol") {
          const Tag = b.t;
          return (
            <Tag key={i} style={{ margin: "3px 0", paddingLeft: 18, textAlign: "left" }}>
              {b.items.map((it, j) => (
                <li key={j} style={{ margin: "1px 0" }}>
                  {inline(it, size)}
                </li>
              ))}
            </Tag>
          );
        }
        return (
          <div key={i} style={{ margin: "3px 0" }}>
            {b.text.split("\n").map((ln, j) => (
              <div key={j}>{inline(ln, size)}</div>
            ))}
          </div>
        );
      })}
    </div>
  );
}
