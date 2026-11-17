/**
 * 在 Steam GamepadUI 左侧菜单里注入一个「DeckMeido」入口。
 *
 * Decky 没有提供往左侧菜单加项的公开 API，这里沿用社区通行做法（Decky Music 等插件同一思路）：
 * 直接改 Steam 自己 React fiber 树上的组件类型——
 *   1. 从 DOM 上的 __reactFiber$ 属性往上爬到 HostRoot；
 *   2. 按 props 特征找到左侧菜单的外层组件（Fe）；
 *   3. 外层类型换成 wrapper + 错误边界，wrapper 渲染后从返回的 element 树里
 *      找到内层菜单项容器（Ie），把它的 type 换成另一个 wrapper；
 *   4. 内层 wrapper 渲染后，在「商店」后面插一个 clone 出来的原生菜单项，
 *      只换 route / label / key / icon；
 *   5. 菜单 remount（进出游戏、睡眠唤醒）后 fiber 会被摘掉，所以每秒检查一次，断了就重打。
 *
 * 约束（每条都对应一次线上事故的教训）：
 *   - 绝不重复包裹自己；wrapper 上带 original 指针，卸载时能把 type 还原回去；
 *   - 任何异常都熔断，熔断后立刻停止注入并清定时器，菜单回到原生状态；
 *   - 原 fiber 的 tag 是 FunctionComponent，不能直接把 type 换成 class，
 *     否则 React 会按旧 tag 当函数调用并抛 "Class constructor cannot be invoked without 'new'"；
 *   - 只依赖 props 特征匹配，不依赖被混淆的组件名。
 */
import { cloneElement, createElement, Component, ReactElement, ReactNode } from "react";
import { GiTeapot } from "react-icons/gi";

/** 注入项的 route，与 index.tsx 里 routerHook 注册的同一个常量（避免循环依赖，这里重复定义一份） */
const MENU_ROUTE = "/deckmeido";
/** 插到这一项之后：/steamweb 是「商店」，菜单里第 4 位 */
const AFTER_ROUTE = "/steamweb";
const ITEM_KEY = "deckmeido-menu-item";
/** 菜单 fiber 被摘掉后重新打补丁的检查间隔 */
const RETRY_MS = 1000;
/** 渲染深度上限：注入 wrapper 递归调用自己时兜底 */
const MAX_RENDER_DEPTH = 8;
const MARK = "__deckmeido";
/** 别的插件 wrapper 上指向原组件的属性（Decky Music 用这个键） */
const FOREIGN_ORIGINAL = "__deckyMusicOriginal";

type AnyFiber = {
  type: any;
  return?: AnyFiber | null;
  child?: AnyFiber | null;
  sibling?: AnyFiber | null;
  alternate?: AnyFiber | null;
  memoizedProps?: any;
  tag?: number;
};

type FiberPred = (f: AnyFiber) => boolean;

const HOST_ROOT = 3;

/** 左侧菜单外层组件：带登录态、展开态和 popup */
const isMenuRootFiber: FiberPred = (f) => {
  const p = f?.memoizedProps;
  return !!p && p.bLoggedIn !== undefined && p.open !== undefined && "popup" in p;
};

/** 左侧菜单项容器：element 层面，带 loggedIn / menuOpen */
const isMenuContainerEl = (x: any) =>
  x &&
  typeof x === "object" &&
  x.props &&
  x.props.loggedIn !== undefined &&
  x.props.menuOpen !== undefined &&
  typeof x.type === "function";

/** 原生菜单项：有 route 和 label 的 element */
const isMenuItemEl = (x: any) =>
  x &&
  typeof x === "object" &&
  x.props &&
  typeof x.props.route === "string" &&
  typeof x.props.label === "string" &&
  x.type &&
  typeof x.type !== "string";

function isClassComponent(type: any): boolean {
  if (typeof type !== "function") return false;
  const proto = type.prototype;
  return !!proto && typeof proto === "object" && "isReactComponent" in proto && Boolean(proto.isReactComponent);
}

