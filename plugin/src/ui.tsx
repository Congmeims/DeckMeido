import type {
  ChangeEvent,
  ComponentProps,
  CompositionEvent,
  KeyboardEvent,
  ReactNode,
  TextareaHTMLAttributes,
} from "react";
import { useEffect, useRef, useState } from "react";
import { CommonUIModule, TextField } from "@decky/ui";
import type { ChatEvent, ChatVoice, Item } from "./useSession";

// 全插件共用的颜色，QAM 与大屏页都从这里取，避免两边各抄一套。
export const ACCENT = "#5aa9e6";
export const DIM = "#8b929a";
export const ERR_COLOR = "#e06c75";
export const TEXT = "#dcdedf";

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

/** 复制文本：Steam 的 CEF 里 navigator.clipboard 可能不可用，退回临时 textarea */
export async function copyText(text: string): Promise<void> {
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    document.execCommand("copy");
    ta.remove();
  }
}

type Align = "" | "left" | "right" | "center";

type Block =
  | { t: "h"; text: string }
  | { t: "ul"; items: string[] }
  | { t: "ol"; items: string[] }
  | { t: "code"; text: string }
  | { t: "table"; header: string[]; aligns: Align[]; rows: string[][] }
  | { t: "p"; text: string };

/** 本机后端地址判定：只有回环主机才算安全，外站/协议相对地址一律丢掉 */
export function localMediaUrl(base: string, raw: string): string {
  const s = String(raw || "").trim();
  if (!s) return "";
  try {
    const u = new URL(s, base || undefined);
    if (u.protocol !== "http:" && u.protocol !== "https:") return "";
    const host = u.hostname.toLowerCase();
    if (host !== "127.0.0.1" && host !== "localhost" && host !== "::1") return "";
    return u.href;
  } catch {
    return "";
  }
}

/** 行内链接只接受 http(s)，其余（javascript:、data: 等）按纯文字显示 */
function externalUrl(raw: string): string {
  try {
    const u = new URL(raw);
    if (u.protocol === "http:" || u.protocol === "https:") return u.href;
  } catch {
    /* 非法 URL 当普通文字 */
  }
  return "";
}

/** 按「未被转义的 |」切单元格；首尾竖线可有可无，行尾缺 | 也认（同网页端） */
function mdSplitCells(line: string): string[] {
  let s = String(line || "").trim();
  if (s.charAt(0) === "|") s = s.slice(1);
  if (s.slice(-1) === "|" && s.slice(-2, -1) !== "\\") s = s.slice(0, -1);
  const cells: string[] = [];
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    const ch = s.charAt(i);
    if (ch === "\\" && s.charAt(i + 1) === "|") {
      cur += "|";
      i++;
      continue;
    }
    if (ch === "|") {
      cells.push(cur);
      cur = "";
      continue;
    }
    cur += ch;
  }
  cells.push(cur);
  return cells.map((c) => c.trim());
}

/** 分隔行：每个单元格只能是 - 加可选冒号；没有竖线的 --- 不算表格 */
function mdSepAligns(line: string): Align[] | null {
  const s = String(line || "").trim();
  if (!s || s.indexOf("|") < 0) return null;
  const cells = mdSplitCells(s);
  if (!cells.length) return null;
  const aligns: Align[] = [];
  for (const cell of cells) {
    const t = cell.replace(/\s+/g, "");
    if (!/^:?-{1,}:?$/.test(t)) return null;
    const left = t.charAt(0) === ":";
    const right = t.charAt(t.length - 1) === ":";
    aligns.push(left && right ? "center" : right ? "right" : left ? "left" : "");
  }
  return aligns;
}

