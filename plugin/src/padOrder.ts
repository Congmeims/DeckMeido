import { call } from "@decky/api";

/**
 * 把 DeckMeido 的虚拟手柄换到 1P（Steam 的 0 号槽位）。
 *
 * 控制器排序只存在 Steam 进程的内存里，插件后端和主程序都够不着，
 * 只有跑在 Steam 页面里的这段 JS 能调 SteamClient.Input。
 *
 * 后端在虚拟手柄建好 / 销毁时广播 pad_order：
 *   to1p   新插上的手柄排在物理手柄后面（就是 2P），不挪到 1P 游戏根本不看它
 *   revert 虚拟手柄没了，确认物理手柄回到 1P，免得下次开机游戏收不到输入
 */

type SteamController = {
  nControllerIndex?: number;
  nXInputIndex?: number;
  nAIndex?: number;
  strName?: string;
  strType?: string;
};

type SteamInputApi = {
  GetControllers?: () => SteamController[];
  GetControllersSorted?: () => SteamController[];
  SwapControllerOrder?: (from: number, to: number) => void;
};

type SteamWsApi = {
  RegisterForControllerList?: (cb: (list: SteamController[]) => void) => unknown;
  SwapControllerOrderByIndex?: (from: number, to: number) => void;
};

export function steamClient(): any {
  const w = window as any;
  return w.SteamClient || w.SP?.SteamClient || null;
}

// 虚拟手柄：名字认 DeckMeido（内核上报的设备名）；认不出就退回
// 「不是 Valve 自家手柄」那一个 —— Steam Deck 本体手柄的 strType 带 Steam/Valve
function isVirtualPad(c: SteamController): boolean {
  return (
    /deckmeido/i.test(String(c?.strName || "")) ||
    !/steam|valve/i.test(String(c?.strType || ""))
  );
}

// 槽位下标：新老客户端字段名不一样，挨个试
function padSlot(c: SteamController): number | undefined {
  for (const v of [c?.nXInputIndex, c?.nAIndex]) {
    if (typeof v === "number" && v >= 0) return v;
  }
  return undefined;
}

function swapBy(client: any, api: SteamWsApi, from: SteamController, to: number): boolean {
  const byIndex = api.SwapControllerOrderByIndex;
  if (typeof byIndex === "function" && typeof from.nControllerIndex === "number") {
    byIndex.call(api, from.nControllerIndex, to);
    return true;
  }
  // 注意：SteamClient.Input.SwapControllerOrder 的两个参数都是「槽位」
  // （nXInputIndex），不是 nControllerIndex —— 这是 Steam 自己的
  // 「为控制器重新排序」面板里的用法：
  //   SwapControllerOrder(items[from].index, items[to].index)
  const slot = padSlot(from);
  const input: SteamInputApi | undefined = client?.Input;
  if (typeof input?.SwapControllerOrder === "function" && typeof slot === "number") {
    input.SwapControllerOrder(slot, to);
    return true;
  }
  return false;
}

/** 当前 Steam 认识的控制器列表（新客户端有 GetControllers，老的只能靠事件回调） */
async function fetchControllers(client: any): Promise<SteamController[]> {
  const input: SteamInputApi | undefined = client?.Input;
  for (const fn of [input?.GetControllers, input?.GetControllersSorted]) {
    if (typeof fn === "function") {
      try {
        const list = fn.call(input);
        if (Array.isArray(list)) return list;
      } catch {
        /* 老客户端里可能只是占位，走下面的 WebSocket 版本 */
      }
    }
  }
  const ws: SteamWsApi | undefined = client?.WebSocket;
  if (typeof ws?.RegisterForControllerList === "function") {
    return await new Promise<SteamController[]>((resolve) => {
      let done = false;
      try {
        ws.RegisterForControllerList!((list) => {
          if (done || !Array.isArray(list)) return;
          done = true;
          resolve(list);
        });
      } catch {
        resolve([]);
      } finally {
        // 回调没来时别一直挂着
        setTimeout(() => {
          if (!done) {
            done = true;
            resolve([]);
          }
        }, 1200);
      }
    });
  }
  return [];
}

/** 换序结果同时写进插件日志：QAM 里看不到 console，出问题只能靠日志 */
export function logPadOrder(msg: string) {
  console.log("[DeckMeido]", msg);
  call("_probe_log", "[padOrder] " + msg).catch(() => undefined);
}

export async function applyPadOrder(action: "to1p" | "revert"): Promise<void> {
  // 防重入：面板重挂载会重新注册监听，同一个请求可能来两次；
  // 两路同时正在换会把顺序搅乱
  if (padOrderBusy) return;
  if (action === "revert" && !padOrderWanted1P) return; // 没换过就不用还原
  padOrderBusy = true;
  try {
    await doPadOrder(action);
  } finally {
    padOrderBusy = false;
    if (action === "to1p") padOrderWanted1P = true;
  }
}

// 本次装的是不是刚把虚拟手柄挪到 1P（决定销毁时要不要还原）
let padOrderWanted1P = false;
let padOrderBusy = false;

async function doPadOrder(action: "to1p" | "revert"): Promise<void> {
  const client = steamClient();
  if (!client) {
    logPadOrder(`没有 SteamClient，跳过换序（${action}）`);
    return;
  }
  const api: SteamWsApi | undefined = client.WebSocket;

  // 设备刚创建时 Steam 还没枚举到它，所以有耐心地重试几轮
  const tries = action === "to1p" ? 12 : 2;
  for (let i = 0; i < tries; i++) {
    // 留出时间让 Steam 把手柄枚举进去，再问它现在有几只手柄
    if (i > 0) await new Promise((r) => setTimeout(r, 300));
    const list = (await fetchControllers(client)).filter(
      (c) => c && typeof c.nControllerIndex === "number"
    );
    const withSlot = list
      .map((c) => ({ c, slot: padSlot(c) }))
      .filter((x) => typeof x.slot === "number");

    if (action === "to1p") {
      const pad = withSlot.find((x) => isVirtualPad(x.c));
      if (!pad) continue;
      if (pad.slot === 0) return; // 已经是 1P
      const ok = swapBy(client, api || {}, pad.c, 0);
      logPadOrder(`换到 1P ${ok ? "已发出" : "失败"}：${JSON.stringify(list)}`);
      return;
    }

    // revert：虚拟手柄已销毁，槽位会自动前移；只有 1P 还被「非 Valve 手柄」占着才动手
    const first = withSlot.find((x) => x.slot === 0);
    if (!first || !isVirtualPad(first.c)) return;
    const deck = withSlot.find((x) => !isVirtualPad(x.c));
    if (!deck) return;
    const ok = swapBy(client, api || {}, deck.c, 0);
    logPadOrder(`还原 ${ok ? "已发出" : "失败"}：${JSON.stringify(list)}`);
    return;
  }
  logPadOrder(`换序未完成（${action}）：Steam 一直没枚举到虚拟手柄`);
}
