import { definePlugin, routerHook } from "@decky/api";
import { GiTeapot } from "react-icons/gi";
import BigScreen from "./BigScreen";
import QamScreen from "./QamScreen";
import { BIG_SCREEN_ROUTE } from "./useSession";

export default definePlugin(() => {
  // 全屏页挂到 Steam 的 GamepadUI 路由里，从 QAM 用 Navigation.Navigate 进入；
  // 卸载时移除，避免插件重载后路由残留。路由 API 不可用时只记日志，不能拖垮 QAM。
  try {
    routerHook.addRoute(BIG_SCREEN_ROUTE, () => <BigScreen />);
  } catch (e) {
    console.error("[DeckMeido] 大屏路由注册失败", e);
  }

  return {
  name: "DeckMeido",
  titleView: <div>DeckMeido</div>,
  // QAM 面板：分页窄屏版（询问 / 输入 / 设置）
  content: <QamScreen />,
  // 图标外框占位与其它插件保持一致，只把墨迹放大，避免把标题推歪
  icon: (
    <div style={{ width: "18px", height: "18px", display: "flex", alignItems: "center", justifyContent: "center", flex: "none" }}>
      <GiTeapot size={20} style={{ transform: "scale(1.15)", transformOrigin: "center" }} />
    </div>
  ),
  onDismount() {
    try {
      routerHook.removeRoute(BIG_SCREEN_ROUTE);
    } catch (e) {
      console.error("[DeckMeido] 大屏路由移除失败", e);
    }
  },
  };
});