/** 热更新残留的 wrapper 没有 original，解不链，直接用空组件顶掉 */
const NullComponent = () => null;
function isLegacyWrapper(type: any): boolean {
  if (typeof type !== "function") return false;
  return Boolean(type[MARK]) && !type[`${MARK}Original`];
}

/**
 * 沿 DOM 节点上的 __reactFiber$ 往上爬到根。
 * 不能直接用 __reactContainer（或 DOM 容器上 fiber 的 stateNode.current）：
 * 那是 Steam UI 根容器的另一棵树，从它出发找不到 Decky 渲染的菜单 fiber，
 * 于是只会在 Decky 自己的边界上打补丁 —— 表现为「音乐在、自己没出现」。
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
  // 用 BFS 而不是 DFS：外层 fiber 会把自己的 props 原样透传给内层（Outer → MenuBoundary → 原组件），
  // 三者都满足 props 特征。DFS 会撞到最里面那个，把它当成菜单根去反复包，菜单就会一直重挂载。
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

/** 在 element 树里 DFS（数组 + props.children），带环保护 */
function deepFind(node: any, pred: (x: any) => boolean, cap = 8000): any {
  return deepFindFirst(node, pred, cap);
}

/**
 * 在 element 树里 DFS，返回第一个满足 pred 的节点；
 * 传 all=true 时收集全部满足的节点，用于和别的插件共享同一个容器。
 */
function deepFindFirst(node: any, pred: (x: any) => boolean, cap = 8000, all = false): any {
  let n = 0;
  const hits: any[] = [];
  const seen = new Set();
  const stack: any[] = [node];
  while (stack.length) {
    const cur = stack.pop();
    if (cur == null || n++ > cap) continue;
    try {
      if (pred(cur)) {
        if (!all) return cur;
        hits.push(cur);
      }
    } catch {
      /* ignore */
    }
    if (Array.isArray(cur)) {
      for (const x of cur) stack.push(x);
      continue;
    }
    if (typeof cur === "object") {
      if (seen.has(cur)) continue;
      seen.add(cur);
      if (cur.props && cur.props.children != null) stack.push(cur.props.children);
    }
  }
  return all ? hits : null;
}

// ---- 运行态 ----
let timer: ReturnType<typeof setInterval> | null = null;
let patched: { fiber: AnyFiber; original: any } | null = null;
let fused = false;
let epoch = 0;
let guardedDepth = 0;

function log(...args: any[]) {
  try {
    console.warn("[DeckMeido]", ...args);
  } catch {
    /* console 不可用不能影响 UI */
  }
}

function fuse(reason: unknown) {
  if (fused) return;
  fused = true;
  const at = epoch;
  log("菜单注入已熔断", reason);
  try {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  } catch {
    /* ignore */
  }
  // 下一轮再真正卸载，避免在 Steam 的 render 栈里同步改 fiber
  try {
    setTimeout(() => {
      if (at !== epoch) return;
      try {
        disableMenuInjection();
      } catch {
        /* fail-closed：入口消失，UI 不受影响 */
      }
    }, 0);
  } catch {
    /* ignore */
  }
}

function boundedRender<T>(fn: () => T): T | null {
  if (guardedDepth >= MAX_RENDER_DEPTH) {
    fuse(new Error("menu render depth exceeded"));
    return null;
  }
  guardedDepth++;
  try {
    return fn();
  } finally {
    guardedDepth--;
  }
}

/** 数组（或 Fragment 的 props 对象）里是否已有菜单项 */
const hasMenuItem = (arr: any[]): boolean => arr.some(isMenuItemEl);

