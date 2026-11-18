/**
 * 在 Steam GamepadUI 左侧菜单里注入一个「DeckMeido」入口。
 *
 * 为什么之前会壮烈失败（记录下来，避免回退）：
 *   社区通行做法（Decky Music 等）是把菜单外层组件 `fe.type` 换成自己的 wrapper，
 *   再从渲染结果里把菜单项容器 `Ie.type` 换成自己的 wrapper，往菜单项数组里 splice 一项。
 *   这套做法是「独占」的——同一时刻只允许一个插件拥有 `fe` / `Ie`。
 *   两个这样做的插件同时安装时，后注入的一方要么被前者的 `__deckyMusic` 标记挡掉
 *   （入口静默消失），要么覆盖掉前者。
 *   另外 Steam 的菜单项没有显式 key，splice 会让 React 按数组下标复用到错位的孩子，
 *   表现为「插了自己的项，别人的项就没了」。
 *
 * 现在的做法（不动 `fe`、不动 `Ie`、不动原菜单项数组）：
 *   1. 从当前窗口（Decky 插件跑在 SharedJSContext）的 DOM 上爬到 React 根，
 *      按 props 特征找到左侧菜单外层组件 `fe`，再找菜单项 Fragment fiber；
 *   2. 在 Fragment 的父 fiber（菜单项容器组件）上用 `onCommitFiberRoot` 之后的渲染钩子不合适，
 *      所以改成 DOM 层：把真实挂载的菜单 Panel（aria-label="主菜单"）里的「商店」那一行
 *      clone 一份，改文案/图标/aria-label，插到它后面；
 *   3. 点击时调用 Decky 的 `Navigation.Navigate("/deckmeido")`，与 QAM 里进大屏是同一条路径；
 *   4. 每秒检查一次：React 重渲染会把我们补的节点丢掉，丢了就补回来。
 *
 * 这样：
 *   - 不动 `fe.type` / `Ie.type`，Decky Music 的所有权检查永远命中它自己那一层；
 *   - 不动 React 的菜单项数组，不存在隐式下标 key 错位；
 *   - 即便 Steam 或别的插件重渲染整个菜单，我们下一轮把这一行补回来。
 *
 * 约束：
 *   - 任何异常都只让本轮 no-op，绝不抛到 Steam 的 render 栈里；
 *   - 卸载时把补的节点删掉，菜单回到原生状态。
 */
import { Navigation } from "@decky/ui";
import { GiTeapot } from "react-icons/gi";

/** 注入项的 route，与 index.tsx 里 routerHook 注册的同一个常量（避免循环依赖，这里重复定义一份） */
const MENU_ROUTE = "/deckmeido";
/** 菜单里「商店」的可见文案（本地化后用来定位锚点） */
const AFTER_LABEL = "商店";
/** 菜单检查间隔：React 重渲染会把我们补的节点丢掉，需要周期补回 */
const RETRY_MS = 1000;
/** 补的节点上的标记，用于去重 / 清理 */
const DOM_MARK = "data-deckmeido-item";
/** 菜单行所属 Panel 的可访问名 */
const PANEL_LABEL = "主菜单";
/** 要插入的菜单项文案 */
const ITEM_LABEL = "DeckMeido";

type AnyFiber = {
  type: any;
  return?: AnyFiber | null;
  child?: AnyFiber | null;
  sibling?: AnyFiber | null;
  memoizedProps?: any;
  tag?: number;
  stateNode?: any;
};

type FiberPred = (f: AnyFiber) => boolean;

/** 左侧菜单外层组件：带登录态、展开态和 popup */
const isMenuRootFiber: FiberPred = (f) => {
  const p = f?.memoizedProps;
  return !!p && p.bLoggedIn !== undefined && p.open !== undefined && "popup" in p;
};

/** 原生菜单项：有 route 和 label 的 element */
const isMenuItemEl = (x: any) =>
  x &&
  typeof x === "object" &&
  x.props &&
  typeof x.props.route === "string" &&
  typeof x.props.label === "string" &&
  x.type &&
  typeof x.type !== "string";

function log(...args: any[]) {
  try {
    console.warn("[DeckMeido]", ...args);
  } catch {
    /* console 不可用不能影响 UI */
  }
}

