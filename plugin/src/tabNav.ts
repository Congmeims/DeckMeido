import { GamepadButton } from "@decky/ui";

/**
 * L1 / R1 换算成页签步进。
 *
 * 大屏 FooterLegend 与 Steam 控制器消息用的是同一套 GamepadButton 枚举：
 * BUMPER_LEFT = 5、BUMPER_RIGHT = 6。小屏原来按 6/7 判断（把 6 当 L1），
 * 结果 L1 完全没绑上、R1 反而变成「上一页」，看起来就是切换失效。
 * 两个界面都只走这里，避免再各写一份数字。
 */
export function bumperStep(button: number | undefined): -1 | 0 | 1 {
  if (button === GamepadButton.BUMPER_LEFT) return -1;
  if (button === GamepadButton.BUMPER_RIGHT) return 1;
  return 0;
}

/** 夹在 0..total-1 之间，避免在首页/末页继续越界 */
export function stepTab(index: number, step: number, total: number): number {
  return Math.max(0, Math.min(total - 1, index + step));
}

// 同一次物理按键可能同时经 Focusable 与控制器消息两路上报；
// 两路都用这个时间戳去重，避免一下跳两页。
let lastBumpAt = 0;

/** 带 200ms 去重的步进；返回 0 表示「不是 L1/R1 或刚处理过」 */
export function bumpOnce(button: number | undefined): -1 | 0 | 1 {
  const step = bumperStep(button);
  if (!step) return 0;
  const now = Date.now();
  if (now - lastBumpAt < 200) return 0;
  lastBumpAt = now;
  return step;
}