/** 在任意「菜单项数组」里插入自己的项：route 即唯一标识，天然去重 */
function injectIntoArray(arr: any[]) {
  if (arr.some((x: any) => x?.props?.route === MENU_ROUTE)) return;
  const tmpl = arr.find(isMenuItemEl);
  if (!tmpl) return;
  // 图标与 QAM 面板保持一致（茶壶）
  const item = cloneElement(tmpl as ReactElement, {
    route: MENU_ROUTE,
    label: "DeckMeido",
    key: ITEM_KEY,
    // 图标外框与原生菜单图标同宽，只把墨迹放大：外框占位决定文字左边距，
    // 直接放大 svg 会把整行文字往右顶。marginLeft 微调整行与上方原生项的左缘。
    icon: createElement(
      "div",
      {
        style: {
          width: 19,
          height: 22,
          flex: "0 0 19px",
          marginLeft: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        },
      },
      createElement(GiTeapot, {
        size: 22,
        style: { transform: "scale(1.2)", transformOrigin: "center" },
      } as any)
    ),
  } as any);
  // 优先按已知 route 定位，Steam 增删项也不串位；锚点缺失退到末尾
  const at = arr.findIndex((x: any) => x?.props?.route === AFTER_ROUTE);
  if (at >= 0) arr.splice(at + 1, 0, item);
  else arr.push(item);
}

/**
 * 直接改 fiber 上已经渲染好的菜单项数组。
 *
 * 菜单项不是由「可插手的 element 链」生成的：Steam 用 jsx 把 item 数组作为一个 Fragment 的
 * props 渲染（tag 7，props 就是 {0: item, 1: item, ...}），外层包装改 type 也碰不到它。
 * 这里找到这个数组原位 splice，current 与 alternate 同时改，任何一次重渲染都会带上入口。
 */
function injectIntoFiberArray(fe: AnyFiber): boolean {
  if (fused) return false;
  const stack: AnyFiber[] = [fe];
  let n = 0;
  while (stack.length && n++ < 20000) {
    const f = stack.pop()!;
    const props: any = f.memoizedProps;
    if (props) {
      const isFragmentProps =
        f.tag === 7 && Array.isArray(Object.keys(props)) && Object.keys(props).some((k) => /^\d+$/.test(k));
      if (isFragmentProps) {
        const keys = Object.keys(props).filter((k) => /^\d+$/.test(k));
        const arr = keys.map((k) => props[k]);
        if (hasMenuItem(arr)) {
          const before = arr.length;
          injectIntoArray(arr);
          if (arr.length === before) return false; // 已经有入口，无需再改
          keys.forEach((k, i) => (props[k] = arr[i]));
          const alt: any = f.alternate?.memoizedProps;
          if (alt && alt !== props) {
            const altKeys = Object.keys(alt).filter((k) => /^\d+$/.test(k));
            if (altKeys.length) {
              const altArr = altKeys.map((k) => alt[k]);
              if (altArr.some((x: any) => x?.props?.route === MENU_ROUTE)) return true;
              const target = altArr.findIndex((x: any) => x === arr[0] || x?.props?.route === arr[0]?.props?.route);
              if (target >= 0) {
                const mine = arr.find((x: any) => x?.props?.route === MENU_ROUTE);
                if (mine) altArr.splice(target, 0, mine);
                altKeys.forEach((k: string, i: number) => (alt[k] = altArr[i]));
              }
            }
          }
          return true;
        }
      }
    }
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
  }
  return false;
}

/**
 * 只改 memoizedProps 不够：React 用 current !== wip 判断「这棵子树上一轮已经渲染过」，
 * 会直接 bailout 而不为新增的 props 生成子 fiber —— 表现为 props 里明明有入口、DOM 里却没有。
 * 这里顺着已插入项的 fiber 找到最近的宿主 DOM 节点，对最近的 class 祖先触发一次 setState，
 * 让 React 从那里开始真正 re-render 一遍（fiber 调度细节不做硬编码，交给 React 自己）。
 */
function hostNodeOf(f: AnyFiber | null | undefined) {
  for (let q = f, n = 0; q && n < 12; q = q.child, n++) {
    for (let x: AnyFiber | null | undefined = q; x; x = x.sibling) {
      if (x.tag === 5 && x.stateNode && x.stateNode.nodeType === 1) return x.stateNode as any;
    }
  }
  return null;
}