/** 调试用的落盘记录（Steam 的 console 在部分窗口不可见，直接读 window.__deckmeidoStamp） */
function stamp(...args: any[]) {
  try {
    const g: any = window;
    g.__deckmeidoStamp = g.__deckmeidoStamp || [];
    g.__deckmeidoStamp.push([Date.now(), ...args]);
    if (g.__deckmeidoStamp.length > 200) g.__deckmeidoStamp.shift();
  } catch {
    /* ignore */
  }
  log(...args);
}

/**
 * 沿 DOM 节点上的 __reactFiber$ 往上爬到根。
 * Decky 插件运行在 SharedJSContext，但这份 fiber 树对应的是主菜单（MainMenu）窗口的文档，
 * 所以 `document.getElementById("MainNavMenuContainer")` 在这里是 null —— 必须靠 fiber 拿到
 * 那个窗口里的真实 DOM 节点。
 */
function rootFiber(): AnyFiber | null {
  try {
    for (const el of Array.from(document.querySelectorAll("*"))) {
      const k = Object.keys(el).find((key) => key.startsWith("__reactFiber"));
      if (k) {
        let f = (el as any)[k] as AnyFiber;
        for (let n = 0; f?.return && n < 1000; n++) f = f.return;
        return f;
      }
    }
  } catch {
    /* 找不到根就 no-op */
  }
  return null;
}

function findFiber(root: AnyFiber, pred: FiberPred, cap = 20000): AnyFiber | null {
  let n = 0;
  const queue: AnyFiber[] = [root];
  while (queue.length) {
    const f = queue.shift();
    if (!f || n++ > cap) continue;
    try {
      if (pred(f)) return f;
    } catch {
      /* 单个谓词异常忽略 */
    }
    if (f.child) queue.push(f.child);
    if (f.sibling) queue.push(f.sibling);
  }
  return null;
}

/** 找已经渲染出来的菜单项 Fragment fiber（tag 7，props 是 {0:item,1:item,...}） */
function findMenuFragment(fe: AnyFiber): AnyFiber | null {
  const stack: AnyFiber[] = [fe];
  let n = 0;
  while (stack.length && n++ < 20000) {
    const f = stack.pop()!;
    const props: any = f.memoizedProps;
    if (f.tag === 7 && props) {
      const keys = Object.keys(props).filter((k) => /^\d+$/.test(k));
      if (keys.length && keys.map((k) => props[k]).some(isMenuItemEl)) return f;
    }
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
  }
  return null;
}

/** 从 fiber 往下走，找到第一个真实 DOM 节点（tag 5 = HostComponent） */
function hostDomOf(f: AnyFiber | null): HTMLElement | null {
  if (!f) return null;
  const stack: AnyFiber[] = [f];
  let n = 0;
  while (stack.length && n++ < 300) {
    const cur = stack.pop()!;
    if (cur.tag === 5 && cur.stateNode && cur.stateNode.nodeType === 1) return cur.stateNode as HTMLElement;
    if (cur.child) stack.push(cur.child);
    if (cur.sibling) stack.push(cur.sibling);
  }
  return null;
}

/**
 * 找到左侧菜单的 Panel：每个菜单行都是它的直接子元素。
 * 通过 aria-label 匹配而不是 class（class 是 Steam 的哈希名，版本一变就失效）。
 */
function menuPanel(): HTMLElement | null {
  const root = rootFiber();
  if (!root) return null;
  const fe = findFiber(root, isMenuRootFiber);
  if (!fe) return null;
  const frag = findMenuFragment(fe);
  const anyRow = hostDomOf(frag?.child ?? null);
  if (!anyRow) return null;
  let cur: HTMLElement | null = anyRow;
  for (let i = 0; cur && i < 8; i++) {
    if (cur.getAttribute && cur.getAttribute("aria-label") === PANEL_LABEL) return cur;
    cur = cur.parentElement;
  }
  return null;
}

function menuRowOf(el: Element | null): Element | null {
  if (!el) return null;
  if (el.getAttribute("role") === "menuitem") return el;
  return el.querySelector('[role="menuitem"]');
}