/** 数据行与表头列数相差 1 也接受：少的补空，多的并进最后一格（同网页端） */
function mdFitRow(row: string[], cols: number): string[] {
  if (row.length > cols) {
    const keep = row.slice(0, cols - 1);
    keep.push(row.slice(cols - 1).join(" | "));
    return keep;
  }
  while (row.length < cols) row.push("");
  return row;
}

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
    // 1) 围栏代码块优先整段吃掉：里面的 #、-、| 和 tab 都不参与结构识别
    if (/^\s*```/.test(line)) {
      flushPara();
      const buf: string[] = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      out.push({ t: "code", text: buf.join("\n") });
      continue;
    }
    // 2) 竖线表格：表头行 + 分隔行 + 连续数据行（规则与网页 renderMd 一致）
    if (line.indexOf("|") >= 0 && i + 1 < lines.length) {
      const aligns = mdSepAligns(lines[i + 1]);
      const header = mdSplitCells(line);
      if (
        aligns &&
        header.length &&
        Math.abs(aligns.length - header.length) <= 1 &&
        header.join("").trim()
      ) {
        const cols = header.length;
        const rowAligns: Align[] = header.map((_, idx) => aligns[idx] || "");
        const rows: string[][] = [];
        let j = i + 2;
        while (j < lines.length) {
          const rl = lines[j];
          if (!rl.trim() || rl.indexOf("|") < 0 || mdSepAligns(rl)) break;
          rows.push(mdFitRow(mdSplitCells(rl), cols));
          j++;
        }
        flushPara();
        out.push({ t: "table", header, aligns: rowAligns, rows });
        i = j - 1;
        continue;
      }
    }
    // 3) 制表符表格：连续 ≥2 行、每行 ≥2 个 tab 且列数一致
    if (line.indexOf("\t") >= 0 && line.indexOf("|") < 0) {
      const first = line.split("\t").map((c) => c.trim());
      if (first.length >= 3 && first.join("").trim()) {
        const tabRows = [first];
        let k = i + 1;
        while (k < lines.length && lines[k].indexOf("\t") >= 0 && lines[k].indexOf("|") < 0) {
          const more = lines[k].split("\t").map((c) => c.trim());
          if (more.length !== first.length) break;
          tabRows.push(more);
          k++;
        }
        if (tabRows.length >= 2) {
          flushPara();
          out.push({
            t: "table",
            header: first,
            aligns: first.map(() => ""),
            rows: tabRows.slice(1),
          });
          i = k - 1;
          continue;
        }
      }
    }
    const h = line.match(/^\s*#{1,4}\s+(.+?)\s*#*\s*$/);
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

/**
 * 行内标记。顺序与网页端一致：代码先抽出（里面的星号不当格式），
 * 再按图片 / 外部链接 / 文件链接 / 粗体 / 斜体匹配。
 * React 节点天然转义，模型给出的 HTML 不会被当成标签。
 */
const INLINE_RE =
  /(`[^`\n]+`)|(!\[[^\]]*\]\(\/uploads\/[^)\s]+\))|(\[[^\]]+\]\(https?:[^)\s]+\))|(\[[^\]]+\]\(\/files\/[^)\s]+\))|(\*\*[^*\n]+\*\*)|(\*[^*\n]+\*)/g;