function forceUpdate(f: AnyFiber) {
  try {
    const el = hostNodeOf(f);
    const k = el && Object.keys(el).find((key: string) => key.startsWith("__reactFiber"));
    let q: AnyFiber | null | undefined = k ? el[k] : null;
    for (let n = 0; q && n < 40; q = q.return, n++) {
      const inst = (q as any).stateNode;
      if (typeof q.type === "function" && inst && typeof inst.forceUpdate === "function") {
        inst.forceUpdate();
        return;
      }
    }
  } catch {
    /* 触不触发都行：下一次导航/重渲染也会带上 */
  }
}

/** 丢到下一轮事件循环执行：避免在 React render 栈里同步改状态 */
function later(fn: () => void) {
  try {
    setTimeout(fn, 0);
  } catch {
    /* ignore */
  }
}

/** 当前菜单容器是否真的能被找到、并插进项；找不到时把原因记下来 */
function injectItem(containerRet: any) {
  if (fused) return;
  const arr = deepFind(containerRet, (nd) => Array.isArray(nd) && nd.some(isMenuItemEl));
  if (!arr) {
    diag("menu items array miss");
    return;
  }
  injectIntoArray(arr);
}

/**
 * 给内层菜单容器（Ie）打补丁：替换它输出的 element 里的菜单项数组。
 * 与 Decky Music 一致：外层 fe 只改「它渲染出的 Ie 的 type」，真正插项在 Ie 的 wrapper 里做。
 * 插项失败只记诊断，不熔断——菜单其余部分必须保持原生可用。
 */
/**
 * 缓存容器 wrapper：按「原始容器引用」缓存，保证同一个 type 引用。
 * 每次 tick 都新建 wrapper 会让 ieEl.type 每次都变，React 会把整个菜单容器当成新组件
 * 卸载重挂载 —— 别的插件（Decky Music）打在旧容器上的补丁随之丢失，它的入口就消失。
 */
const ieWrapperCache = new WeakMap<any, any>();

function containerWrapperFor(OrigIe: any): any {
  if (typeof OrigIe !== "function") return OrigIe;
  if (isClassComponent(OrigIe)) return OrigIe; // class 不能用 apply 调，直接放弃注入
  if (isLegacyWrapper(OrigIe)) {
    fuse(new Error("legacy container wrapper"));
    return NullComponent;
  }
  if (OrigIe[MARK]) return OrigIe; // 绝不再包自己的 wrapper
  const cached = ieWrapperCache.get(OrigIe);
  if (cached) return cached;
  // 别的插件（Decky Music）的 wrapper：留在链上继续调用它，再在它的结果里插自己的项，
  // 这样两边都会往同一个菜单项数组里 push，互不覆盖。
  const foreign = (OrigIe as any)[FOREIGN_ORIGINAL];
  const wrappedIe = function (this: any) {
    return boundedRender(() => {
      const ret = OrigIe.apply(this, arguments as any);
      if (!fused) {
        try {
          injectItem(ret);
        } catch (e) {
          diag("injectItem throw", String(e));
        }
      }
      return ret;
    });
  };
  (wrappedIe as any)[MARK] = true;
  // 同时打上 Decky Music 的标记：它的外层每次渲染都会检查 ieEl.type.__deckyMusic，
  // 没有就再包一层（type 每秒都变 → 菜单整体重挂载 → 它的入口消失）。
  // 打上标记后它认为已包裹，不再动我们的 wrapper，两边各自往菜单项数组里插一项。
  (wrappedIe as any).__deckyMusic = true;
  (wrappedIe as any)[`${MARK}Original`] = foreign && typeof foreign === "function" ? foreign : OrigIe;
  ieWrapperCache.set(OrigIe, wrappedIe);
  return wrappedIe;
}

