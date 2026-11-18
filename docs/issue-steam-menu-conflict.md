# Steam 左侧菜单注入与其他插件冲突：多个插件同时包装菜单组件时后注入者会被静默丢弃

## 环境

| 项目 | 值 |
| --- | --- |
| 设备 | Steam Deck (LCD) |
| 系统 | SteamOS 3.x |
| Decky Loader | 最新稳定版 |
| Decky Music | 最新版 |
| 冲突插件 | DeckMeido（同样采用「包装 Steam 内部菜单组件」的注入方式） |

## 现象

单独安装 Decky Music 时，左侧菜单显示正常：

```
主页 | 库 | 商店 | 音乐 | 好友与聊天 | 媒体 | 下载 | 设置 | 电源
```

单独安装 DeckMeido 时，同样正常：

```
主页 | 库 | 商店 | DeckMeido | 好友与聊天 | 媒体 | 下载 | 设置 | 电源
```

**两个插件同时安装时，DeckMeido 的入口完全不出现，且没有任何报错**，菜单回到只有「音乐」的状态：

```
主页 | 库 | 商店 | 音乐 | 好友与聊天 | 媒体 | 下载 | 设置 | 电源
```

DeckMeido 侧的注入逻辑确实执行了（我加了日志确认 `tryPatch` 每秒都在跑），但它拿不到可注入的位置。

## 复现步骤

1. 安装 Decky Music，确认左侧菜单出现「音乐」。
2. 安装任意第二个同样通过包装 Steam 左侧菜单组件来注入入口的插件（本例为 DeckMeido）。
3. 重启 `plugin_loader.service`，观察左侧菜单：只有「音乐」，「DeckMeido」不出现。
4. 把 Decky Music 的插件目录移出 `/home/deck/homebrew/plugins/` 后重启：DeckMeido 立即正常出现。

## 根因分析

Steam 的左侧菜单由内部组件 `Ie` 内联渲染，`Ie` 的宿主是外层组件 `fe`。两个插件都采用了同一套社区通行做法：

1. 把 `fe.type` 换成自己的 boundary；
2. 在自己的 boundary 里调用原 `fe`，从返回的 element 树里深搜 `<Ie>`；
3. 把 `Ie.type` 换成自己的 wrapper，在 wrapper 里往菜单项数组插入自己的项。

问题出在**第 2 步的所有权判定**上。我实测拿到的 fiber 链如下（`__deckyMusic` 表示 Decky Music 的标记，`__deckmeido` 表示 DeckMeido 的标记）：

```text
Outer          (dm=true, music=true)   ← 最外层
MenuBoundary
wrapped        (dm=true,  dmOrig=true)     ← DeckMeido 的 foreignOuter
MenuBoundary
wrapped        (music=true, musicOrig=true) ← Decky Music 的 Outer
MenuBoundary
wrapped        (music=true, musicOrig=true) ← Decky Music 的 Ie 包装
Fragment (tag 7)  ← 菜单项数组在这里
  Pe  Pe  Pe ...  ← 每个菜单项
```

`steamMenu.ts` 第 337-345 行：

```ts
const ieEl = deepFind(ret, isIeEl);
if (ieEl && typeof ieEl.type === "function") {
  if (isLegacyMenuWrapper(ieEl.type)) {
    ieEl.type = NullComponent;
    fuse(new Error("legacy menu wrapper"));
  } else if (!(ieEl.type as any).__deckyMusic) {
    ieEl.type = ieWrapperFor(ieEl.type);
  }
}
```

第 110 行 `isIeEl` 的判定条件是 element 上带 `loggedIn` / `menuOpen` 两个 prop。而 Decky Music 的 `ieWrapperFor`（第 266-294 行）把 `Ie.type` 换成了自己的 `wrapped`，并在其上打 `__deckyMusic = true`。

于是后注入的插件在 `deepFind(ret, isIeEl)` 里拿到的 element 的 `type` 已经是 Decky Music 的 `wrapped`：

- 若它判断「已被别的插件接管 → 跳过」，入口就不出现（这正是 DeckMeido 遇到的情况）；
- 若它强行覆盖 `ieEl.type`，则会把 Decky Music 的整层包装连同「音乐」入口一起抹掉。

链路之外还有第二个隐患：第 260 行

```ts
if (at >= 0) arr.splice(at + 1, 0, item);
```