function inline(text: string, size: number, base: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let last = 0;
  let k = 0;
  let m: RegExpExecArray | null;
  INLINE_RE.lastIndex = 0;
  while ((m = INLINE_RE.exec(text))) {
    if (m.index > last) nodes.push(text.slice(last, m.index));
    const tk = m[0];
    if (m[1]) {
      nodes.push(
        <code
          key={k++}
          style={{
            background: "rgba(255,255,255,.1)",
            borderRadius: 3,
            padding: "0 3px",
            fontSize: size * 0.88,
            fontFamily: "ui-monospace, Menlo, Consolas, monospace",
          }}
        >
          {tk.slice(1, -1)}
        </code>
      );
    } else if (m[2]) {
      const im = /^!\[([^\]]*)\]\((\/uploads\/[^)\s]+)\)$/.exec(tk);
      if (im) {
        nodes.push(
          <ShotThumb key={k++} src={im[2]} base={base} maxH={150} inline alt={im[1]} />
        );
      } else {
        nodes.push(tk);
      }
    } else if (m[3]) {
      const lm = /^\[([^\]]+)\]\((https?:[^)\s]+)\)$/.exec(tk);
      const safe = lm ? externalUrl(lm[2]) : "";
      if (lm && safe) {
        nodes.push(
          <a
            key={k++}
            href={safe}
            rel="noopener noreferrer"
            onClick={(e) => {
              // 面板内不能跳走（跳走会把 QAM/大屏路由顶掉），交给系统浏览器打开
              e.preventDefault();
              e.stopPropagation();
              try {
                window.open(safe, "_blank", "noopener,noreferrer");
              } catch {
                /* CEF 拦截了就保持面板不动 */
              }
            }}
            style={{ color: "#6fc3ff", textDecoration: "underline", wordBreak: "break-all" }}
          >
            {lm[1]}
          </a>
        );
      } else {
        nodes.push(lm ? lm[1] : tk);
      }
    } else if (m[4]) {
      // 工作区文件卡片：地址补成本机后端前缀后才可点，否则只显示文件名
      const fm = /^\[([^\]]+)\]\((\/files\/[^)\s]+)\)$/.exec(tk);
      const safe = fm ? localMediaUrl(base, fm[2]) : "";
      const label = fm ? fm[1] || fm[2].split("/").pop() || fm[2] : "";
      if (safe) {
        nodes.push(
          <a
            key={k++}
            href={safe}
            rel="noopener noreferrer"
            onClick={(e) => {
              e.preventDefault();
              e.stopPropagation();
              try {
                window.open(safe, "_blank", "noopener,noreferrer");
              } catch {
                /* 忽略 */
              }
            }}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "0 5px",
              borderRadius: 3,
              border: "1px solid rgba(255,255,255,.18)",
              background: "rgba(255,255,255,.06)",
              color: TEXT,
              textDecoration: "none",
              fontSize: size * 0.92,
              maxWidth: "100%",
            }}
          >
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {label}
            </span>
          </a>
        );
      } else {
        nodes.push(label || tk);
      }
    } else if (m[5]) {
      nodes.push(
        <b key={k++} style={{ color: "#fff" }}>
          {tk.slice(2, -2)}
        </b>
      );
    } else {
      nodes.push(
        <i key={k++} style={{ color: "#e8eaf0" }}>
          {tk.slice(1, -1)}
        </i>
      );
    }
    last = m.index + tk.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

/**
 * 插件的 markdown 渲染。QAM 只有 ~300px 宽，默认字号 12.5；
 * 大屏页传更大的 size，正文更舒展。
 *
 * 代码块与表格都在自己的滚动容器里：面板再窄也不会把布局撑破，
 * 也不会为了塞下内容改成逐字换行（那正是「表格压成一行」的来源）。
 */