function patchContainer(ieEl: any) {
  if (!ieEl || typeof ieEl.type !== "function") return;
  const OrigIe: any = ieEl.type;
  if (isLegacyWrapper(OrigIe)) {
    ieEl.type = NullComponent;
    fuse(new Error("legacy container wrapper"));
    return;
  }
  if (OrigIe[MARK]) {
    // 已经是自己的 wrapper：记下来的只是渲染结果里的快照，改它不会影响下一轮渲染，
    // 因此不需要留引用。
    return;
  }
  const next = containerWrapperFor(OrigIe);
  if (next === OrigIe) return;
  diag("container patched", OrigIe.name || "anonymous");
  ieEl.type = next;
}

/**
 * 给菜单外层 fiber 套错误边界。
 * 外层保持普通函数组件（与原 fiber 的 tag 一致），由 React 单独为 class 边界建 fiber。
 */
function menuRootBoundary(wrapped: any, original: any) {
  class MenuBoundary extends Component<{ children?: ReactNode }, { failed: boolean }> {
    constructor(props: any) {
      super(props);
      this.state = { failed: false };
    }
    static getDerivedStateFromError() {
      return { failed: true };
    }
    componentDidCatch(error: unknown) {
      fuse(error);
    }
    render() {
      if (this.state.failed) return null;
      const Child = wrapped;
      return createElement(Child as any, this.props);
    }
  }
  const Outer = function (props: any) {
    return createElement(MenuBoundary as any, props);
  };
  (Outer as any)[MARK] = true;
  (Outer as any)[`${MARK}Original`] = original;
  return Outer;
}

/** 沿 return 链能否走到 HostRoot：能则说明已打补丁的 fiber 仍挂在树上 */
function stillPatched(): boolean {
  const fe = patched?.fiber;
  if (!fe || !patched) return false;
  // 共享模式（fe.type 是别的插件包的外层，本插件没改它）：只需确认外层还在树上，
  // 菜单项由 injectIntoFiberArray 每轮补齐。
  const shared = !fe.type?.[MARK];
  if (!shared && (isLegacyWrapper(fe.type) || !fe.type?.[MARK])) return false;
  for (let f: AnyFiber | null | undefined = fe, n = 0; f && n < 1000; f = f.return, n++) {
    if (f.tag === HOST_ROOT) return true;
  }
  return false;
}

/**
 * 共享模式不能用 stillPatched 早退：Steam 每次重渲染都从头 jsx 一份菜单项数组，
 * 我们改进去的项会被覆盖掉，所以每轮都要重新注入（injectIntoArray 自身按 route 去重）。
 */
function needsInject(): boolean {
  const fe = patched?.fiber;
  return !fe || !stillPatched() || !fe.type?.[MARK];
}