Steam 原始的菜单项**没有显式 `key`**，React 会退化为用数组下标作隐式 key。任一插件 `splice` 插入后，后面所有项的下标都会平移，后渲染的一方会按位置复用错的孩子 —— 表现为「插入 A 之后 B 的入口被顶掉」或出现重复项。这也是两个插件无法共存的另一个原因。

## 建议的解决方向

不要求改动现有单插件行为，只在不破坏自身注入的前提下给其他插件留出位置。任选其一或组合：

### 方案 A（推荐）：不去包装 `Ie`，而是包装 `Ie` 的父级、在数组层面合作

给菜单项数组本身建立一层共享约定：所有插件都往同一个数组里插项，而不是各自把 `Ie.type` 换成自己的 wrapper 互相覆盖。

最小实现思路：把「找数组 → 插项」抽成一个幂等的操作，并在 `Ie` 的 wrapper 上暴露一个公开的合作接口，例如

```ts
// 在 ieWrapperFor 生成的 wrapped 上挂一个稳定契约
(wrapped as any).__menuItemInjectors = (wrapped as any).__menuItemInjectors ?? [];
```

这样其他插件在发现 `ieEl.type.__deckyMusic` 为真时，不再盲目跳过，而是把自己的插入函数 push 进去，由 Decky Music 在每轮渲染时统一调用。双方的项都会出现在同一个数组里，顺序由 anchor route 决定。

### 方案 B（最小改动）：给所有菜单项补显式 `key` 后再 splice

在 `injectItem` 插入之前，先把数组里每一项按自身 `route`（缺失时按原下标）补上稳定的显式 `key`，再插入自己的项。这样 React 按 key 复用时不会因下标平移而错位，其他插件即便各自 `splice` 也不会互相顶掉：

```ts
// 插入前先给原生项补稳定 key，避免隐式下标 key 让后插入者顶掉已有项
arr.forEach((x: any, i: number) => {
  if (x && typeof x === "object" && x.key == null) {
    arr[i] = cloneElement(x, { key: `steam-${i}-${x.props?.route ?? "x"}` });
  }
});
```

### 方案 C：文档化冲突，至少让失败可见

如果短期内不打算做协作式注入，建议在 README 或 `docs/design/steam-menu-inject.md` 中明确说明「本插件的菜单注入会独占 Steam 左侧菜单，与其他同类插件不兼容」，并避免使用 `fuse(...)` + `NullComponent` 静默清空别人的入口 —— 这种失败对用户完全不可见，很难自行排查。

## 我这边会做的兼容处理

为了不把兼容责任完全推给您，DeckMeido 侧我也会做以下调整：

- 优先检测 `Ie.type.__deckyMusic`，若存在则改为走方案 A 的合作式注入（把插入函数交给 Decky Music 统一调用）；
- 若对方版本不支持合作接口，则退化为 DOM 补一行（不改 React 树），保证双方入口都不丢；
- 保证自身失败时熔断且不影响 Steam 原生菜单。

如果您更倾向方案 A 或 B，我这边可以按您的实现细节对齐，避免两边各自定义不同的约定。

## 附：可自行验证的检查手段

在 Steam 的 CDP 调试端口（`localhost:8080`）连到 `SharedJSContext`，执行下面这段即可看到上图那份 fiber 链：

```js
(() => {
  let root = null;
  for (const el of Array.from(document.querySelectorAll("*"))) {
    const k = Object.keys(el).find((x) => x.startsWith("__reactFiber"));
    if (k) { let f = el[k]; for (let n = 0; f?.return && n < 1000; n++) f = f.return; root = f; break; }
  }
  const isFe = (f) => { const p = f?.memoizedProps; return !!p && p.bLoggedIn !== undefined && p.open !== undefined && "popup" in p; };
  let fe = null; const q = [root]; let n = 0;
  while (q.length && n++ < 20000) { const f = q.shift(); if (!f) continue; if (isFe(f)) { fe = f; break; } if (f.child) q.push(f.child); if (f.sibling) q.push(f.sibling); }
  const out = []; const st = [fe];
  while (st.length) {
    const f = st.pop(); if (!f) continue;
    out.push({ name: f.type?.name || (f.tag === 7 ? "Fragment" : null), tag: f.tag,
      music: !!f.type?.__deckyMusic, dm: !!f.type?.__deckmeido });
    if (f.child) st.push(f.child); if (f.sibling) st.push(f.sibling);
  }
  return JSON.stringify(out.slice(0, 6), null, 2);
})()
```
