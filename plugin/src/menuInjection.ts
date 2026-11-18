/**
 * 在 Steam GamepadUI 左侧菜单里注入一个「DeckMeido」入口。
 *
 * 已经试过、确认走不通的路（别再回头）：
 *
 * 1) 社区通行做法（DeckWebBrowser / Deckcord / Loadout / Decky Music）：包菜单外层和内层
 *    菜单组件的 `type`，再往菜单项数组里 splice。这套是「独占」的，两个插件同时装会
 *    互相顶掉。
 *
 * 2) 纯 DOM clone：克隆出来的节点没有导航节点，Steam 焦点导航按导航树遍历，补多少
 *    `Focusable` / `tabindex` 都没用，方向键直接略过。
 *
 * 3) 改已提交 fiber 的 `memoizedProps`：不会触发重渲染，DOM 永远不变。
 *
 * 4) 包 `S.type` / 替换 `inst.render` + `forceUpdate`：前者破坏 hooks 链，后者直接把
 *    mainmenu 搞崩。
 *
 * 现在这条路：**借位**——不加新节点，直接把原生「库」那一格占下来。
 *
 * Steam 左侧菜单是固定的 9 项，每项在 `MainNavMenuContainer` 导航树里有一个节点，
 * 节点上真正决定行为的是 `m_element`（焦点框哪个 DOM）和 `m_rgNavigationHandlers`
 * （按 A 干什么）。所以：
 *
 *   - 在菜单列表 DOM 里用我们的条目顶掉 `库` 那一项；
 *   - 把 `库` 节点的 `m_element` 指向我们的条目，往回压一个导航处理器接管 A 键。
 *
 * 这样方向键走到这一格时，焦点框落的是 DeckMeido，按 A 进大屏。完全不碰 React，
 * 也就不再和 Decky Music 抢菜单。
 *
 * 已知差异：原生的 `库` 入口会被顶掉（要进库得走主页或用其它入口）。
 *
 * 约束：
 *   - 不碰 Steam 内部模块，只读写导航树实例上的字段；
 *   - 任何异常只让本次注入放弃，绝不把菜单搞崩；
 *   - 卸载时把 DOM 和节点字段还原。
 */
import { Navigation } from "@decky/ui";
import { createElement } from "react";
import { GiTeapot } from "react-icons/gi";

/**
 * 把图标组件转成 SVG 字符串。
 *
 * react-dom 在插件里不是可用的导入（@decky/rollup 不打包它，运行时也拿不到），
 * 而图标组件本身就是 `{ tag: "svg", attr: {...}, child: [...] }` 这种结构，
 * 直接拼字符串最省事。
 */
function svgMarkup() {
  const icon: any = createElement(GiTeapot, { size: "1.4em" });
  const { tag, attr, child } = icon.props;
  const attrs = Object.keys(attr || {})
    .map((k) => `${k}="${String(attr[k]).replace(/"/g, "&quot;")}"`)
    .join(" ");
  const body = Array.isArray(child)
    ? child
        .map((c: any) => {
          const a = Object.keys(c.attr || {})
            .map((k) => `${k}="${String(c.attr[k])}"`)
            .join(" ");
          return `<${c.tag} ${a}></${c.tag}>`;
        })
        .join("")
    : "";
  return `<${tag} ${attrs}>${body}</${tag}>`;
}

/** 注入项的 route，与 index.tsx 里 routerHook 注册的同一个常量（避免循环依赖，这里重复定义） */
const MENU_ROUTE = "/deckmeido";
/** 要插入的菜单项文案 */
const ITEM_LABEL = "DeckMeido";
/** 借用的菜单项文案 */
const ANCHOR_LABEL = "库";
/** 我们的可见条目 */
const ENTRY_CLASS = "deckmeido-menu-entry";

function log(...a: any[]) {
  try {
    console.warn("[DeckMeido]", ...a);
  } catch {
    /* console 不可用不能影响 UI */
  }
}

/** 调试用落盘（Steam 的 console 在部分窗口不可见，直接读 window.__deckmeidoStamp） */
function stamp(...a: any[]) {
  try {
    const g: any = window;
    g.__deckmeidoStamp = g.__deckmeidoStamp || [];
    g.__deckmeidoStamp.push([Date.now(), ...a]);
    if (g.__deckmeidoStamp.length > 200) g.__deckmeidoStamp.shift();
  } catch {
    /* ignore */
  }
  log(...a);
}

/**
 * 拿 CFocusNavController 实例。
 *
 * 实测插件跑在 shared 窗口，`window.FocusNavController` 就在这一侧，并且
 * `m_rgAllContexts` 里能看到其它窗口的导航树；`@decky/ui` 导出的 `Navigation`
 * 在运行时是 undefined，所以只能走全局对象。
 */
function getController(): any | null {
  const w = window as any;
  const candidates = [w.FocusNavController, (Navigation as any)?.NavigationManager];
  for (const c of candidates) {
    if (c && Array.isArray(c.m_rgAllContexts)) return c;
  }
  return null;
}

function findMenuList(fc: any): { list: any; kids: any[] } | null {
  for (const ctx of Array.from(fc.m_rgAllContexts || []) as any[]) {
    for (const t of ctx?.m_rgGamepadNavigationTrees || []) {
      if (t?.m_ID !== "MainNavMenuContainer") continue;
      // 根的直接子节点可能是菜单列表本身，也可能再套一层，按元素数量找真正的列表
      const queue = [...(t.Root?.m_rgChildren || [])];
      while (queue.length) {
        const node: any = queue.shift();
        const kids = node?.m_rgChildren;
        if (Array.isArray(kids) && kids.length >= 5) return { list: node, kids };
        if (Array.isArray(kids)) queue.push(...kids);
      }
    }
  }
  return null;
}