function tryPatch() {
  try {
    if (fused) {
      diag("fused");
      return;
    }
    if (!needsInject()) return;
    const root = rootFiber();
    if (!root) {
      diag("rootFiber miss");
      return;
    }
    const fe = findFiber(root, isMenuRootFiber);
    if (!fe) {
      diag("menu root fiber miss");
      return;
    }
    // 已打过补丁（包装式或直接改菜单项数组）就不用再动；顺带确认菜单项真的在
    if (fe.type?.[MARK]) return;
    if (!((fe.type as any)?.[FOREIGN_ORIGINAL] || (fe.type as any)?.__deckyMusic) && typeof fe.type !== "function") {
      diag("menu root type unsupported", typeof fe.type, fe.tag);
      return;
    }
    if (!((fe.type as any)?.[FOREIGN_ORIGINAL] || (fe.type as any)?.__deckyMusic) && isClassComponent(fe.type)) {
      diag("menu root type unsupported", typeof fe.type, fe.tag);
      return;
    }
    if (fe.type[MARK]) {
      if (isLegacyWrapper(fe.type)) {
        fe.type = NullComponent;
        if (fe.alternate) fe.alternate.type = NullComponent;
        fuse(new Error("legacy menu wrapper"));
      }
      return;
    }
    const Orig = fe.type;
    // 外层已经被别的插件（Decky Music）包成 Outer→MenuBoundary：不改它的 type，
    // 直接把项塞进已经渲染出来的菜单项数组（实测外层被包过时，wrapper 里拿到的 element 树插不进去）。
    if ((Orig as any)[FOREIGN_ORIGINAL] || (Orig as any).__deckyMusic) {
      if (injectIntoFiberArray(fe)) {
        forceUpdate(fe);
        diag("array inject", Orig.name || "anonymous");
      }
      patched = { fiber: fe, original: Orig };
      return;
    }
    const wrapped = function (this: any) {
      return boundedRender(() => {
        if (fused) return Orig.apply(this, arguments as any);
        let ret: any;
        try {
          ret = Orig.apply(this, arguments as any);
        } catch (e) {
          // 原组件自己抛的异常交给上层 ErrorBoundary，这里不吞
          throw e;
        }
        try {
          if (ret != null) {
            // 外层返回的容器可能不止一个，且可能已被别的插件包过；挑没包过的那个打补丁
            const els = deepFindFirst(ret, isMenuContainerEl, 8000, true) as any[];
            const target = els.find((x) => !(x.type && x.type[MARK]));
            if (!target) diag("container all owned", els.length);
            patchContainer(target);
          }
        } catch (e) {
          diag("outer patch throw", String(e));
        }
        // 兜底：菜单项数组是 Steam 自己 jsx 出来的，可能不在返回值里，再按整棵 fiber 补一次。
        // forceUpdate 不能放在这个 render 里（会同步 setState 报错），丢到下一轮 tick。
        try {
          if (injectIntoFiberArray(fe)) later(() => forceUpdate(fe));
        } catch (e) {
          diag("fiber array inject throw", String(e));
        }
        return ret;
      });
    };
    (wrapped as any)[MARK] = true;
    (wrapped as any)[`${MARK}Original`] = Orig;
    const boundary = menuRootBoundary(wrapped, Orig);
    fe.type = boundary;
    if (fe.alternate) fe.alternate.type = boundary;
    patched = { fiber: fe, original: Orig };
    diag("menu root patched", typeof Orig, Orig.name || "anonymous");
  } catch {
    /* 单轮失败下次重试 */
  }
}

/** 诊断输出节流：同一条信息 10 秒内只打一次，避免每秒刷屏 */
const diagSeen = new Map<string, number>();
function diag(...args: any[]) {
  try {
    const key = String(args[0]);
    const now = Date.now();
    const last = diagSeen.get(key) ?? 0;
    if (now - last < 10000) return;
    diagSeen.set(key, now);
    log(...args);
  } catch {
    /* ignore */
  }
}

/** 卸载时撤掉直接改进去的菜单项（重新编号，避免数组出现空洞） */
function removeFromFiberArray(fe: AnyFiber) {
  const stack: AnyFiber[] = [fe];
  let n = 0;
  while (stack.length && n++ < 20000) {
    const f = stack.pop()!;
    const props: any = f.memoizedProps;
    if (props && f.tag === 7) {
      const keys = Object.keys(props).filter((k) => /^\d+$/.test(k));
      if (keys.some((k) => props[k]?.props?.route === MENU_ROUTE)) {
        const arr = keys.map((k) => props[k]).filter((x: any) => x?.props?.route !== MENU_ROUTE);
        keys.forEach((k, i) => (props[k] = arr[i]));
        keys.slice(arr.length).forEach((k) => delete props[k]);
        return;
      }
    }
    if (f.child) stack.push(f.child);
    if (f.sibling) stack.push(f.sibling);
  }
}

export function enableMenuInjection() {
  if (timer) return;
  epoch++;
  fused = false;
  patched = null;
  timer = setInterval(tryPatch, RETRY_MS);
  tryPatch();
}

export function disableMenuInjection() {
  epoch++;
  fused = true;
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  try {
    if (patched) {
      const root = patched.fiber;
      patched.fiber.type = patched.original;
      if (patched.fiber.alternate) patched.fiber.alternate.type = patched.original;
      // 直接改进去的菜单项同样要撤掉，否则插件卸载后入口还留在菜单里
      removeFromFiberArray(root);
    }
  } catch {
    /* 还原失败也无所谓，fiber 迟早被重建 */
  }
  patched = null;
}