function rowLabel(row: Element | null): string | null {
  const item = menuRowOf(row);
  return item ? item.getAttribute("aria-label") : null;
}

/** 我们的入口是否已经在菜单里 */
function existingItem(): HTMLElement | null {
  const panel = menuPanel();
  if (!panel) return null;
  return panel.querySelector(`[${DOM_MARK}]`) as HTMLElement | null;
}

/** 跳到大屏：与 QAM 里走的是同一个 Decky 路由，Steam 侧不用关心 */
function activate(e?: Event) {
  try {
    e?.preventDefault?.();
    e?.stopPropagation?.();
  } catch {
    /* ignore */
  }
  try {
    Navigation.Navigate(MENU_ROUTE);
  } catch (err) {
    log("跳转失败", err);
  }
}

function bindActivate(el: HTMLElement) {
  if ((el as any).__deckmeidoBound) return;
  (el as any).__deckmeidoBound = true;
  el.addEventListener("click", activate);
  el.addEventListener("keydown", (e: any) => {
    if (e.key === "Enter" || e.key === " ") activate(e);
  });
}

/**
 * 生成图标节点的 svg 字符串。
 * 直接用 react-icons 组件定义里的 path 数据手拼 svg，避免引入 react-dom/server。
 */
function iconSvg(icon: any): string {
  try {
    const props = typeof icon === "function" ? icon({ size: 22, color: "currentColor" }) : icon;
    const children = props?.props?.children;
    const inner = Array.isArray(children) ? children : [children];
    const body = inner
      .filter(Boolean)
      .map((c: any) => {
        const cp = c?.props || {};
        const attrs = Object.keys(cp)
          .filter((k) => k !== "children")
          .map((k) => `${k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}="${String(cp[k])}"`)
          .join(" ");
        return `<${c.type || "path"} ${attrs} />`;
      })
      .join("");
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width="22" height="22" fill="currentColor">${body}</svg>`;
  } catch {
    return "";
  }
}

/** clone「商店」那一行，换文案 + 图标，插到它后面 */
function insertDomItem(panel: HTMLElement): boolean {
  const rows = Array.from(panel.children);
  const anchor = rows.find((r) => rowLabel(r) === AFTER_LABEL);
  if (!anchor) return false;
  const doc = anchor.ownerDocument;
  const el = anchor.cloneNode(true) as HTMLElement;
  el.setAttribute(DOM_MARK, "1");
  const item = menuRowOf(el);
  if (!item) return false;
  item.setAttribute("aria-label", ITEM_LABEL);
  item.removeAttribute("aria-current");

  // 文案：菜单行里第一个非空文本节点
  const walker = doc.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let textSet = false;
  while (walker.nextNode()) {
    const t = walker.currentNode as Text;
    if (t.nodeValue && t.nodeValue.trim()) {
      t.nodeValue = ITEM_LABEL;
      textSet = true;
      break;
    }
  }
  if (!textSet) return false;

  // 图标：把原生 svg 换成我们的图标，容器尺寸不变（决定文字左边距）
  const svg = el.querySelector("svg");
  const holder = svg?.parentElement;
  const markup = iconSvg(GiTeapot as any);
  if (holder && markup) holder.innerHTML = markup;

  bindActivate(el);
  anchor.parentElement?.insertBefore(el, anchor.nextSibling);
  return true;
}

function removeDomItem() {
  try {
    const item = existingItem();
    if (item) item.remove();
  } catch {
    /* ignore */
  }
}

// ---- 生命周期 ----
let timer: ReturnType<typeof setInterval> | null = null;

function tick() {
  try {
    if (existingItem()) return;
    const panel = menuPanel();
    if (!panel) return;
    if (insertDomItem(panel)) stamp("dom insert ok");
  } catch (e) {
    /* 单轮失败不影响菜单，下一轮再试 */
    stamp("tick error", String(e));
  }
}

export function enableMenuInjection() {
  stamp("enable called", "timer=", timer ? "on" : "off");
  if (timer) return;
  timer = setInterval(tick, RETRY_MS);
  tick();
}

export function disableMenuInjection() {
  try {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  } catch {
    /* ignore */
  }
  removeDomItem();
}