export function Md({
  text,
  align,
  size = 12.5,
  base = "",
}: {
  text: string;
  align?: "left" | "right";
  size?: number;
  base?: string;
}) {
  const blocks = parseMd(text);
  return (
    // 对齐挂在最外层：用户消息靠右显示，自己说的话右边缘对齐才顺眼。
    <div style={{ textAlign: align ?? "left", minWidth: 0, maxWidth: "100%" }}>
      {blocks.map((b, i) => {
        if (b.t === "h") {
          return (
            <div
              key={i}
              style={{ fontWeight: 700, fontSize: size, color: "#fff", margin: "6px 0 3px" }}
            >
              {inline(b.text, size, base)}
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
                // pre 不换行：长行留在代码块内部横向滚动，不能把气泡拉宽
                whiteSpace: "pre",
                wordBreak: "normal",
                overflowWrap: "normal",
                overflowX: "auto",
                overflowY: "auto",
                maxWidth: "100%",
                width: "100%",
                boxSizing: "border-box",
                textAlign: "left",
                fontFamily: "ui-monospace, Menlo, Consolas, monospace",
                tabSize: 4,
              }}
            >
              <code>{b.text}</code>
            </pre>
          );
        }
        if (b.t === "table") {
          return (
            <div
              key={i}
              style={{
                margin: "4px 0",
                maxWidth: "100%",
                minWidth: 0,
                overflowX: "auto",
                overflowY: "hidden",
                border: "1px solid rgba(255,255,255,.14)",
                borderRadius: 4,
                textAlign: "left",
              }}
            >
              <table
                style={{
                  borderCollapse: "collapse",
                  // max-content 让列按内容自然排布，窄屏由外层滚动，不把表格压成一段
                  width: "max-content",
                  minWidth: "100%",
                  fontSize: size * 0.92,
                }}
              >
                <thead>
                  <tr>
                    {b.header.map((cell, j) => (
                      <th
                        key={j}
                        style={{
                          padding: "3px 7px",
                          borderBottom: "1px solid rgba(255,255,255,.18)",
                          borderRight: "1px solid rgba(255,255,255,.08)",
                          background: "rgba(255,255,255,.06)",
                          color: "#fff",
                          fontWeight: 600,
                          textAlign: b.aligns[j] || "left",
                          whiteSpace: "normal",
                          wordBreak: "break-word",
                          minWidth: 56,
                          maxWidth: 260,
                        }}
                      >
                        {inline(cell, size, base)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((row, r) => (
                    <tr key={r}>
                      {row.map((cell, j) => (
                        <td
                          key={j}
                          style={{
                            padding: "3px 7px",
                            borderBottom: "1px solid rgba(255,255,255,.08)",
                            borderRight: "1px solid rgba(255,255,255,.08)",
                            textAlign: b.aligns[j] || "left",
                            whiteSpace: "normal",
                            wordBreak: "break-word",
                            minWidth: 56,
                            maxWidth: 260,
                            verticalAlign: "top",
                          }}
                        >
                          {inline(cell, size, base)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          );
        }
        if (b.t === "ul" || b.t === "ol") {
          const Tag = b.t;
          return (
            <Tag key={i} style={{ margin: "3px 0", paddingLeft: 18, textAlign: "left" }}>
              {b.items.map((it, j) => (
                <li key={j} style={{ margin: "1px 0" }}>
                  {inline(it, size, base)}
                </li>
              ))}
            </Tag>
          );
        }
        return (
          <div key={i} style={{ margin: "3px 0" }}>
            {b.text.split("\n").map((ln, j) => (
              <div key={j}>{inline(ln, size, base)}</div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

/** 语音时长：和网页端一样按 m:ss 显示 */
function fmtSeconds(sec?: number): string {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
}

/**
 * 消息里的画面：缩略显示，点击在原位放大/还原。
 * 地址只接受本机后端（见 localMediaUrl），加载失败给一句明确的占位说明。
 */
function ShotThumb({
  src,
  base,
  maxH = 180,
  inline,
  alt,
}: {
  src: string;
  base: string;
  maxH?: number;
  inline?: boolean;
  alt?: string;
}) {
  const [err, setErr] = useState(false);
  const [big, setBig] = useState(false);
  const safe = localMediaUrl(base, src);
  if (!safe) {
    return (
      <span style={{ color: DIM, fontSize: 11 }}>（图片地址不是本机后端，已忽略）</span>
    );
  }
  if (err) {
    return (
      <div style={{ color: DIM, fontSize: 11, lineHeight: 1.5 }}>
        （这张画面已不在服务器上，仅当时的模型看过）
      </div>
    );
  }
  return (
    <img
      src={safe}
      alt={alt || "画面"}
      onError={() => setErr(true)}
      onClick={() => setBig((v) => !v)}
      title={big ? "点击缩小" : "点击放大"}
      style={{
        display: inline ? "inline-block" : "block",
        verticalAlign: inline ? "middle" : undefined,
        maxWidth: "100%",
        maxHeight: big ? 480 : maxH,
        width: "auto",
        height: "auto",
        objectFit: "contain",
        margin: inline ? "2px 4px" : "0 0 6px",
        borderRadius: 4,
        cursor: "pointer",
      }}
    />
  );
}

/** 思考块：默认收起（与网页历史回放一致），放在正文之前 */
function ThoughtRow({ text, size }: { text: string; size: number }) {
  const [open, setOpen] = useState(false);
  return (
    <div
      style={{
        margin: "4px 0",
        paddingLeft: 8,
        borderLeft: "2px solid rgba(255,255,255,.14)",
      }}
    >
      <div
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          cursor: "pointer",
          userSelect: "none",
          color: DIM,
          fontSize: size * 0.95,
        }}
      >
        <span>{open ? "▾" : "▸"}</span>
        <span>思考</span>
      </div>
      {open && (
        <div
          style={{
            marginTop: 3,
            color: DIM,
            fontSize: size * 0.9,
            lineHeight: 1.5,
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            maxHeight: 160,
            overflowY: "auto",
          }}
        >
          {text}
        </div>
      )}
    </div>
  );
}

/** 工具行文案：与网页 toolRowInfo 的措辞保持一致 */
function toolHeadInfo(ev: Extract<ChatEvent, { type: "tool" }>): { text: string; chip?: string } {
  const chipBase = ev.summary || "";
  switch (ev.name) {
    case "read_file":
      return { text: "已读取", chip: chipBase };
    case "list_dir":
      return { text: "已查看目录", chip: chipBase };
    case "write_file":
    case "edit_file":
      return { text: "已编辑 1个文件" };
    case "run_command":
      return { text: "已运行", chip: chipBase };
    case "screenshot":
      return { text: ev.error ? "截屏失败" : "已截屏", chip: chipBase };
    case "get_weather":
      return { text: "已查询天气", chip: chipBase };
    case "web_search":
      return { text: "已搜索", chip: chipBase };
    case "list_installed_games":
      return { text: "已查询游戏库" };
    case "find_game":
      return { text: "已在商店搜索", chip: chipBase };
    case "list_emulator_roms":
      return { text: "已扫描模拟器 ROM" };
    case "check_system_updates":
      return { text: "已检查系统更新" };
    case "install_game":
      return { text: "已安装游戏", chip: chipBase };
    default:
      return { text: "已完成 " + (ev.label || ev.name || ""), chip: chipBase };
  }
}

/** 工具/执行条目：标题行显示摘要与状态，长输出默认折叠，点开后在固定高度里滚动 */
function ToolRow({
  ev,
  size,
  base,
}: {
  ev: Extract<ChatEvent, { type: "tool" }>;
  size: number;
  base: string;
}) {
  const info = toolHeadInfo(ev);
  const failed = !!ev.error || !!ev.denied;
  const hasBody = !!(ev.output || ev.image || ev.error || ev.reason || ev.files?.length || ev.diff);
  const [open, setOpen] = useState(!failed && !!ev.image);
  const head = ev.approved ? `已批准：${ev.summary || ""}` : failed && ev.denied ? `已拒绝：${info.text}` : info.text;
  return (
    <div
      style={{
        margin: "3px 0",
        padding: "3px 6px",
        borderLeft: `2px solid ${failed ? ERR_COLOR : "rgba(255,255,255,.16)"}`,
        background: "rgba(255,255,255,.03)",
        borderRadius: 3,
        minWidth: 0,
        maxWidth: "100%",
      }}
    >
      <div
        onClick={() => hasBody && setOpen((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 5,
          minWidth: 0,
          cursor: hasBody ? "pointer" : "default",
          color: failed ? ERR_COLOR : DIM,
          fontSize: size * 0.92,
          userSelect: "none",
        }}
      >
        <span>{hasBody ? (open ? "▾" : "▸") : "·"}</span>
        <span style={{ flex: "0 1 auto", minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {head}
        </span>
        {!ev.approved && info.chip && (
          <span
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              color: TEXT,
              background: "rgba(255,255,255,.07)",
              borderRadius: 3,
              padding: "0 4px",
              fontFamily: "ui-monospace, Menlo, Consolas, monospace",
              fontSize: size * 0.85,
            }}
          >
            {info.chip}
          </span>
        )}
      </div>
      {open && hasBody && (
        <div style={{ marginTop: 3, maxHeight: 190, overflow: "auto", minWidth: 0 }}>
          {ev.image && <ShotThumb src={ev.image} base={base} maxH={220} alt="屏幕截图" />}
          {ev.error && (
            <div style={{ color: ERR_COLOR, fontSize: size * 0.9, whiteSpace: "pre-wrap" }}>
              截图没有成功：{ev.error}
            </div>
          )}
          {ev.reason && !ev.error && (
            <div style={{ color: ERR_COLOR, fontSize: size * 0.9, whiteSpace: "pre-wrap" }}>
              {ev.reason}
            </div>
          )}
          {ev.output && (
            <pre
              style={{
                margin: 0,
                color: "#cfd3da",
                fontSize: size * 0.86,
                lineHeight: 1.4,
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
                fontFamily: "ui-monospace, Menlo, Consolas, monospace",
              }}
            >
              {ev.output}
            </pre>
          )}
          {!!ev.diff && (
            <div
              style={{
                fontSize: size * 0.9,
                color: DIM,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {ev.path ? ev.path.split(/[\\/]/).pop() || ev.path : "文件改动"}{" "}
              <span style={{ color: "#7fce7f" }}>+{ev.diff.add || 0}</span>{" "}
              <span style={{ color: ERR_COLOR }}>-{ev.diff.del || 0}</span>
            </div>
          )}
          {(ev.files || []).map((f, i) => (
            <div
              key={i}
              style={{
                fontSize: size * 0.9,
                color: DIM,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              写入 {f}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 主动提问：问题正文 + 候选按钮（插件内只展示，点选仍在网页/客户端做） */
function AskCard({
  question,
  options,
  size,
  base,
}: {
  question: string;
  options?: string[];
  size: number;
  base: string;
}) {
  return (
    <div style={{ margin: "4px 0", minWidth: 0 }}>
      {!!question.trim() && <Md text={question} size={size} base={base} />}
      {!!options?.length && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
          {options.map((o, i) => (
            <span
              key={i}
              style={{
                maxWidth: "100%",
                padding: "1px 6px",
                border: "1px solid rgba(255,255,255,.16)",
                borderRadius: 3,
                background: "rgba(255,255,255,.07)",
                color: TEXT,
                fontSize: size * 0.9,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {o}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

/** 语音条：播放/暂停、进度、时长；不自动播放（与网页端一致） */
function VoiceBar({ voice, base, size }: { voice: ChatVoice; base: string; size: number }) {
  const src = localMediaUrl(base, voice.url || "");
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [pct, setPct] = useState(0);
  const [err, setErr] = useState("");
  if (!src) {
    return (
      <div style={{ marginTop: 5, color: DIM, fontSize: 11 }}>
        （语音地址不是本机后端，已忽略）
      </div>
    );
  }
  const toggle = async () => {
    const el = audioRef.current;
    if (!el) return;
    if (!el.paused) {
      el.pause();
      return;
    }
    setErr("");
    try {
      await el.play();
    } catch {
      setErr("语音播放失败：插件内播放器不可用，请到网页端收听");
    }
  };
  return (
    <div style={{ marginTop: 6, maxWidth: "100%", minWidth: 0 }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "4px 7px",
          border: "1px solid rgba(255,255,255,.14)",
          borderRadius: 4,
          background: "rgba(127,127,127,.10)",
          minWidth: 0,
        }}
      >
        <button
          type="button"
          onClick={() => void toggle()}
          style={{
            width: 22,
            height: 22,
            flex: "0 0 auto",
            border: "none",
            borderRadius: "50%",
            background: ACCENT,
            color: "#fff",
            fontSize: 11,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            cursor: "pointer",
            padding: 0,
          }}
        >
          {playing ? "■" : "▶"}
        </button>
        <div
          style={{
            flex: "1 1 auto",
            minWidth: 24,
            height: 3,
            borderRadius: 2,
            background: "rgba(255,255,255,.16)",
            overflow: "hidden",
          }}
        >
          <div style={{ width: `${pct}%`, height: "100%", background: ACCENT }} />
        </div>
        <span
          style={{
            flex: "0 0 auto",
            color: DIM,
            fontSize: size * 0.88,
            whiteSpace: "nowrap",
          }}
        >
          {(voice.speaker || "机器人") + " · " + fmtSeconds(voice.seconds)}
        </span>
        <audio
          ref={audioRef}
          src={src}
          preload="none"
          onPlay={() => setPlaying(true)}
          onPause={() => setPlaying(false)}
          onEnded={() => {
            setPlaying(false);
            setPct(0);
          }}
          onTimeUpdate={(e) => {
            const a = e.currentTarget;
            const d = a.duration || Number(voice.seconds) || 0;
            setPct(d ? Math.min(100, (a.currentTime / d) * 100) : 0);
          }}
          onError={() => setErr("语音播放失败：插件内播放器不可用，请到网页端收听")}
        />
      </div>
      {err && (
        <div style={{ marginTop: 3, color: ERR_COLOR, fontSize: size * 0.88 }}>{err}</div>
      )}
    </div>
  );
}

// 消息来源标签：与网页端 clientLabel 一致；本插件自己发的不标
const CLIENT_LABEL: Record<string, string> = {
  web: "电脑",
  mobile: "手机",
  client: "掌机",
  qam: "插件",
};

/**
 * 一条消息的完整内容：画面缩略图 → 过程事件（思考/过渡正文/工具/提问，
 * 保持后端给出的顺序，思考自然排在正文前）→ 正文 → 语音条。
 */
export function MessageContent({
  item,
  size,
  align,
}: {
  item: Item;
  size: number;
  align?: "left" | "right";
}) {
  const base = item.base || "";
  const body = String(item.text || "").trim();
  const events = item.events || [];
  const srcLabel =
    item.role === "user" && item.client !== "qam"
      ? CLIENT_LABEL[item.client] || (item.device ? "电脑" : "")
      : "";
  return (
    <div style={{ minWidth: 0, maxWidth: "100%" }}>
      {!!srcLabel && (
        <div style={{ fontSize: size * 0.82, color: DIM, marginBottom: 2 }}>{srcLabel}</div>
      )}
      {item.image && <ShotThumb src={item.image} base={base} maxH={180} alt="附带的画面" />}
      {events.map((ev, i) => {
        if (ev.type === "thought") {
          return ev.text.trim() ? <ThoughtRow key={i} text={ev.text} size={size} /> : null;
        }
        if (ev.type === "text") {
          const t = ev.text.trim();
          // 最终正文已经在下面渲染，过程里完全相同的一段不再重复
          if (!t || t === body) return null;
          return (
            <div key={i} style={{ margin: "3px 0" }}>
              <Md text={ev.text} size={size} align={align} base={base} />
            </div>
          );
        }
        if (ev.type === "ask") {
          return (
            <AskCard
              key={i}
              question={ev.question}
              options={ev.options}
              size={size}
              base={base}
            />
          );
        }
        return <ToolRow key={i} ev={ev} size={size} base={base} />;
      })}
      {!!body && <Md text={item.text} align={align} size={size} base={base} />}
      {item.voice && <VoiceBar voice={item.voice} base={base} size={size} />}
    </div>
  );
}
