/**
 * 状态文案的唯一出口：大屏和 QAM 都从这里拿，避免两处措辞不一致。
 *
 * 昵称来自后端 persona 的 name（随 get_status 一起返回，后端内部读的是
 * /api/config）。这里只做进程内缓存，不额外发请求：状态一刷新就更新，
 * 两个界面共用同一份。
 */

/** 昵称兜底：取不到时不能空着，也不能显示「未设置」 */
export const DEFAULT_PERSONA_NAME = "Meido";

let cachedPersonaName = DEFAULT_PERSONA_NAME;

/** 把 get_status 带回来的昵称同步进缓存；缺失时保留上一次（含默认值） */
export function syncPersonaName(status?: { persona_name?: string } | null): string {
  const name = String(status?.persona_name ?? "").trim();
  if (name) cachedPersonaName = name;
  return cachedPersonaName;
}

export function personaName(): string {
  return cachedPersonaName || DEFAULT_PERSONA_NAME;
}

export type StatusLineState = {
  /** 忙碌时不显示「思考中」，只在忙碌期间显示「正在处理…」 */
  busy?: boolean;
  /** 只有 game_confidence === "high" 才传游戏名，没有就退回通用句 */
  game?: string;
};

/** 本地模板兜底；后端接口不可用时的唯一文案来源 */
export function localStatusLine(state: StatusLineState = {}): string {
  const name = personaName();
  if (state.busy) return `${name}，正在处理…`;
  const game = String(state.game ?? "").trim();
  if (game) return `${name}，在《${game}》里待命~`;
  return `${name}，正在待命~`;
}

/**
 * 优先用后端 GET /api/status/line 的文案，失败/不存在就静默回退本地模板。
 * 该接口本轮还没实现，所以这里暂不请求；后端上线后只改这一个函数：
 * 发请求 → 拿到非空文案就 return，否则 return localStatusLine(state)。
 */
export function statusLine(state: StatusLineState = {}): string {
  return localStatusLine(state);
}

/** 面板/大屏的便捷入口：顺手同步昵称，再按状态生成一句话 */
export function statusLineFor(
  status: { persona_name?: string; game_name?: string; game_confidence?: string } | null | undefined,
  busy: boolean
): string {
  syncPersonaName(status);
  const game = status && status.game_confidence === "high" ? status.game_name : "";
  return statusLine({ busy, game });
}
