import type { CSSProperties, ReactNode } from "react";

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