/** 找要借位的菜单项：原生 `库` 那一项 */
function pickSlot(kids: any[]): any | null {
  for (const k of kids) {
    const text = k?.m_element?.textContent?.trim();
    if (text && text.includes(ANCHOR_LABEL)) return k;
  }
  return kids[1] || null;
}

function goToMenuRoute() {
  try {
    const nm: any = Navigation;
    if (nm && typeof nm.Navigate === "function") {
      nm.Navigate(MENU_ROUTE);
      return;
    }
  } catch (e) {
    stamp("navigate failed", String(e));
  }
  stamp("navigate unavailable");
}

/**
 * 接管档位节点的激活行为。
 *
 * 原生菜单项的 A 键不在节点属性上（`m_Properties.onActivate` 是 undefined），但节点挂
 * DOM 时注册的处理器按 DOM 上的 `__nav_node` 反查节点，所以把我们的条目伪装成那个节点
 * 的 DOM（补上 `__nav_node` / `m_element` / `m_bMounted`）后，方向键与按 A 都走原生路径。
 */
function attachNavNode(slot: any, host: HTMLElement) {
  (host as any).__nav_node = slot;
  slot.m_element = host;
  slot.m_bMounted = true;
  const navKey = slot.m_Properties?.navKey;
  if (navKey) host.setAttribute("data-nav-key", String(navKey));
}

export function enableMenuInjection() {
  stamp("enable called");
  let tries = 0;
  const tick = () => {
    if (++tries > 240) return; // 约 2 分钟还没就绪就放弃，不再空转
    try {
      const fc = getController();
      if (!fc) {
        if (tries % 20 === 1) stamp("waiting for FocusNavController", tries);
      } else {
        const found = findMenuList(fc);
        if (found && install(fc, found)) return;
        if (tries % 20 === 1) stamp("waiting for menu tree", tries, !!found);
      }
    } catch (e) {
      stamp("install error", String(e));
    }
    setTimeout(tick, 500);
  };
  tick();
}

let installed: {
  slot: any;
  host: HTMLElement;
  prevEl: any;
  prevHandlers?: any[];
} | null = null;

function install(fc: any, found: { list: any; kids: any[] }): boolean {
  if (installed) return true;
  const slot: any = pickSlot(found.kids);
  if (!slot) return false;
  const idx = found.kids.indexOf(slot);

  const prevEl = slot.m_element;
  if (!prevEl || !prevEl.parentElement) return false;
  const doc: Document = prevEl.ownerDocument;
  const win: any = doc.defaultView || window;

  // 原来的菜单项挪到旁边藏着，先不删：删掉的话万一 Steam 要重排菜单，会把整行搞乱。
  // 它的 class / tabindex 复制到我们的条目上，Steam 的焦点样式和 gpfocus 类照常生效。
  prevEl.style.position = "absolute";
  prevEl.style.visibility = "hidden";
  prevEl.style.pointerEvents = "none";
  const host = doc.createElement("div");
  host.className = (prevEl.className || "") + " " + ENTRY_CLASS;
  host.setAttribute("role", "menuitem");
  host.setAttribute("aria-label", ITEM_LABEL);
  host.setAttribute("tabindex", prevEl.getAttribute("tabindex") || "0");
  host.style.display = "flex";
  host.style.alignItems = "center";
  host.style.width = "200px";
  host.style.flex = "0 0 auto";

  // 图标 + 文案：照抄原生菜单项的内部结构，避免被菜单项自身的 flex 布局挡住文字
  const icon = doc.createElement("div");
  const iconEl = prevEl.querySelector("svg")?.parentElement;
  if (iconEl) icon.className = iconEl.className;
  else icon.style.marginRight = "11px";
  icon.innerHTML = svgMarkup();
  const label = doc.createElement("span");
  label.textContent = ITEM_LABEL;
  label.style.marginLeft = "4px";
  host.append(icon, label);

  host.addEventListener("click", () => goToMenuRoute());
  prevEl.parentElement.insertBefore(host, prevEl);

  const prevHandlers = Array.isArray(slot.m_rgNavigationHandlers) ? slot.m_rgNavigationHandlers.slice() : undefined;
  attachNavNode(slot, host);
  // 节点换了 DOM，导航树缓存的路由 / 排序依据都要作废，否则方向键按旧顺序走
  slot.m_bChildrenSorted = false;
  found.list.m_bChildrenSorted = false;

  installed = { slot, host, prevEl, prevHandlers };
  stamp("menu slot installed at", idx, "win:", win?.name || "");
  return true;
}

export function disableMenuInjection() {
  if (!installed) return;
  const it = installed;
  installed = null;
  try {
    it.slot.m_element = it.prevEl;
    if (it.prevHandlers) it.slot.m_rgNavigationHandlers = it.prevHandlers;
    it.prevEl.style.position = "";
    it.prevEl.style.visibility = "";
    it.prevEl.style.pointerEvents = "";
  } catch {
    /* ignore */
  }
  try {
    if (it.host.parentElement && it.prevEl) it.host.parentElement.insertBefore(it.prevEl, it.host);
  } catch {
    /* ignore */
  }
  try {
    it.host.remove();
  } catch {
    /* ignore */
  }
  stamp("menu slot removed");
}
